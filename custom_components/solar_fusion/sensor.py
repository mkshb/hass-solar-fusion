"""Sensor platform for Solar Fusion."""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ALL_SOURCES, CONF_INSTANCE_NAME, CONF_PV_ENTITY, CONF_PV_ENTITIES, DOMAIN, SOURCE_NAMES
from .coordinator import SolarForecastCoordinator

_LOGGER = logging.getLogger(__name__)
CONF_SOURCES_KEY = "sources"


def _sensor_name(entry: ConfigEntry, suffix: str) -> str:
    """Return a sensor name prefixed with the instance name if set."""
    instance = entry.data.get(CONF_INSTANCE_NAME, "").strip()
    if instance:
        return f"Solar Fusion {instance} – {suffix}"
    return f"Solar Fusion – {suffix}"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SolarForecastCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities: List[SensorEntity] = [
        FusedForecastSensor(coordinator, config_entry, "today"),
        FusedForecastSensor(coordinator, config_entry, "tomorrow"),
        FusedHourlySensor(coordinator, config_entry),
        ForecastUncertaintySensor(coordinator, config_entry),
        MorningSnapshotSensor(coordinator, config_entry),
    ]

    for source_id in config_entry.data.get(CONF_SOURCES_KEY, []):
        entities.append(SourceQualitySensor(coordinator, config_entry, source_id))

    # Add the built-in daily meter if PV sensor(s) are configured
    pv_entities: List[str] = config_entry.data.get(CONF_PV_ENTITIES) or []
    if not pv_entities and config_entry.data.get(CONF_PV_ENTITY):
        pv_entities = [config_entry.data[CONF_PV_ENTITY]]
    if pv_entities:
        entities.append(PVDailyMeterSensor(config_entry, pv_entities))

    async_add_entities(entities, update_before_add=True)


# ──────────────────────────────────────────────────────────────────────────────
# Built-in daily PV meter  (replaces external utility_meter helper)
# ──────────────────────────────────────────────────────────────────────────────

class PVDailyMeterSensor(RestoreEntity, SensorEntity):
    """
    Tracks daily PV production, supporting one or multiple source sensors.

    When multiple source sensors are configured (e.g. Dach + Garage), their
    values are summed to produce a single combined daily total.

    Works with both sensor types:
    - total_increasing (lifetime kWh counter): tracks delta since midnight
    - daily-resetting sensor (already resets at midnight): passes through max value

    Resets itself at midnight and persists its state across HA restarts.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power-variant"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, source_entity_ids: List[str]) -> None:
        self._entry = entry
        self._source_entity_ids = source_entity_ids
        self._attr_unique_id = f"{entry.entry_id}_pv_daily_meter"
        self._attr_name = _sensor_name(entry, "PV Tagesproduktion")
        self._attr_device_info = _device(entry)

        self._value: Optional[float] = None
        # Per-source tracking: {entity_id: {"start": float, "state_class": str}}
        self._source_state: Dict[str, Dict] = {
            eid: {"start": None, "state_class": None} for eid in source_entity_ids
        }
        self._today: date = date.today()

    async def async_added_to_hass(self) -> None:
        """Restore state and subscribe to source sensors + midnight."""
        await super().async_added_to_hass()

        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            try:
                self._value = float(last.state)
                self._today = date.fromisoformat(
                    last.attributes.get("date", date.today().isoformat())
                )
                for eid in self._source_entity_ids:
                    saved = last.attributes.get(f"day_start_{eid.replace('.', '_')}")
                    if saved is not None:
                        self._source_state[eid]["start"] = float(saved)
            except (ValueError, TypeError):
                pass

        # Track all source sensors
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._source_entity_ids, self._handle_source_change
            )
        )

        # Reset at midnight
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._handle_midnight, hour=0, minute=0, second=5
            )
        )

        # Seed day_start values from current source states
        for eid in self._source_entity_ids:
            state = self.hass.states.get(eid)
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    val = float(state.state)
                    self._source_state[eid]["state_class"] = state.attributes.get("state_class", "")
                    if self._source_state[eid]["start"] is None:
                        self._source_state[eid]["start"] = val
                except (ValueError, TypeError):
                    pass

    @callback
    def _handle_source_change(self, event) -> None:
        """Recalculate summed daily total when any source sensor changes."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if not entity_id or new_state is None or new_state.state in ("unknown", "unavailable"):
            return

        try:
            current_val = float(new_state.state)
        except (ValueError, TypeError):
            return

        src = self._source_state[entity_id]
        src["state_class"] = new_state.attributes.get("state_class", "")

        # Day rollover without midnight event
        if date.today() != self._today:
            self._reset()
            return

        if src["start"] is None:
            src["start"] = current_val

        # Recalculate total across all sources
        self._value = round(self._calculate_total(), 3)
        self.async_write_ha_state()

    def _calculate_total(self) -> float:
        """Sum today's production across all configured source sensors."""
        total = 0.0
        for eid in self._source_entity_ids:
            state = self.hass.states.get(eid)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            try:
                val = float(state.state)
            except (ValueError, TypeError):
                continue
            src = self._source_state[eid]
            if src["state_class"] == "total_increasing":
                start = src["start"] or val
                delta = max(0.0, val - start)
                total += delta
            else:
                total += val
        return total

    @callback
    def _handle_midnight(self, now: datetime) -> None:
        """Reset meter at midnight."""
        self._reset()

    def _reset(self) -> None:
        """Reset all source tracking for a new day."""
        _LOGGER.debug("PV daily meter reset for new day")
        self._today = date.today()
        for eid in self._source_entity_ids:
            state = self.hass.states.get(eid)
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    self._source_state[eid]["start"] = float(state.state)
                except (ValueError, TypeError):
                    self._source_state[eid]["start"] = None
            else:
                self._source_state[eid]["start"] = None
        self._value = 0.0
        self.async_write_ha_state()

    @property
    def native_value(self) -> Optional[float]:
        return self._value

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {
            "date": self._today.isoformat(),
            "source_count": len(self._source_entity_ids),
            "source_entities": self._source_entity_ids,
        }
        # Per-source day_start values for debugging / state restore
        for eid in self._source_entity_ids:
            key = f"day_start_{eid.replace('.', '_')}"
            attrs[key] = self._source_state[eid]["start"]
        return attrs


