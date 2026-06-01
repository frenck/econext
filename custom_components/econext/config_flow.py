"""Config flow for ecoNEXT integration."""

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import EconextConnectionError, EconextApi
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_COOLING_CIRCUIT,
    CONF_COOLING_ENABLED,
    CONF_DEW_POINT_ENTITY,
    CONF_OEM_FLOW_ENTITY,
    CONF_THERMOSTAT_ENTITY,
    DEFAULT_COOLING_CIRCUIT,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


class EconextConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ecoNEXT."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return EconextOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await self._async_validate_input(user_input)
            except EconextConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Set unique ID based on device UID
                await self.async_set_unique_id(info["uid"])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info["name"],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle reconfiguration of the integration."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            # Split into data (connection) and options (thermostat)
            thermostat_entity = user_input.pop(CONF_THERMOSTAT_ENTITY, None)

            try:
                await self._async_validate_input(user_input)
            except EconextConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Save thermostat entity in options
                new_options = dict(reconfigure_entry.options)
                if thermostat_entity:
                    new_options[CONF_THERMOSTAT_ENTITY] = thermostat_entity
                elif CONF_THERMOSTAT_ENTITY in new_options:
                    del new_options[CONF_THERMOSTAT_ENTITY]
                self.hass.config_entries.async_update_entry(
                    reconfigure_entry, options=new_options,
                )

                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data_updates=user_input,
                )

        # Pre-fill with current values
        current_data = reconfigure_entry.data
        current_options = reconfigure_entry.options
        current_entity = current_options.get(CONF_THERMOSTAT_ENTITY)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=current_data.get(CONF_HOST, "")): str,
                    vol.Optional(CONF_PORT, default=current_data.get(CONF_PORT, DEFAULT_PORT)): int,
                    vol.Optional(
                        CONF_THERMOSTAT_ENTITY,
                        description={"suggested_value": current_entity} if current_entity else None,
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor", device_class="temperature"),
                    ),
                }
            ),
            errors=errors,
        )

    async def _async_validate_input(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate the user input and return device info."""
        session = async_get_clientsession(self.hass)
        api = EconextApi(
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            session=session,
        )

        return await api.async_test_connection()


class EconextOptionsFlow(OptionsFlow):
    """Handle options flow for ecoNEXT."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # Remove empty strings so clearing the entity selector actually clears it,
            # but keep the booleans (False is meaningful).
            clean = {
                k: v
                for k, v in user_input.items()
                if v != "" and v is not None
            }
            return self.async_create_entry(title="", data=clean)

        current = self._config_entry.options
        current_entity = current.get(CONF_THERMOSTAT_ENTITY)
        current_dew = current.get(CONF_DEW_POINT_ENTITY)
        current_oem = current.get(CONF_OEM_FLOW_ENTITY)
        # Only circuits 1-3 are supported cooling targets; if an older entry
        # has a higher value stored, fall back to the default for display so
        # the NumberSelector doesn't reject it.
        current_circuit = current.get(CONF_COOLING_CIRCUIT, DEFAULT_COOLING_CIRCUIT)
        if not 1 <= int(current_circuit) <= 3:
            current_circuit = DEFAULT_COOLING_CIRCUIT
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_THERMOSTAT_ENTITY,
                        description={"suggested_value": current_entity} if current_entity else None,
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor", device_class="temperature"),
                    ),
                    vol.Optional(
                        CONF_COOLING_ENABLED,
                        default=current.get(CONF_COOLING_ENABLED, False),
                    ): bool,
                    vol.Optional(
                        CONF_DEW_POINT_ENTITY,
                        description={"suggested_value": current_dew} if current_dew else None,
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor", device_class="temperature"),
                    ),
                    vol.Optional(
                        CONF_OEM_FLOW_ENTITY,
                        description={"suggested_value": current_oem} if current_oem else None,
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor", device_class="temperature"),
                    ),
                    vol.Optional(
                        CONF_COOLING_CIRCUIT,
                        default=current_circuit,
                    ): NumberSelector(
                        NumberSelectorConfig(min=1, max=3, step=1, mode=NumberSelectorMode.BOX),
                    ),
                }
            ),
        )
