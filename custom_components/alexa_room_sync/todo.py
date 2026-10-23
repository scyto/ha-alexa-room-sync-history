"""Alexa lists as Home Assistant to-do entities.

One `todo.*` entity per Alexa named list (Shopping List, To-do List, custom lists), read
and written over the session borrowed from alexa_media — no second Amazon login.

Being a real todo entity rather than a sensor is the point: HA's own to-do card, the
dashboard cards and voice assistants all speak this interface, so ticking an item off in
the sidebar removes it from what the Echo reads back.

⚠️ These endpoints are the Alexa app's private ones and Amazon has been retiring list
access. If they don't answer, this platform logs once and creates NO entities — an empty
integration is a better failure than a wall of unavailable cards. Run the
`alexa_room_sync.probe_lists` service to see what the account still serves.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from . import alexa_client as ac
from .const import CONF_EMAIL, DOMAIN, LIST_SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create one to-do entity per Alexa list, or none if lists aren't available."""
    try:
        login = ac.get_login_obj(hass, entry.data.get(CONF_EMAIL))
        lists = await ac.get_lists(login)
    except ac.AlexaClientError as err:
        # Amazon has stopped serving lists on many accounts, so this is expected
        # rather than a fault: say it once per HA run, at info level.
        logged = hass.data.setdefault(DOMAIN, {}).setdefault("_lists_unavailable_logged", set())
        if entry.entry_id not in logged:
            logged.add(entry.entry_id)
            _LOGGER.info(
                "Alexa lists unavailable (%s) — no to-do entities created. "
                "Call alexa_room_sync.probe_lists to see what the account still serves.",
                err,
            )
        return

    if not lists:
        _LOGGER.warning(
            "Alexa returned no named lists — no to-do entities created. Amazon may have "
            "retired list access for this account; try alexa_room_sync.probe_lists."
        )
        return

    coordinator = AlexaListCoordinator(hass, entry, login, lists)
    await coordinator.async_config_entry_first_refresh()
    async_add_entities(AlexaTodoList(coordinator, lst) for lst in lists)
    _LOGGER.info("Alexa lists: %s", ", ".join(lst.name for lst in lists))


class AlexaListCoordinator(DataUpdateCoordinator[dict[str, list[ac.AlexaListItem]]]):
    """Polls every list once per cycle and keeps the raw item documents.

    The raw documents matter: Alexa's write endpoints want their own item back, version
    field included, so we can't reconstruct one from a TodoItem alone.
    """

    def __init__(self, hass, entry, login, lists: list[ac.AlexaList]) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} lists",
            update_interval=timedelta(seconds=LIST_SCAN_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.login = login
        self.lists = lists

    async def _async_update_data(self) -> dict[str, list[ac.AlexaListItem]]:
        # Re-borrow the session each cycle: alexa_media re-authenticates periodically and
        # holding a stale login object is how this dies quietly after a token refresh.
        try:
            self.login = ac.get_login_obj(self.hass, self.entry.data.get(CONF_EMAIL))
        except ac.AlexaClientError as err:
            _LOGGER.debug("Keeping previous Alexa login (%s)", err)
        out: dict[str, list[ac.AlexaListItem]] = {}
        for lst in self.lists:
            try:
                out[lst.list_id] = await ac.get_list_items(self.login, lst.list_id)
            except ac.AlexaClientError as err:
                _LOGGER.warning("Alexa list '%s' failed to load: %s", lst.name, err)
                out[lst.list_id] = (self.data or {}).get(lst.list_id, [])
        return out


class AlexaTodoList(CoordinatorEntity[AlexaListCoordinator], TodoListEntity):
    """One Alexa named list."""

    _attr_has_entity_name = False
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, coordinator: AlexaListCoordinator, alexa_list: ac.AlexaList) -> None:
        super().__init__(coordinator)
        self._list = alexa_list
        self._attr_name = f"Alexa {alexa_list.name}"
        self._attr_unique_id = f"{DOMAIN}_list_{alexa_list.list_id}"
        self._attr_icon = (
            "mdi:cart-outline"
            if "shop" in (alexa_list.name or "").lower()
            or (alexa_list.type or "").upper().startswith("SHOPPING")
            else "mdi:format-list-checks"
        )

    @property
    def _items(self) -> list[ac.AlexaListItem]:
        return (self.coordinator.data or {}).get(self._list.list_id, [])

    @property
    def todo_items(self) -> list[TodoItem]:
        return [
            TodoItem(
                uid=item.item_id,
                summary=item.value,
                status=TodoItemStatus.COMPLETED
                if item.completed
                else TodoItemStatus.NEEDS_ACTION,
            )
            for item in self._items
        ]

    def _raw(self, uid: str) -> ac.AlexaListItem | None:
        return next((i for i in self._items if i.item_id == uid), None)

    async def async_create_todo_item(self, item: TodoItem) -> None:
        await ac.add_list_item(self.coordinator.login, self._list.list_id, item.summary or "")
        await self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        raw = self._raw(item.uid or "")
        if raw is None:
            return
        await ac.update_list_item(
            self.coordinator.login,
            raw,
            value=item.summary,
            completed=item.status == TodoItemStatus.COMPLETED,
        )
        await self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        for uid in uids:
            raw = self._raw(uid)
            if raw is not None:
                await ac.delete_list_item(self.coordinator.login, self._list.list_id, raw)
        await self.coordinator.async_request_refresh()
