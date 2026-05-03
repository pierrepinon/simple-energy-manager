"""Energy Planner : optimise la planification des ballons d'eau chaude."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EnergyPlannerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["binary_sensor", "calendar"]
SERVICE_PLAN_DEVICE = "plan_device"

_SERVICE_SCHEMA = vol.Schema({
    vol.Required("duration"): vol.Coerce(float),
    vol.Required("power"): vol.Coerce(float),
    vol.Required("earliest_start"): str,
    vol.Required("latest_end"): str,
    vol.Optional("label", default="Appareil planifié"): str,
})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = EnergyPlannerCoordinator(hass, entry)
    await coordinator.async_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_on_entry_updated))

    if not hass.services.has_service(DOMAIN, SERVICE_PLAN_DEVICE):
        async def _handle_plan_device(call: ServiceCall) -> dict:
            coordinators: list[EnergyPlannerCoordinator] = list(hass.data[DOMAIN].values())
            if not coordinators:
                raise HomeAssistantError("Energy Planner n'est pas configuré")
            coord = coordinators[0]

            duration_min: float = call.data["duration"]
            power_w: float = call.data["power"]
            label: str = call.data["label"]

            earliest_start = dt_util.parse_datetime(call.data["earliest_start"])
            latest_end = dt_util.parse_datetime(call.data["latest_end"])

            if earliest_start is None or latest_end is None:
                raise HomeAssistantError("Format de date invalide (utilisez ISO 8601)")

            if earliest_start.tzinfo is None:
                earliest_start = dt_util.as_local(earliest_start)
            if latest_end.tzinfo is None:
                latest_end = dt_util.as_local(latest_end)

            now = dt_util.now()
            if earliest_start <= now:
                raise HomeAssistantError("earliest_start doit être dans le futur")
            if latest_end <= now:
                raise HomeAssistantError("latest_end doit être dans le futur")
            if latest_end <= earliest_start:
                raise HomeAssistantError("latest_end doit être après earliest_start")

            result = coord.find_cheapest_slot(power_w, duration_min, earliest_start, latest_end)
            if result is None:
                raise HomeAssistantError(
                    "Aucun créneau disponible dans la fenêtre spécifiée "
                    "(contrainte de puissance ou prix manquants)"
                )

            coord.add_custom_event(label, result["start_ts"], result["end_ts"], power_w, result["cost"])

            return {
                "start": dt_util.utc_from_timestamp(result["start_ts"]).isoformat(),
                "cost": result["cost"],
            }

        hass.services.async_register(
            DOMAIN,
            SERVICE_PLAN_DEVICE,
            _handle_plan_device,
            schema=_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    return True


async def _async_on_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: EnergyPlannerCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.cancel_scheduled_refresh()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_PLAN_DEVICE)
    return unload_ok
