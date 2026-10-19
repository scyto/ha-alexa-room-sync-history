"""Preview / Sync buttons for the whole house and for the area picked in the select."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import runner
from .select import selected_area_id
from .const import CONF_CONFIRMED, CONF_DRY_RUN, DEFAULT_DRY_RUN, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(
        [PreviewButton(entry), SyncButton(entry), PreviewAreaButton(entry), SyncAreaButton(entry)]
    )


class _RoomSyncButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Alexa Room Sync",
            entry_type=DeviceEntryType.SERVICE,
        )


class PreviewButton(_RoomSyncButton):
    """Show what a full sync would do. Never writes."""

    _attr_icon = "mdi:eye-outline"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "preview")

    async def async_press(self) -> None:
        await runner.run_sync(self.hass, self._entry, apply=False)


class SyncButton(_RoomSyncButton):
    """Run a full sync. Still a preview while dry-run is on or writes aren't confirmed."""

    _attr_icon = "mdi:sync"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "sync")

    async def async_press(self) -> None:
        options = self._entry.options
        apply = bool(options.get(CONF_CONFIRMED)) and not options.get(
            CONF_DRY_RUN, DEFAULT_DRY_RUN
        )
        await runner.run_sync(self.hass, self._entry, apply=apply)


def _writes_enabled(entry: ConfigEntry) -> bool:
    options = entry.options
    return bool(options.get(CONF_CONFIRMED)) and not options.get(CONF_DRY_RUN, DEFAULT_DRY_RUN)


class _AreaButton(_RoomSyncButton):
    def _area(self) -> str:
        area_id = selected_area_id(self.hass, self._entry)
        if not area_id:
            raise HomeAssistantError("Pick an area in 'One area' first.")
        return area_id


class PreviewAreaButton(_AreaButton):
    """Show what syncing the selected area would change. Never writes."""

    _attr_icon = "mdi:eye-check-outline"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "preview_area")

    async def async_press(self) -> None:
        await runner.run_sync(
            self.hass, self._entry, only_area_id=self._area(), apply=False
        )


class SyncAreaButton(_AreaButton):
    """Sync just the selected area. Previews while writes aren't enabled."""

    _attr_icon = "mdi:sync"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "sync_area")

    async def async_press(self) -> None:
        await runner.run_sync(
            self.hass,
            self._entry,
            only_area_id=self._area(),
            apply=_writes_enabled(self._entry),
        )
