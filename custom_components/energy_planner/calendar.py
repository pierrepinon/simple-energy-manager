"""Calendrier unique de planification des ballons d'eau chaude."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnergyPlannerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EnergyPlannerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EnergyPlannerCalendar(coordinator)])


class EnergyPlannerCalendar(CoordinatorEntity, CalendarEntity):
    def __init__(self, coordinator: EnergyPlannerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Planning Energy Planner"
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_calendar"

    def _friendly_name(self, entity_id: str) -> str:
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if entry and (entry.name or entry.original_name):
            return entry.name or entry.original_name
        state = self.hass.states.get(entity_id)
        return state.name if state else entity_id

    def _get_calendar_events(self) -> list[CalendarEvent]:
        events = []
        if self.coordinator.data:
            for entity_id, info in self.coordinator.data.items():
                name = self._friendly_name(entity_id)
                for slot in info.get("scheduled_slots", []):
                    start = datetime.fromisoformat(slot["start"])
                    end = datetime.fromisoformat(slot["end"])
                    cost = slot.get("cost", 0)
                    events.append(CalendarEvent(
                        start=start,
                        end=end,
                        summary=f"Chauffe — {name}",
                        description=f"Coût estimé : {cost:+.4f} €",
                    ))
        for ev in self.coordinator.custom_events:
            start = datetime.fromisoformat(ev["start_iso"])
            end = datetime.fromisoformat(ev["end_iso"])
            events.append(CalendarEvent(
                start=start,
                end=end,
                summary=ev["label"],
                description=f"Coût estimé : {ev['cost']:+.4f} €",
            ))
        return sorted(events, key=lambda e: e.start)

    @property
    def event(self) -> CalendarEvent | None:
        events = self._get_calendar_events()
        if not events:
            return None
        now = datetime.now(tz=events[0].start.tzinfo)
        for ev in events:
            if ev.start <= now < ev.end:
                return ev
        return events[0]

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        return [
            ev for ev in self._get_calendar_events()
            if ev.start < end_date and ev.end > start_date
        ]