# ──────────────────────────────────────────────────────────────────────────────


def _device(entry: ConfigEntry) -> DeviceInfo:
    instance = entry.data.get(CONF_INSTANCE_NAME, "").strip()
    device_name = f"Solar Fusion \u2013 {instance}" if instance else "Solar Fusion"
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=device_name,
        manufacturer="Solar Fusion",
        model="Adaptive Ensemble Forecaster",
        sw_version="1.0.0",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Fused daily total  (today / tomorrow)
# ──────────────────────────────────────────────────────────────────────────────

class FusedForecastSensor(CoordinatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator, entry, day: str) -> None:
        super().__init__(coordinator)
        self._day = day
        self._attr_unique_id = f"{entry.entry_id}_fused_{day}"
        self._attr_name = _sensor_name(entry, f"Fused {day.capitalize()}")
        self._attr_device_info = _device(entry)

    @property
    def native_value(self) -> Optional[float]:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(f"fused_{self._day}_kwh")

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        weights = data.get("weights", {})
        hourly = data.get(f"fused_{self._day}", {})
        raw = data.get("raw_readings", {})
        return {
            "source_weights": {
                SOURCE_NAMES.get(k, k): round(v, 3) for k, v in weights.items()
            },
            "source_values_kwh": {
                SOURCE_NAMES.get(sid, sid): vals.get(f"{self._day}_kwh")
                for sid, vals in raw.items()
            },
            "hourly_forecast_wh": {k: round(v, 0) for k, v in sorted(hourly.items())},
            "active_sources": [SOURCE_NAMES.get(s, s) for s in data.get("active_sources", [])],
            "missing_sources": [SOURCE_NAMES.get(s, s) for s in data.get("missing_sources", [])],
            "last_updated": data.get("last_updated"),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Full hourly JSON sensor (both days combined)
# ──────────────────────────────────────────────────────────────────────────────

class FusedHourlySensor(CoordinatorEntity, SensorEntity):
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_fused_hourly"
        self._attr_name = _sensor_name(entry, "Hourly Forecast")
        self._attr_device_info = _device(entry)

    @property
    def native_value(self) -> Optional[float]:
        """Combined today + tomorrow total as state."""
        data = self.coordinator.data
        if not data:
            return None
        return round(
            (data.get("fused_today_kwh") or 0) + (data.get("fused_tomorrow_kwh") or 0), 2
        )

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        today_h = data.get("fused_today", {})
        tomorrow_h = data.get("fused_tomorrow", {})
        combined = {**today_h, **tomorrow_h}
        return {
            "forecast": {k: round(v, 0) for k, v in sorted(combined.items())},
            "today_kwh": data.get("fused_today_kwh"),
            "tomorrow_kwh": data.get("fused_tomorrow_kwh"),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Uncertainty sensor
# ──────────────────────────────────────────────────────────────────────────────

class ForecastUncertaintySensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-areaspline-variant"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_uncertainty"
        self._attr_name = _sensor_name(entry, "Forecast Uncertainty")
        self._attr_device_info = _device(entry)

    @property
    def native_value(self) -> Optional[float]:
        data = self.coordinator.data
        return data.get("uncertainty_pct") if data else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        pct = data.get("uncertainty_pct", 0)
        return {
            "interpretation": _uncertainty_label(pct),
            "source_weights": {
                SOURCE_NAMES.get(k, k): round(v, 3)
                for k, v in data.get("weights", {}).items()
            },
        }


# ──────────────────────────────────────────────────────────────────────────────
# Per-source quality sensor
# ──────────────────────────────────────────────────────────────────────────────

class SourceQualitySensor(CoordinatorEntity, SensorEntity):
    _attr_icon = "mdi:check-decagram-outline"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry, source_id: str) -> None:
        super().__init__(coordinator)
        self._source_id = source_id
        display = SOURCE_NAMES.get(source_id, source_id)
        self._attr_unique_id = f"{entry.entry_id}_quality_{source_id}"
        self._attr_name = _sensor_name(entry, f"{display} RMSE")
        self._attr_device_info = _device(entry)

    @property
    def native_value(self) -> Optional[float]:
        q = self._quality()
        return q.get("rmse")

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        q = self._quality()
        data = self.coordinator.data or {}
        raw = data.get("raw_readings", {}).get(self._source_id, {})
        attrs = {
            "rmse_kwh": q.get("rmse"),
            "mae_kwh": q.get("mae"),
            "bias_kwh": q.get("bias"),
            "days_evaluated": q.get("days_evaluated", 0),
            "weight": round(data.get("weights", {}).get(self._source_id, 0), 3),
            "today_kwh": raw.get("today_kwh"),
            "tomorrow_kwh": raw.get("tomorrow_kwh"),
        }
        if q.get("rmse") is not None:
            attrs["quality_label"] = _quality_label(q["rmse"])
        return attrs

    def _quality(self) -> Dict:
        data = self.coordinator.data
        if not data:
            return {}
        return data.get("source_quality", {}).get(self._source_id, {})


# ──────────────────────────────────────────────────────────────────────────────
# Morning snapshot sensor
# ──────────────────────────────────────────────────────────────────────────────

class MorningSnapshotSensor(CoordinatorEntity, SensorEntity):
    """
    Exposes the 06:00 forecast snapshots used as RMSE reference values.

    State  : ISO timestamp of today's snapshot ("2026-03-11T06:00"), or
             "pending" if the snapshot has not been taken yet today.
    Attrs  : today's per-source values + full snapshot history (last 30 days).
    """

    _attr_icon = "mdi:weather-sunset-up"
    _attr_native_unit_of_measurement = None

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_morning_snapshot"
        self._attr_name = _sensor_name(entry, "Morning Snapshot")
        self._attr_device_info = _device(entry)

    @property
    def native_value(self) -> str:
        today_str = date.today().isoformat()
        snapshots = self.coordinator._morning_snapshots
        if today_str in snapshots:
            return f"{today_str}T06:00"
        return "pending"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        snapshots = self.coordinator._morning_snapshots
        today_str = date.today().isoformat()
        today_snap = snapshots.get(today_str, {})

        attrs: Dict[str, Any] = {
            "snapshot_taken": today_str in snapshots,
            "snapshot_time": f"{today_str}T06:00" if today_str in snapshots else None,
        }

        # Today's per-source values as flat attributes for easy use in Lovelace
        for source_id, kwh in today_snap.items():
            label = SOURCE_NAMES.get(source_id, source_id)
            attrs[f"{label.lower().replace(' ', '_').replace('.', '')}_kwh"] = round(kwh, 3)

        # Full history dict for ApexCharts / template cards
        attrs["history"] = {
            d: {SOURCE_NAMES.get(s, s): round(v, 3) for s, v in vals.items()}
            for d, vals in sorted(snapshots.items(), reverse=True)
        }

        return attrs


# ──────────────────────────────────────────────────────────────────────────────
# Label helpers
# ──────────────────────────────────────────────────────────────────────────────

def _uncertainty_label(pct: float) -> str:
    if pct < 10:
        return "Low – sources agree well"
    if pct < 25:
        return "Moderate – some disagreement"
    if pct < 50:
        return "High – sources diverge significantly"
    return "Very high – forecast unreliable"


def _quality_label(rmse: float) -> str:
    if rmse < 0.5:
        return "Excellent"
    if rmse < 1.0:
        return "Good"
    if rmse < 2.0:
        return "Fair"
    return "Poor"
