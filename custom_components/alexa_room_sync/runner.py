"""Run a room sync end to end: plan, reconcile, apply, remember, report.

Shared by the services and the buttons so both behave the same way.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import alexa_client as ac
from . import sync as sync_mod
from .const import CONF_EMAIL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SIGNAL_RESULT = f"{DOMAIN}_result_{{entry_id}}"
NOTIFICATION_ID = f"{DOMAIN}_result"


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    stores = hass.data.setdefault(DOMAIN, {}).setdefault("_stores", {})
    if entry.entry_id not in stores:
        stores[entry.entry_id] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.rooms"
        )
    return stores[entry.entry_id]


async def run_sync(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    only_area_id: str | None = None,
    apply: bool = False,
    auto: bool = False,
) -> dict[str, Any]:
    """Plan and (if ``apply``) carry out the sync. Returns the service response.

    The outcome goes to the Last result sensor and the device's Activity. An
    ``auto`` run (auto-sync) only reports when it changed something or failed, so
    the hourly check doesn't fill Activity with "nothing to do".
    """
    async with _lock(hass, entry):
        return await _run_sync(hass, entry, only_area_id=only_area_id, apply=apply,
                               auto=auto)


def _lock(hass: HomeAssistant, entry: ConfigEntry) -> asyncio.Lock:
    """One run at a time per account, so a button press and an auto-sync can't
    both write the same groups."""
    locks = hass.data.setdefault(DOMAIN, {}).setdefault("_locks", {})
    return locks.setdefault(entry.entry_id, asyncio.Lock())


async def _run_sync(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    only_area_id: str | None,
    apply: bool,
    auto: bool,
) -> dict[str, Any]:
    # Older versions posted a notification; clear any that's still lying around.
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
    scope = "Auto-sync" if auto else _scope(hass, only_area_id)
    try:
        login = ac.get_login_obj(hass, entry.data.get(CONF_EMAIL))
        plan = await sync_mod.build_plan(
            hass, login, dict(entry.options), only_area_id=only_area_id
        )
    except ac.AlexaClientError as err:
        details = {"scope": scope, "mode": "error", "ran_at": dt_util.utcnow().isoformat(),
                   "groups": {}, "error": str(err)}
        _publish(hass, entry, f"{scope} – didn't run: {str(err)[:180]}", details)
        _log_to_activity(hass, entry, details)
        raise HomeAssistantError(str(err)) from err

    store = _store(hass, entry)
    data = await store.async_load() or {}
    managed: dict[str, dict] = dict(data.get("rooms", {}))
    _check_groups_listed(plan.groups, managed)
    actions = sync_mod.reconcile(plan, managed, only_area_id=only_area_id)

    results = None
    if apply:
        results = await sync_mod.apply_actions(login, plan, actions, managed)
        await store.async_save({"rooms": managed})

    response = _response(plan, actions, results, managed, dry_run=not apply)
    if auto and all(a.kind == sync_mod.UNCHANGED for a in actions):
        return response
    details = _details(actions, results, scope=scope, applied=apply)
    _publish(hass, entry, _short_summary(actions, applied=apply, scope=scope), details)
    _log_to_activity(hass, entry, details)
    return response


async def remove_managed_rooms(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, str]:
    """Undo every room this integration manages in Alexa. Returns name -> result."""
    store = _store(hass, entry)
    data = await store.async_load() or {}
    managed: dict[str, dict] = dict(data.get("rooms", {}))
    if not managed:
        return {}
    try:
        login = ac.get_login_obj(hass, entry.data.get(CONF_EMAIL))
        groups = await ac.get_groups(login)
    except ac.AlexaClientError as err:
        raise HomeAssistantError(str(err)) from err
    _check_groups_listed(groups, managed)
    plan = sync_mod.SyncPlan(rooms=[], existing_group_names=[], total_exposed=0,
                             total_in_area=0, groups=groups)
    actions = sync_mod.teardown(groups, managed)
    results = await sync_mod.apply_actions(login, plan, actions, managed)
    await store.async_save({"rooms": managed})
    return results


def _check_groups_listed(groups, managed: dict[str, dict]) -> None:
    """Refuse to act on an empty group listing while we manage rooms.

    An empty or unrecognised response would otherwise read as "every managed room
    is gone": a sync would recreate them all and the record would be lost.
    """
    if managed and not groups:
        raise HomeAssistantError(
            "Alexa returned no groups, but this integration manages "
            f"{len(managed)} group(s); not changing anything. Try again. If you really "
            "deleted every group in the Alexa app, remove and re-add this integration "
            "to clear its record."
        )


async def forget_managed_rooms(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the saved record of managed rooms. Alexa itself is not touched."""
    await _store(hass, entry).async_remove()


