"""Config & options flow for Alexa Room Sync."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import AreaSelector, AreaSelectorConfig

from . import alexa_client as ac
from .const import (
    CONF_AREA_NAME_OVERRIDES,
    CONF_CONFIRMED,
    CONF_DRY_RUN,
    CONF_EMAIL,
    CONF_EXCLUDED_AREAS,
    CONF_INCLUDE_CHILD_LOCKS,
    CONF_INCLUDE_CLIMATE,
    CONF_ROOM_PREFIX,
    DEFAULT_DRY_RUN,
    DEFAULT_INCLUDE_CHILD_LOCKS,
    DEFAULT_INCLUDE_CLIMATE,
    DEFAULT_ROOM_PREFIX,
    DOMAIN,
)


class AlexaRoomSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Alexa Room Sync."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        emails = ac.list_configured_emails(self.hass)
        if not emails:
            return self.async_abort(reason="no_alexa_media")

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            await self.async_set_unique_id(f"{DOMAIN}:{email}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Alexa Room Sync ({email})",
                data={CONF_EMAIL: email},
                options={CONF_DRY_RUN: DEFAULT_DRY_RUN},
            )

        schema = vol.Schema(
            {vol.Required(CONF_EMAIL, default=emails[0]): vol.In(emails)}
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AlexaRoomSyncOptionsFlow(config_entry)


class AlexaRoomSyncOptionsFlow(OptionsFlow):
    """Options: dry-run, naming, and the write-confirmation gate."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self._entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_DRY_RUN,
                    default=opts.get(CONF_DRY_RUN, DEFAULT_DRY_RUN),
                ): bool,
                vol.Optional(
                    CONF_CONFIRMED,
                    default=opts.get(CONF_CONFIRMED, False),
                ): bool,
                vol.Optional(
                    CONF_ROOM_PREFIX,
                    default=opts.get(CONF_ROOM_PREFIX, DEFAULT_ROOM_PREFIX),
                ): str,
                vol.Optional(
                    CONF_INCLUDE_CLIMATE,
                    default=opts.get(CONF_INCLUDE_CLIMATE, DEFAULT_INCLUDE_CLIMATE),
                ): bool,
                vol.Optional(
                    CONF_INCLUDE_CHILD_LOCKS,
                    default=opts.get(
                        CONF_INCLUDE_CHILD_LOCKS, DEFAULT_INCLUDE_CHILD_LOCKS
                    ),
                ): bool,
                vol.Optional(
                    CONF_EXCLUDED_AREAS,
                    default=opts.get(CONF_EXCLUDED_AREAS, []),
                ): AreaSelector(AreaSelectorConfig(multiple=True)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
