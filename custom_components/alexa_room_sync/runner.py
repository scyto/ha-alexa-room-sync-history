"""Run a room sync end to end: plan, reconcile, apply, remember, report.

Shared by the services and the buttons so both behave the same way.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

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
    notify: bool = False,
) -> dict[str, Any]:
    """Plan and (if ``apply``) carry out the sync. Returns the service response."""
    try:
        login = ac.get_login_obj(hass, entry.data.get(CONF_EMAIL))
        plan = await sync_mod.build_plan(
            hass, login, dict(entry.options), only_area_id=only_area_id
        )
    except ac.AlexaClientError as err:
        if notify:
            persistent_notification.async_create(
                hass, f"Sync didn't run: {err}", "Alexa Room Sync", NOTIFICATION_ID
            )
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
    scope = _scope(hass, only_area_id)
    _publish(hass, entry, _short_summary(actions, applied=apply, scope=scope))
    if notify:
        persistent_notification.async_create(
            hass, _notification_text(actions, results, scope), "Alexa Room Sync", NOTIFICATION_ID
        )
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


def _notification_text(actions, results, scope: str = "Whole house") -> str:
    if results is None:
        lines = [f"**{scope} – preview. Nothing was changed in Alexa.**", ""]
        lines += [f"- {a.room_name}: {a.describe()}" for a in actions]
    else:
        lines = [f"**{scope} – sync finished.**", ""]
        lines += [f"- {name}: {res}" for name, res in results.items()]
    changes = [a for a in actions if a.kind not in (sync_mod.UNCHANGED, sync_mod.SKIP)]
    if results is None and not changes:
        lines.append("\nEverything already matches.")
    return "\n".join(lines)


def _short_summary(actions, *, applied: bool, scope: str = "Whole house") -> str:
    counts: dict[str, int] = {}
    for a in actions:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    order = [(sync_mod.CREATE, "create"), (sync_mod.UPDATE, "update"),
             (sync_mod.DELETE, "delete"), (sync_mod.UNCHANGED, "unchanged")]
    bits = [f"{counts[k]} {label}" for k, label in order if counts.get(k)]
    head = f"{scope} – {'synced' if applied else 'preview'}"
    return f"{head}: {', '.join(bits) or 'nothing to do'}. Details in Notifications."


def _publish(hass: HomeAssistant, entry: ConfigEntry, text: str) -> None:
    hass.data.setdefault(DOMAIN, {}).setdefault("_last_result", {})[entry.entry_id] = text
    async_dispatcher_send(hass, SIGNAL_RESULT.format(entry_id=entry.entry_id), text)


def last_result(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    return hass.data.get(DOMAIN, {}).get("_last_result", {}).get(entry.entry_id)
