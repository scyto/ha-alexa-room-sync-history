"""Alexa Room Sync — mirror Home Assistant areas into Alexa rooms/groups.

Creates one Alexa group per HA area and assigns each Alexa-exposed HA device to the
room matching its HA area, using the *unofficial* Alexa API (the private endpoints
already spoken by the Alexa Media Player integration). No credentials are handled here —
the authenticated session is borrowed from alexa_media at runtime.

Safety: every service defaults to a read-only PREVIEW. Writes only happen when you call
``sync_area`` (one room) or ``sync`` (all rooms) with dry_run turned off in the options.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from . import alexa_client as ac
from . import diag as diag_mod
from . import sync as sync_mod
from .const import (
    ATTR_AREA_ID,
    CONF_CONFIRMED,
    CONF_DRY_RUN,
    CONF_EMAIL,
    DEFAULT_DRY_RUN,
    DOMAIN,
    SERVICE_DUMP_APPLIANCES,
    SERVICE_LIST_GROUPS,
    SERVICE_PREVIEW,
    SERVICE_PROBE_LISTS,
    SERVICE_SYNC,
    SERVICE_SYNC_AREA,
)

_LOGGER = logging.getLogger(__name__)

# Alexa's named lists become HA to-do entities (see todo.py). Kept in this integration
# rather than a second one because the authenticated session is already here.
PLATFORMS = [Platform.TODO]


def _login_for_entry(hass: HomeAssistant, entry: ConfigEntry):
    email = entry.data.get(CONF_EMAIL)
    try:
        return ac.get_login_obj(hass, email)
    except ac.AlexaClientError as err:
        raise HomeAssistantError(str(err)) from err


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Alexa Room Sync from a config entry and register services."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    def _options(local_entry: ConfigEntry) -> dict:
        return dict(local_entry.options)

    async def _handle_preview(call: ServiceCall) -> ServiceResponse:
        login = _login_for_entry(hass, entry)
        area_id = call.data.get(ATTR_AREA_ID)
        plan = await sync_mod.build_plan(hass, login, _options(entry), only_area_id=area_id)
        for line in plan.summary_lines():
            _LOGGER.info("[preview] %s", line)
        return _plan_to_response(plan, dry_run=True, results=None)

    async def _handle_list_groups(call: ServiceCall) -> ServiceResponse:
        login = _login_for_entry(hass, entry)
        groups = await ac.get_groups(login)
        return {
            "groups": [
                {
                    "name": g.name,
                    "group_id": g.group_id,
                    "device_count": len(g.appliance_ids),
                }
                for g in groups
            ]
        }

    async def _handle_probe_endpoints(call: ServiceCall) -> ServiceResponse:
        """Read-only: try candidate device-list endpoints, report which returns data."""
        login = _login_for_entry(hass, entry)
        results = await ac.probe_read_endpoints(login)
        for r in results:
            _LOGGER.info("[probe] %s -> type=%s hits=%s err=%s",
                         r["endpoint"], r["type"], r["entity_id_hits"], r["error"])
        return {"probes": results}

    async def _handle_dump_appliances(call: ServiceCall) -> ServiceResponse:
        """Read-only diagnostic: return a redacted structural summary of the raw
        /api/phoenix appliance payload so entity->appliance matching can be fixed."""
        login = _login_for_entry(hass, entry)
        raw = await ac.get_raw_appliance_dicts(login)
        sample_count = int(call.data.get("sample_count", 4))
        summary = diag_mod.summarize(raw, sample_count=sample_count)
        if not raw:
            summary["raw_phoenix_shape"] = await ac.get_raw_phoenix_shape(login)
        _LOGGER.info(
            "[dump_appliances] %d appliances, %d carry an embedded entity_id; "
            "entity_id locations: %s",
            summary["appliance_count"],
            summary["appliances_with_embedded_entity_id"],
            [loc["json_path"] for loc in summary["entity_id_locations"]],
        )
        return summary

    async def _run_sync(call: ServiceCall, area_id: str | None) -> ServiceResponse:
        login = _login_for_entry(hass, entry)
        options = _options(entry)
        dry_run = options.get(CONF_DRY_RUN, DEFAULT_DRY_RUN)
        # Per-call override lets an explicit apply happen without editing options,
        # but ONLY if the user has also confirmed writes in options.
        if call.data.get("apply") is True:
            if not options.get(CONF_CONFIRMED):
                raise HomeAssistantError(
                    "Refusing to write: enable 'I understand this writes to my Alexa "
                    "account' in the integration options before using apply:true."
                )
            dry_run = False

        plan = await sync_mod.build_plan(hass, login, options, only_area_id=area_id)
        for line in plan.summary_lines():
            _LOGGER.info("[sync%s] %s", "" if not area_id else f":{area_id}", line)

        if dry_run:
            _LOGGER.warning(
                "Alexa Room Sync is in DRY-RUN — no groups created. "
                "Set apply:true (with options confirmation) to write."
            )
            return _plan_to_response(plan, dry_run=True, results=None)

        results = await sync_mod.apply_plan(hass, login, plan)
        for name, res in results.items():
            _LOGGER.info("[applied] %s: %s", name, res)
        return _plan_to_response(plan, dry_run=False, results=results)

    # Real coroutine functions, not lambdas: HA only awaits a handler it can
    # recognise as async, so a lambda returning a coroutine never runs.
    async def _handle_sync_area(call: ServiceCall) -> ServiceResponse:
        return await _run_sync(call, call.data[ATTR_AREA_ID])

    async def _handle_sync(call: ServiceCall) -> ServiceResponse:
        return await _run_sync(call, None)

    hass.services.async_register(
        DOMAIN,
        SERVICE_PREVIEW,
        _handle_preview,
        schema=vol.Schema({vol.Optional(ATTR_AREA_ID): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_GROUPS,
        _handle_list_groups,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_AREA,
        _handle_sync_area,
        schema=vol.Schema(
            {
                vol.Required(ATTR_AREA_ID): cv.string,
                vol.Optional("apply"): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC,
        _handle_sync,
        schema=vol.Schema({vol.Optional("apply"): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DUMP_APPLIANCES,
        _handle_dump_appliances,
    )
    async def _handle_probe_lists(call: ServiceCall) -> ServiceResponse:
        """Read-only: which Alexa list endpoints still answer for this account?"""
        login = _login_for_entry(hass, entry)
        results = await ac.probe_lists(login)
        if call.data.get("raw"):
            results = await ac.probe_lists_raw(login)
        for r in results:
            _LOGGER.info(
                "[probe_lists] %s -> rows=%s names=%s err=%s",
                r["endpoint"], r.get("row_count"), r.get("list_names"), r.get("error"),
            )
        return {"probes": results}

    hass.services.async_register(
        DOMAIN,
        SERVICE_PROBE_LISTS,
        _handle_probe_lists,
        schema=vol.Schema({vol.Optional("raw"): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "probe_endpoints",
        _handle_probe_endpoints,
        schema=vol.Schema({vol.Optional("sample_count"): cv.positive_int}),
        supports_response=SupportsResponse.OPTIONAL,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _plan_to_response(plan, dry_run: bool, results) -> ServiceResponse:
    response: dict = {
        "dry_run": dry_run,
        "total_exposed": plan.total_exposed,
        "total_in_area": plan.total_in_area,
        "existing_groups": plan.existing_group_names,
        "rooms": [
            {
                "area_id": r.area_id,
                "room_name": r.room_name,
                "matched": r.matched,
                "unmatched": r.unmatched,
            }
            for r in plan.rooms
        ],
    }
    if results is not None:
        response["results"] = results
    return response


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    for service in (
        SERVICE_PREVIEW,
        SERVICE_SYNC_AREA,
        SERVICE_SYNC,
        SERVICE_LIST_GROUPS,
        SERVICE_DUMP_APPLIANCES,
        SERVICE_PROBE_LISTS,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
