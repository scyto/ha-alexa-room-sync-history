"""Optional auto-sync: run a whole-house sync after areas, floors, devices or
entities change (debounced), and hourly as a fallback."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from . import runner
from .const import (
    AUTO_SYNC_DELAY_SECONDS,
    AUTO_SYNC_INTERVAL_HOURS,
    CONF_CONFIRMED,
    CONF_DRY_RUN,
    DEFAULT_DRY_RUN,
)

_LOGGER = logging.getLogger(__name__)

# Exposing an entity to Alexa is stored in the entity registry, so it fires this too.
_EVENTS = (
    ar.EVENT_AREA_REGISTRY_UPDATED,
    fr.EVENT_FLOOR_REGISTRY_UPDATED,
    dr.EVENT_DEVICE_REGISTRY_UPDATED,
    er.EVENT_ENTITY_REGISTRY_UPDATED,
)


def writes_allowed(options: dict) -> bool:
    """Auto-sync writes only under the same gate as the Sync buttons."""
    return not options.get(CONF_DRY_RUN, DEFAULT_DRY_RUN) and bool(
        options.get(CONF_CONFIRMED)
    )


@callback
def async_start(hass: HomeAssistant, entry: ConfigEntry) -> CALLBACK_TYPE:
    """Start listening; returns the function that stops it."""
    unsubs: list[CALLBACK_TYPE] = []
    pending: list[CALLBACK_TYPE] = []

    async def _run(_now=None) -> None:
        # The hourly run also covers a debounced one still waiting: cancel it, or it
        # fires a second sync and outlives _stop. Cancelling a fired timer is a no-op.
        for cancel in pending:
            cancel()
        pending.clear()
        if not writes_allowed(dict(entry.options)):
            return
        try:
            await runner.run_sync(hass, entry, apply=True, auto=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Alexa Room Sync auto-sync didn't run: %s", err)

    @callback
    def _schedule(_event: Event | None = None) -> None:
        # Restart the wait on every change, so a burst of edits is one sync.
        for cancel in pending:
            cancel()
        pending.clear()
        pending.append(async_call_later(hass, AUTO_SYNC_DELAY_SECONDS, _run))

    for event_type in _EVENTS:
        unsubs.append(hass.bus.async_listen(event_type, _schedule))
    unsubs.append(
        async_track_time_interval(hass, _run, timedelta(hours=AUTO_SYNC_INTERVAL_HOURS))
    )
    if hass.is_running:
        _schedule()
    else:
        unsubs.append(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _schedule)
        )

    @callback
    def _stop() -> None:
        for cancel in pending + unsubs:
            cancel()
        pending.clear()
        unsubs.clear()

    return _stop
