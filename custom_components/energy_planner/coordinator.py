"""Coordinator : lit les prix, calcule le planning et pilote les ballons."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_PRICE_SENSOR,
    CONF_MAX_POWER_SENSOR,
    CONF_BOILER_ENTITY,
    CONF_BOILER_POWER,
    CONF_BOILER_DURATION,
    CONF_BOILER_LATEST,
    PRICE_ATTR,
    PRICE_TIMESTAMP,
    PRICE_VALUE,
)

_LOGGER = logging.getLogger(__name__)

_EVENTS_STORE_KEY = "energy_planner.custom_events"
_SCHEDULE_STORE_KEY = "energy_planner.schedule"
_STORE_VERSION = 1


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
        # raw plan: {entity_id: {"slots": [...], "power_w": float}}
        self._raw_schedule: dict[str, dict] = {}
        self._plan_ready: bool = False
        self._events_store: Store = Store(hass, _STORE_VERSION, _EVENTS_STORE_KEY)
        self._schedule_store: Store = Store(hass, _STORE_VERSION, _SCHEDULE_STORE_KEY)
        self._schedule_evening_refresh()
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._async_refresh_on_start)

    # ------------------------------------------------------------------ startup

    @callback
    def _async_refresh_on_start(self, _: Event) -> None:
        self.hass.async_create_task(self._async_load_and_refresh())

    async def _async_load_and_refresh(self) -> None:
        now_ts = dt_util.now().timestamp()

        events_data = await self._events_store.async_load()
        if events_data:
            self._custom_events = [
                e for e in events_data.get("events", [])
                if e.get("end_ts", 0) > now_ts
            ]

        schedule_data = await self._schedule_store.async_load()
        if schedule_data:
            restored: dict[str, dict] = {}
            for entity_id, entry in schedule_data.items():
                future_slots = [s for s in entry.get("slots", []) if s["end"] > now_ts]
                if future_slots:
                    restored[entity_id] = {"slots": future_slots, "power_w": entry["power_w"]}
            if restored:
                self._raw_schedule = restored
                self._plan_ready = True
                _LOGGER.debug("Planning restauré depuis le stockage")

        await self.async_refresh()

    # ----------------------------------------------------------------- midnight

    def _schedule_evening_refresh(self) -> None:
        @callback
        async def _evening_refresh(_now: datetime) -> None:
            self._plan_ready = False  # force recompute
            await self.async_refresh()

        self._unsub_midnight = async_track_time_change(
            self.hass, _evening_refresh, hour=20, minute=0, second=0
        )

    # --------------------------------------------------------------- transitions

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

    # --------------------------------------------------------------- update data

    async def _async_update_data(self) -> dict:
        price_sensor_id = self.config_entry.data[CONF_PRICE_SENSOR]
        state = self.hass.states.get(price_sensor_id)
        prices: list[dict] = state.attributes.get(PRICE_ATTR, []) if state else []

        if self._plan_ready:
            # Rafraîchit _slots sans recalculer le planning
            if prices:
                self._refresh_slots(prices)
            self._schedule_transitions(self._collect_transitions())
            return self._build_result()

        # Pas de plan valide : on calcule depuis le capteur de prix
        if not prices:
            if state is None:
                _LOGGER.debug("Capteur prix pas encore disponible : %s", price_sensor_id)
            else:
                _LOGGER.warning("Attribut '%s' vide sur %s", PRICE_ATTR, price_sensor_id)
            return self._build_result()

        boilers = [
            s.data for s in self.config_entry.subentries.values()
            if s.subentry_type == "boiler"
        ]

        self._raw_schedule = self._compute_raw_schedule(prices, boilers)
        self._plan_ready = True
        self.hass.async_create_task(self._schedule_store.async_save(self._raw_schedule))

        transitions = self._collect_transitions()
        self._schedule_transitions(transitions)

        return self._build_result()

    # ------------------------------------------------------------------ planning

    def _refresh_slots(self, prices: list[dict]) -> None:
        sorted_prices = sorted(prices, key=lambda p: p[PRICE_TIMESTAMP])
        slots: list[dict] = []
        for i, entry in enumerate(sorted_prices):
            start: float = entry[PRICE_TIMESTAMP]
            end: float = (
                sorted_prices[i + 1][PRICE_TIMESTAMP]
                if i + 1 < len(sorted_prices)
                else start + (
                    sorted_prices[-1][PRICE_TIMESTAMP] - sorted_prices[-2][PRICE_TIMESTAMP]
                    if len(sorted_prices) > 1 else 3600
                )
            )
            slots.append({"start": start, "end": end, "price": entry[PRICE_VALUE], "duration_min": (end - start) / 60})
        self._slots = slots

    def _compute_raw_schedule(
        self, prices: list[dict], boilers: list[dict]
    ) -> dict[str, dict]:
        if not prices:
            return {}

        now_ts = dt_util.now().timestamp()
        self._refresh_slots(prices)
        slots = self._slots

        raw: dict[str, dict] = {}
        for boiler in boilers:
            entity_id = boiler[CONF_BOILER_ENTITY]
            needed_min: float = boiler[CONF_BOILER_DURATION]
            power_w: float = boiler.get(CONF_BOILER_POWER, 0)
            latest_time: str = boiler.get(CONF_BOILER_LATEST, "")

            horizon_ts = self._compute_horizon(now_ts, latest_time)
            future_slots = [s for s in slots if s["end"] > now_ts and s["start"] < horizon_ts]

            selected: list[dict] = []
            covered_min = 0.0
            for slot in sorted(future_slots, key=lambda s: s["price"]):
                if covered_min >= needed_min:
                    break
                selected.append(slot)
                covered_min += slot["duration_min"]

            raw[entity_id] = {"slots": selected, "power_w": power_w}

        self._rebuild_boiler_power_map(raw)
        return raw

    @staticmethod
    def _compute_horizon(now_ts: float, latest_time: str) -> float:
        """Retourne le timestamp de la prochaine occurrence de latest_time (HH:MM:SS)."""
        if not latest_time:
            return now_ts + 86400
        try:
            h, m, s = (int(x) for x in latest_time.split(":"))
        except (ValueError, AttributeError):
            return now_ts + 86400
        from datetime import timedelta
        now_dt = dt_util.now()
        candidate = now_dt.replace(hour=h, minute=m, second=s, microsecond=0)
        if candidate.timestamp() <= now_ts:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    def _rebuild_boiler_power_map(self, raw: dict[str, dict]) -> None:
        self._boiler_power_by_slot = {}
        for entry in raw.values():
            for s in entry["slots"]:
                self._boiler_power_by_slot[s["start"]] = (
                    self._boiler_power_by_slot.get(s["start"], 0.0) + entry["power_w"]
                )

    def _collect_transitions(self) -> list[float]:
        transitions: set[float] = set()
        for entry in self._raw_schedule.values():
            for s in entry["slots"]:
                transitions.add(s["start"])
                transitions.add(s["end"])
        return sorted(transitions)

    def _build_result(self) -> dict:
        now_ts = dt_util.now().timestamp()
        result: dict[str, dict] = {}
        for entity_id, entry in self._raw_schedule.items():
            slots = entry["slots"]
            power_w = entry["power_w"]
            is_on = any(s["start"] <= now_ts < s["end"] for s in slots)
            scheduled_slots = self._merge_slots(
                [s for s in slots if s["end"] > now_ts], power_w
            )
            result[entity_id] = {"is_on": is_on, "scheduled_slots": scheduled_slots}
        return result

    # ---------------------------------------------------------------- merge/cost

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

    # ---------------------------------------------------------------- power cap

    def _get_max_power(self) -> float | None:
        sensor_id = self.config_entry.data.get(CONF_MAX_POWER_SENSOR)
        if not sensor_id:
            return None
        state = self.hass.states.get(sensor_id)
        if state is None:
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = state.attributes.get("unit_of_measurement", "VA")
        if unit in ("kVA", "kW", "kva"):
            value *= 1000
        return value

    def _custom_power_in_slot(self, slot_start_ts: float, slot_end_ts: float) -> float:
        total = 0.0
        for event in self._custom_events:
            if event["start_ts"] < slot_end_ts and event["end_ts"] > slot_start_ts:
                total += event["power_w"]
        return total

    # --------------------------------------------------------- plan_device service

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
        self._custom_events = [e for e in self._custom_events if e["label"] != label]
        self._custom_events.append({
            "label": label,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "power_w": power_w,
            "cost": round(cost, 4),
            "start_iso": dt_util.utc_from_timestamp(start_ts).isoformat(),
            "end_iso": dt_util.utc_from_timestamp(end_ts).isoformat(),
        })
        self.hass.async_create_task(
            self._events_store.async_save({"events": self._custom_events})
        )
        self.async_update_listeners()
