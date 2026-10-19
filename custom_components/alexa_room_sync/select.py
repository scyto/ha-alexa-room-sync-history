"""'Area to sync' picker on the device page, used by the per-area buttons.

It lists only areas a full sync would give a room, so skipped areas and areas
with nothing exposed to Alexa don't clutter it.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .sync import syncable_area_ids


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AreaToSyncSelect(entry)])


def selected_area_id(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The area_id currently picked in the select, if any."""
    return hass.data.get(DOMAIN, {}).get("_selected_area", {}).get(entry.entry_id)


class AreaToSyncSelect(SelectEntity, RestoreEntity):
    """Pick the HA area the 'selected area' buttons act on."""

    _attr_has_entity_name = True
    _attr_translation_key = "area_to_sync"
    _attr_icon = "mdi:texture-box"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_area_to_sync"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Alexa Room Sync",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._attr_options = []
        self._attr_current_option = None
        self._area_id: str | None = None  # tracked by id so a rename keeps the pick

    async def async_added_to_hass(self) -> None:
        self._refresh_options()
        last = await self.async_get_last_state()
        if last and last.state in self._attr_options:
            self._set(last.state)
        # Area renames, entity area moves and Alexa exposure changes all land here.
        for event in (ar.EVENT_AREA_REGISTRY_UPDATED, er.EVENT_ENTITY_REGISTRY_UPDATED):
            self.async_on_remove(self.hass.bus.async_listen(event, self._areas_changed))

    @callback
    def _areas_changed(self, _event: Event) -> None:
        before = (self._attr_options, self._attr_current_option)
        self._refresh_options()
        area = ar.async_get(self.hass).async_get_area(self._area_id) if self._area_id else None
        self._set(area.name if area and area.name in self._attr_options else None)
        if (self._attr_options, self._attr_current_option) != before:
            self.async_write_ha_state()

    def _refresh_options(self) -> None:
        wanted = syncable_area_ids(self.hass, dict(self._entry.options))
        names = [
            a.name for a in ar.async_get(self.hass).async_list_areas() if a.id in wanted
        ]
        self._attr_options = sorted(names, key=str.lower)

    def _set(self, name: str | None) -> None:
        self._attr_current_option = name
        area = ar.async_get(self.hass).async_get_area_by_name(name) if name else None
        self._area_id = area.id if area else None
        self.hass.data.setdefault(DOMAIN, {}).setdefault("_selected_area", {})[
            self._entry.entry_id
        ] = self._area_id

    async def async_select_option(self, option: str) -> None:
        self._set(option)
        self.async_write_ha_state()
