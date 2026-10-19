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
from homeassistant.helpers.storage import Store

from . import alexa_client as ac
from . import sync as sync_mod
from .const import CONF_EMAIL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
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
    actions = sync_mod.reconcile(plan, managed, only_area_id=only_area_id)

    results = None
    if apply:
        results = await sync_mod.apply_actions(login, plan, actions, managed)
        await store.async_save({"rooms": managed})

    response = _response(plan, actions, results, managed, dry_run=not apply)
    if notify:
        persistent_notification.async_create(
            hass, _notification_text(actions, results), "Alexa Room Sync", NOTIFICATION_ID
        )
    return response


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


def _notification_text(actions, results) -> str:
    if results is None:
        lines = ["**Preview only. Nothing was changed in Alexa.**", ""]
        lines += [f"- {a.room_name}: {a.describe()}" for a in actions]
    else:
        lines = ["**Sync finished.**", ""]
        lines += [f"- {name}: {res}" for name, res in results.items()]
    changes = [a for a in actions if a.kind not in (sync_mod.UNCHANGED, sync_mod.SKIP)]
    if results is None and not changes:
        lines.append("\nEverything already matches.")
    return "\n".join(lines)
