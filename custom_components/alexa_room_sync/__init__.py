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
from . import autosync
from . import diag as diag_mod
from . import runner
from . import sync as sync_mod
from .const import (
    CONF_AUTO_SYNC,
    DEFAULT_AUTO_SYNC,
    ATTR_AREA_ID,
    CONF_CONFIRMED,
    CONF_DRY_RUN,
    CONF_EMAIL,
    DEFAULT_DRY_RUN,
    DOMAIN,
    SERVICE_DUMP_APPLIANCES,
    SERVICE_LIST_GROUPS,
    SERVICE_PREVIEW,
    SERVICE_PROBE_ENDPOINTS,
    SERVICE_PROBE_LISTS,
    SERVICE_REMOVE_MANAGED_ROOMS,
    SERVICE_SYNC,
    SERVICE_SYNC_AREA,
)

_LOGGER = logging.getLogger(__name__)

# Alexa's named lists become HA to-do entities (see todo.py). Kept in this integration
# rather than a second one because the authenticated session is already here.
PLATFORMS = [Platform.BUTTON, Platform.SELECT, Platform.SENSOR, Platform.TODO]


ATTR_CONFIG_ENTRY_ID = "config_entry_id"
_TARGET = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}


def _target_entry(hass: HomeAssistant, call: ServiceCall) -> ConfigEntry:
    """The account a service call is for. Required once there is more than one."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if wanted:
        for e in entries:
            if e.entry_id == wanted:
                return e
        raise HomeAssistantError(f"No loaded Alexa Room Sync entry '{wanted}'.")
    if len(entries) == 1:
        return entries[0]
    if not entries:
        raise HomeAssistantError("Alexa Room Sync isn't loaded.")
    raise HomeAssistantError(
        "More than one Alexa account is set up; pass config_entry_id to choose one."
    )


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
    if entry.options.get(CONF_AUTO_SYNC, DEFAULT_AUTO_SYNC):
        entry.async_on_unload(autosync.async_start(hass, entry))

    def _options(local_entry: ConfigEntry) -> dict:
        return dict(local_entry.options)

    async def _handle_preview(call: ServiceCall) -> ServiceResponse:
        entry = _target_entry(hass, call)
        return await runner.run_sync(
            hass, entry, only_area_id=call.data.get(ATTR_AREA_ID), apply=False
        )

    async def _handle_list_groups(call: ServiceCall) -> ServiceResponse:
        entry = _target_entry(hass, call)
        login = _login_for_entry(hass, entry)
        groups = await ac.get_groups(login)
        # Names are a nicety: if the device list fails, still list members by id.
        try:
            names = {a.appliance_id: a.friendly_name
                     for a in await ac.get_appliances(login) if a.friendly_name}
        except ac.AlexaClientError as err:
            _LOGGER.warning("list_groups: device names unavailable (%s)", err)
            names = {}
        return {
            "groups": [
                {
                    "name": g.name,
                    "group_id": g.group_id,
                    "device_count": len(g.appliance_ids),
                    "devices": [sync_mod.device_label(a, names) for a in g.appliance_ids],
                }
                for g in groups
            ]
        }

    async def _handle_probe_endpoints(call: ServiceCall) -> ServiceResponse:
        """Read-only: try candidate device-list endpoints, report which returns data."""
        entry = _target_entry(hass, call)
        login = _login_for_entry(hass, entry)
        results = await ac.probe_read_endpoints(login)
        for r in results:
            _LOGGER.info("[probe] %s -> type=%s hits=%s err=%s",
                         r["endpoint"], r["type"], r["entity_id_hits"], r["error"])
        return {"probes": results}

    async def _handle_dump_appliances(call: ServiceCall) -> ServiceResponse:
        """Read-only diagnostic: redacted summaries of what each device-list source
        returns (the GraphQL query the sync uses, and the legacy /api/phoenix), so
        entity->appliance matching problems can be traced to the right one."""
        entry = _target_entry(hass, call)
        login = _login_for_entry(hass, entry)
        sample_count = int(call.data.get("sample_count", 4))
        report: dict = {}
        for source in ("graphql", "phoenix"):
            try:
                raw = await ac.get_raw_appliance_dicts(login, source=source)
            except ac.AlexaClientError as err:
                report[source] = {"error": str(err)}
                continue
            summary = diag_mod.summarize(raw, sample_count=sample_count)
            if source == "phoenix" and not raw:
                # Extra requests; a failure here must not lose the other source.
                try:
                    summary["raw_phoenix_shape"] = await ac.get_raw_phoenix_shape(login)
                except ac.AlexaClientError as err:
                    summary["raw_phoenix_shape_error"] = str(err)
            report[source] = summary
            _LOGGER.info(
                "[dump_appliances:%s] %d appliances, %d carry an embedded entity_id",
                source,
                summary["appliance_count"],
                summary["appliances_with_embedded_entity_id"],
            )
        return report

    async def _run_sync(call: ServiceCall, area_id: str | None) -> ServiceResponse:
        entry = _target_entry(hass, call)
        options = _options(entry)
        apply = not options.get(CONF_DRY_RUN, DEFAULT_DRY_RUN)
        # Per-call override lets an explicit apply happen without editing options,
        # but ONLY if the user has also confirmed writes in options.
        if call.data.get("apply") is True:
            if not options.get(CONF_CONFIRMED):
                raise HomeAssistantError(
                    "Refusing to write: enable 'I understand this writes to my Alexa "
                    "account' in the integration options before using apply:true."
                )
            apply = True
        elif apply and not options.get(CONF_CONFIRMED):
            apply = False
        if not apply:
            _LOGGER.warning(
                "Alexa Room Sync is in DRY-RUN; nothing written. "
                "Set apply:true (with options confirmation) to write."
            )
        return await runner.run_sync(hass, entry, only_area_id=area_id, apply=apply)

    # Real coroutine functions, not lambdas: HA only awaits a handler it can
    # recognise as async, so a lambda returning a coroutine never runs.
    async def _handle_sync_area(call: ServiceCall) -> ServiceResponse:
        return await _run_sync(call, call.data[ATTR_AREA_ID])

    async def _handle_sync(call: ServiceCall) -> ServiceResponse:
        return await _run_sync(call, None)

    if hass.services.has_service(DOMAIN, SERVICE_PREVIEW):
        # Services are domain-wide and pick their account per call, so a second
        # account's entry only needs its platforms.
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

    hass.services.async_register(
        DOMAIN,
        SERVICE_PREVIEW,
        _handle_preview,
        schema=vol.Schema({vol.Optional(ATTR_AREA_ID): cv.string, **_TARGET}),
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
                **_TARGET,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC,
        _handle_sync,
        schema=vol.Schema({vol.Optional("apply"): cv.boolean, **_TARGET}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DUMP_APPLIANCES,
        _handle_dump_appliances,
        schema=vol.Schema({vol.Optional("sample_count"): cv.positive_int, **_TARGET}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    async def _handle_probe_lists(call: ServiceCall) -> ServiceResponse:
        """Read-only: which Alexa list endpoints still answer for this account?"""
        entry = _target_entry(hass, call)
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
        schema=vol.Schema({vol.Optional("raw"): cv.boolean, **_TARGET}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PROBE_ENDPOINTS,
        _handle_probe_endpoints,
        schema=vol.Schema({vol.Optional("sample_count"): cv.positive_int, **_TARGET}),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _handle_remove_managed_rooms(call: ServiceCall) -> ServiceResponse:
        entry = _target_entry(hass, call)
        if not call.data.get("confirm"):
            raise HomeAssistantError("Set confirm: true to remove the Alexa groups it manages.")
        return {"results": await runner.remove_managed_rooms(hass, entry)}

    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_MANAGED_ROOMS,
        _handle_remove_managed_rooms,
        schema=vol.Schema({vol.Required("confirm"): cv.boolean, **_TARGET}),
        supports_response=SupportsResponse.OPTIONAL,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    others = [
        e for e in hass.config_entries.async_loaded_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ]
    for service in () if others else (
        SERVICE_PREVIEW,
        SERVICE_SYNC_AREA,
        SERVICE_SYNC,
        SERVICE_LIST_GROUPS,
        SERVICE_DUMP_APPLIANCES,
        SERVICE_PROBE_LISTS,
        SERVICE_PROBE_ENDPOINTS,
        SERVICE_REMOVE_MANAGED_ROOMS,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Removing the integration leaves Alexa's rooms alone; only our record goes.

    Use Configure > Remove managed rooms first if you want the rooms gone too.
    """
    await runner.forget_managed_rooms(hass, entry)
