"""Coordinator : lit les prix, calcule le planning et pilote les ballons."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_PRICE_SENSOR,
    CONF_MAX_POWER_SENSOR,
    CONF_BOILER_ENTITY,
    CONF_BOILER_POWER,
    CONF_BOILER_DURATION,
    PRICE_ATTR,
    PRICE_TIMESTAMP,
    PRICE_VALUE,
)

_LOGGER = logging.getLogger(__name__)


class EnergyPlannerCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=None,
        )
        self._unsub_midnight: Any = None
        self._unsub_transitions: list[Any] = []
        self._slots: list[dict] = []
        self._boiler_power_by_slot: dict[float, float] = {}
        self._custom_events: list[dict] = []
        self._schedule_midnight_refresh()
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._async_refresh_on_start)

    @callback
    def _async_refresh_on_start(self, _: Event) -> None:
        self.hass.async_create_task(self.async_refresh())

    def _schedule_midnight_refresh(self) -> None:
        @callback
        async def _midnight_refresh(_now: datetime) -> None:
            await self.async_refresh()

        self._unsub_midnight = async_track_time_change(
            self.hass, _midnight_refresh, hour=0, minute=5, second=0
        )

    def _schedule_transitions(self, transition_timestamps: list[float]) -> None:
        for unsub in self._unsub_transitions:
            unsub()
        self._unsub_transitions.clear()

        now_ts = dt_util.now().timestamp()

        @callback
        def _on_transition(_dt: datetime) -> None:
            self.hass.async_create_task(self.async_refresh())

        for ts in transition_timestamps:
            if ts > now_ts:
                point = dt_util.utc_from_timestamp(ts).astimezone(dt_util.DEFAULT_TIME_ZONE)
                self._unsub_transitions.append(
                    async_track_point_in_time(self.hass, _on_transition, point)
                )

    def cancel_scheduled_refresh(self) -> None:
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None
        for unsub in self._unsub_transitions:
            unsub()
        self._unsub_transitions.clear()

    async def _async_update_data(self) -> dict:
        price_sensor_id = self.config_entry.data[CONF_PRICE_SENSOR]
        state = self.hass.states.get(price_sensor_id)

        if state is None:
            _LOGGER.debug("Capteur prix pas encore disponible : %s", price_sensor_id)
            return {}

        prices: list[dict] = state.attributes.get(PRICE_ATTR, [])
        if not prices:
            _LOGGER.warning("Attribut '%s' vide sur %s", PRICE_ATTR, price_sensor_id)
            return {}

        boilers = [
            s.data for s in self.config_entry.subentries.values()
            if s.subentry_type == "boiler"
        ]

        schedule, transitions = self._compute_schedule(prices, boilers)
        self._schedule_transitions(transitions)

        return schedule

    def _compute_schedule(
        self, prices: list[dict], boilers: list[dict]
    ) -> tuple[dict[str, dict], list[float]]:
        if not prices:
            return {}, []

        now_ts = dt_util.now().timestamp()

        sorted_prices = sorted(prices, key=lambda p: p[PRICE_TIMESTAMP])
        slots: list[dict] = []
        for i, entry in enumerate(sorted_prices):
            start: float = entry[PRICE_TIMESTAMP]
            if i + 1 < len(sorted_prices):
                end: float = sorted_prices[i + 1][PRICE_TIMESTAMP]
            else:
                gap = (
                    sorted_prices[-1][PRICE_TIMESTAMP] - sorted_prices[-2][PRICE_TIMESTAMP]
                    if len(sorted_prices) > 1 else 3600
                )
                end = start + gap
            slots.append({
                "start": start,
                "end": end,
                "price": entry[PRICE_VALUE],
                "duration_min": (end - start) / 60,
            })

        self._slots = slots
        self._boiler_power_by_slot = {}

        schedule: dict[str, dict] = {}
        all_transitions: set[float] = set()

        for boiler in boilers:
            entity_id = boiler[CONF_BOILER_ENTITY]
            needed_min: float = boiler[CONF_BOILER_DURATION]
            power_w: float = boiler.get(CONF_BOILER_POWER, 0)

            selected: list[dict] = []
            covered_min = 0.0
            for slot in sorted(slots, key=lambda s: s["price"]):
                if covered_min >= needed_min:
                    break
                selected.append(slot)
                covered_min += slot["duration_min"]

            for s in selected:
                self._boiler_power_by_slot[s["start"]] = (
                    self._boiler_power_by_slot.get(s["start"], 0.0) + power_w
                )

            is_on = any(s["start"] <= now_ts < s["end"] for s in selected)
            scheduled_slots = self._merge_slots(
                [s for s in selected if s["end"] > now_ts],
                power_w,
            )

            schedule[entity_id] = {"is_on": is_on, "scheduled_slots": scheduled_slots}

            for s in selected:
                all_transitions.add(s["start"])
                all_transitions.add(s["end"])

        return schedule, sorted(all_transitions)

    @staticmethod
    def _merge_slots(slots: list[dict], power_w: float) -> list[dict]:
        if not slots:
            return []
        power_kw = power_w / 1000
        sorted_slots = sorted(slots, key=lambda s: s["start"])

        merged: list[dict] = []
        current = dict(sorted_slots[0])
        current["cost"] = current["price"] * power_kw * current["duration_min"] / 60

        for slot in sorted_slots[1:]:
            slot_cost = slot["price"] * power_kw * slot["duration_min"] / 60
            if slot["start"] == current["end"]:
                current["end"] = slot["end"]
                current["cost"] += slot_cost
            else:
                merged.append(current)
                current = dict(slot)
                current["cost"] = slot_cost
        merged.append(current)

        return [
            {
                "start": dt_util.utc_from_timestamp(s["start"]).isoformat(),
                "end": dt_util.utc_from_timestamp(s["end"]).isoformat(),
                "cost": round(s["cost"], 4),
            }
            for s in merged
        ]

    def _get_max_power(self) -> float | None:
        sensor_id = self.config_entry.data.get(CONF_MAX_POWER_SENSOR)
        if not sensor_id:
            return None
        state = self.hass.states.get(sensor_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _custom_power_in_slot(self, slot_start_ts: float, slot_end_ts: float) -> float:
        total = 0.0
        for event in self._custom_events:
            if event["start_ts"] < slot_end_ts and event["end_ts"] > slot_start_ts:
                total += event["power_w"]
        return total

    def find_cheapest_slot(
        self,
        power_w: float,
        duration_min: float,
        earliest_start_dt: datetime,
        latest_end_dt: datetime,
    ) -> dict | None:
        if not self._slots:
            return None

        max_power = self._get_max_power()
        earliest_ts = earliest_start_dt.timestamp()
        latest_ts = latest_end_dt.timestamp()
        power_kw = power_w / 1000

        window_slots = sorted(
            [s for s in self._slots if s["end"] > earliest_ts and s["start"] < latest_ts],
            key=lambda s: s["start"],
        )

        best: dict | None = None

        for i in range(len(window_slots)):
            accumulated_min = 0.0
            cost = 0.0
            valid = True

            for j in range(i, len(window_slots)):
                slot = window_slots[j]

                if j > i and slot["start"] != window_slots[j - 1]["end"]:
                    valid = False
                    break

                used_power = self._boiler_power_by_slot.get(slot["start"], 0.0)
                used_power += self._custom_power_in_slot(slot["start"], slot["end"])
                if max_power is not None and used_power + power_w > max_power:
                    valid = False
                    break

                remaining = duration_min - accumulated_min
                slot_contrib = min(slot["duration_min"], remaining)
                cost += slot["price"] * power_kw * slot_contrib / 60
                accumulated_min += slot["duration_min"]

                if accumulated_min >= duration_min:
                    actual_end_ts = window_slots[i]["start"] + duration_min * 60
                    if best is None or cost < best["cost"]:
                        best = {
                            "start_ts": window_slots[i]["start"],
                            "end_ts": actual_end_ts,
                            "cost": round(cost, 4),
                        }
                    break

            if not valid:
                continue

        return best

    @property
    def custom_events(self) -> list[dict]:
        return self._custom_events

    def add_custom_event(
        self,
        label: str,
        start_ts: float,
        end_ts: float,
        power_w: float,
        cost: float,
    ) -> None:
        self._custom_events.append({
            "label": label,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "power_w": power_w,
            "cost": round(cost, 4),
            "start_iso": dt_util.utc_from_timestamp(start_ts).isoformat(),
            "end_iso": dt_util.utc_from_timestamp(end_ts).isoformat(),
        })
        self.async_update_listeners()