def _response(plan, actions, results, managed, *, dry_run: bool) -> dict[str, Any]:
    response: dict[str, Any] = {
        "dry_run": dry_run,
        "total_exposed": plan.total_exposed,
        "total_in_area": plan.total_in_area,
        "existing_groups": plan.existing_group_names,
        "actions": [
            {
                "area_id": a.area_id,
                "room_name": a.room_name,
                "action": a.kind,
                "summary": a.describe(),
            }
            for a in actions
        ],
        "rooms": [
            {
                "area_id": r.area_id,
                "room_name": r.room_name,
                "matched": r.matched,
                "unmatched": r.unmatched,
            }
            for r in plan.rooms
        ],
        "managed_rooms": managed,
    }
    if results is not None:
        response["results"] = results
    return response


def _scope(hass: HomeAssistant, only_area_id: str | None) -> str:
    """'Whole house', or 'One area (Kitchen)', matching the button names."""
    if not only_area_id:
        return "Whole house"
    area = ar.async_get(hass).async_get_area(only_area_id)
    return f"One area ({area.name if area else only_area_id})"


def _short_summary(actions, *, applied: bool, scope: str = "Whole house") -> str:
    counts: dict[str, int] = {}
    for a in actions:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    order = [(sync_mod.CREATE, "create"), (sync_mod.UPDATE, "update"),
             (sync_mod.DELETE, "delete"), (sync_mod.UNCHANGED, "unchanged")]
    bits = [f"{counts[k]} {label}" for k, label in order if counts.get(k)]
    head = f"{scope} – {'synced' if applied else 'preview'}"
    return f"{head}: {', '.join(bits) or 'nothing to do'}. See Activity."


def _details(actions, results, *, scope: str, applied: bool) -> dict[str, Any]:
    """The per-group outcome, kept as sensor attributes so it survives in History
    after the run."""
    if results is None:
        groups = {a.room_name: a.describe() for a in actions}
    else:
        groups = dict(results)
    return {
        "scope": scope,
        "mode": "sync" if applied else "preview",
        "ran_at": dt_util.utcnow().isoformat(),
        "groups": groups,
    }


def _log_to_activity(hass: HomeAssistant, entry: ConfigEntry, details: dict[str, Any]) -> None:
    """Write the per-group outcome to the logbook, so the device's Activity panel
    shows what each run changed."""
    try:
        from homeassistant.components.logbook import async_log_entry  # noqa: PLC0415
    except ImportError:
        return
    if details.get("error"):
        body = f"didn't run: {details['error']}"
        entity_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_last_result"
        )
        async_log_entry(hass, details["scope"], body, domain=DOMAIN, entity_id=entity_id)
        return
    groups = details["groups"]
    # Skips stay in: "no matched devices" is worth seeing, not the same as matching.
    notable = {
        name: what for name, what in groups.items()
        if not str(what).startswith("unchanged")
    }
    if notable:
        # " | " between groups: a group's own text uses ";" between added and removed.
        body = " | ".join(f"{name}: {what}" for name, what in notable.items())
    elif groups:
        body = f"all {len(groups)} group(s) already matched"
    else:
        body = "nothing to sync"
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_result"
    )
    async_log_entry(
        hass,
        f"{details['scope']} – {details['mode']}",
        body,
        domain=DOMAIN,
        entity_id=entity_id,
    )


def _publish(
    hass: HomeAssistant, entry: ConfigEntry, text: str, details: dict[str, Any]
) -> None:
    hass.data.setdefault(DOMAIN, {}).setdefault("_last_result", {})[entry.entry_id] = (
        text,
        details,
    )
    async_dispatcher_send(
        hass, SIGNAL_RESULT.format(entry_id=entry.entry_id), text, details
    )


def last_result(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[str, dict[str, Any]] | None:
    return hass.data.get(DOMAIN, {}).get("_last_result", {}).get(entry.entry_id)
