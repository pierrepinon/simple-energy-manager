"""Config flow : paramétrage de l'intégration Energy Planner."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    DOMAIN,
    CONF_PRICE_SENSOR,
    CONF_MAX_POWER_SENSOR,
    CONF_BOILER_ENTITY,
    CONF_BOILER_POWER,
    CONF_BOILER_DURATION,
)

_BOILER_SCHEMA = vol.Schema({
    vol.Required(CONF_BOILER_ENTITY): EntitySelector(
        EntitySelectorConfig(domain="water_heater")
    ),
    vol.Required(CONF_BOILER_POWER, default=2000): NumberSelector(
        NumberSelectorConfig(min=100, max=20000, step=100, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
    ),
    vol.Required(CONF_BOILER_DURATION, default=120): NumberSelector(
        NumberSelectorConfig(min=30, max=720, step=30, unit_of_measurement="min", mode=NumberSelectorMode.SLIDER)
    ),
})


class EnergyPlannerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @classmethod
    def async_get_supported_subentry_types(
        cls, _config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        return {"boiler": BoilerSubentryFlow}

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title="Energy Planner",
                data={
                    CONF_PRICE_SENSOR: user_input[CONF_PRICE_SENSOR],
                    CONF_MAX_POWER_SENSOR: user_input[CONF_MAX_POWER_SENSOR],
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_PRICE_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_MAX_POWER_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
            }),
        )


class BoilerSubentryFlow(config_entries.ConfigSubentryFlow):
    """Flux pour ajouter ou modifier un ballon d'eau chaude."""

    def _find_subentry(
        self,
    ) -> tuple[config_entries.ConfigEntry | None, str | None]:
        """Retrouve la config entry parente et l'ID de la subentry.

        _entry_id peut être l'ID de la config entry parente ou l'ID de la
        subentry selon la version de HA — on teste les deux.
        """
        # Cas 1 : _entry_id est l'ID de la config entry parente
        parent = self.hass.config_entries.async_get_entry(self._entry_id)
        if parent is not None:
            subentry_id = self.context.get("subentry_id")
            if subentry_id and subentry_id in parent.subentries:
                return parent, subentry_id

        # Cas 2 : _entry_id est l'ID de la subentry
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if self._entry_id in entry.subentries:
                return entry, self._entry_id

        return None, None

    def _entity_title(self, entity_id: str) -> str:
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if entry and (entry.name or entry.original_name):
            return entry.name or entry.original_name
        state = self.hass.states.get(entity_id)
        if state:
            return state.name
        return entity_id

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            entity_id = user_input[CONF_BOILER_ENTITY]
            return self.async_create_entry(
                title=self._entity_title(entity_id),
                data={
                    CONF_BOILER_ENTITY: entity_id,
                    CONF_BOILER_POWER: user_input[CONF_BOILER_POWER],
                    CONF_BOILER_DURATION: user_input[CONF_BOILER_DURATION],
                },
            )

        return self.async_show_form(step_id="user", data_schema=_BOILER_SCHEMA)

    async def async_step_reconfigure(self, user_input=None):
        config_entry, subentry_id = self._find_subentry()
        if config_entry is None:
            return self.async_abort(reason="entry_not_found")
        current = config_entry.subentries[subentry_id].data

        if user_input is not None:
            entity_id = user_input[CONF_BOILER_ENTITY]
            return self.async_update_and_abort(
                config_entry,
                subentry_id,
                title=self._entity_title(entity_id),
                data={
                    CONF_BOILER_ENTITY: entity_id,
                    CONF_BOILER_POWER: user_input[CONF_BOILER_POWER],
                    CONF_BOILER_DURATION: user_input[CONF_BOILER_DURATION],
                },
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_BOILER_ENTITY, default=current[CONF_BOILER_ENTITY]): EntitySelector(
                    EntitySelectorConfig(domain="water_heater")
                ),
                vol.Required(CONF_BOILER_POWER, default=current[CONF_BOILER_POWER]): NumberSelector(
                    NumberSelectorConfig(min=100, max=20000, step=100, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_BOILER_DURATION, default=current[CONF_BOILER_DURATION]): NumberSelector(
                    NumberSelectorConfig(min=0.5, max=12, step=0.5, unit_of_measurement="h", mode=NumberSelectorMode.SLIDER)
                ),
            }),
        )
