"""A 'Last result' line next to the buttons, pointing at the notification."""

from __future__ import annotations

from homeassistant.components.sensor import RestoreSensor
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import runner
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([LastResultSensor(entry)])


class LastResultSensor(RestoreSensor):
    """Short summary of the last Preview or Sync, e.g. 'Whole house – preview: 19 create, 1 unchanged.'"""

    _attr_has_entity_name = True
    _attr_translation_key = "last_result"
    _attr_icon = "mdi:bell-outline"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_last_result"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Alexa Room Sync",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        current = runner.last_result(self.hass, self._entry)
        if current:
            self._attr_native_value, self._attr_extra_state_attributes = current
        else:
            last_data = await self.async_get_last_sensor_data()
            last_state = await self.async_get_last_state()
            self._attr_native_value = (
                last_data.native_value if last_data else None
            ) or "Press a Preview button; results appear in Notifications (the bell)."
            if last_state:
                self._attr_extra_state_attributes = {
                    k: v
                    for k, v in last_state.attributes.items()
                    if k in ("scope", "mode", "ran_at", "groups")
                }
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                runner.SIGNAL_RESULT.format(entry_id=self._entry.entry_id),
                self._update,
            )
        )

    @callback
    def _update(self, text: str, details: dict) -> None:
        # ran_at changes every run, so History records each run even when the
        # one-line summary is the same as last time.
        self._attr_native_value = text
        self._attr_extra_state_attributes = details
        self.async_write_ha_state()
