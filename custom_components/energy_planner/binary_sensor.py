"""Binary sensors indiquant si chaque ballon doit être en chauffe."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_BOILER_ENTITY
from .coordinator import EnergyPlannerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EnergyPlannerCoordinator = hass.data[DOMAIN][entry.entry_id]
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type == "boiler":
            async_add_entities(
                [BoilerBinarySensor(coordinator, subentry_id, subentry.data[CONF_BOILER_ENTITY])],
                config_subentry_id=subentry_id,
            )


class BoilerBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: EnergyPlannerCoordinator,
        subentry_id: str,
        boiler_entity_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._boiler_entity_id = boiler_entity_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{subentry_id}_binary"
        self._attr_name = self._resolve_name(coordinator.hass, boiler_entity_id)

    @staticmethod
    def _resolve_name(hass, entity_id: str) -> str:
        registry = er.async_get(hass)
        entry = registry.async_get(entity_id)
        if entry and (entry.name or entry.original_name):
            return entry.name or entry.original_name
        state = hass.states.get(entity_id)
        return state.name if state else entity_id

    @property
    def is_on(self) -> bool:
        if not self.coordinator.data:
            return False
        return self.coordinator.data.get(self._boiler_entity_id, {}).get("is_on", False)

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        slots = self.coordinator.data.get(self._boiler_entity_id, {}).get("scheduled_slots", [])
        return {"scheduled_slots": slots}
