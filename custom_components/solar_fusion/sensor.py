"""Sensor platform for Solar Fusion."""
from __future__ import annotations

import json as _json
import logging
import pathlib as _pathlib
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from homeassistant.util import dt as dt_util

def _manifest_version() -> str:
    try:
        return _json.loads((_pathlib.Path(__file__).parent / "manifest.json").read_text())["version"]
    except Exception:
        return "unknown"

_MANIFEST_VERSION = _manifest_version()

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


def _entity_name(entry: ConfigEntry, suffix: str) -> str:
    """Return a fixed English entity name prefixed with the instance name."""
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
        FusedHourlySensor(coordinator, config_entry, hour_offset=0),
        FusedHourlySensor(coordinator, config_entry, hour_offset=1),
        FusedHourlySensor(coordinator, config_entry, hour_offset=2),
        ForecastUncertaintySensor(coordinator, config_entry),
        MorningSnapshotSensor(coordinator, config_entry),
    ]

    for source_id in config_entry.data.get(CONF_SOURCES_KEY, []):
        entities.append(SourceQualitySensor(coordinator, config_entry, source_id))

    pv_entities: List[str] = config_entry.data.get(CONF_PV_ENTITIES) or []
    if not pv_entities and config_entry.data.get(CONF_PV_ENTITY):
        pv_entities = [config_entry.data[CONF_PV_ENTITY]]
    if pv_entities:
        entities.append(PVDailyMeterSensor(config_entry, pv_entities))

    async_add_entities(entities, update_before_add=True)


# ──────────────────────────────────────────────────────────────────────────────
# Built-in daily PV meter
# ──────────────────────────────────────────────────────────────────────────────

class PVDailyMeterSensor(RestoreEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power-variant"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, source_entity_ids: List[str]) -> None:
        self._entry = entry
        self._source_entity_ids = source_entity_ids
        self._attr_unique_id = f"{entry.entry_id}_pv_daily_meter"
        self._attr_name = _entity_name(entry, "Diagnostics – PV Daily Production")
        self._attr_device_info = _device(entry)
        self._value: Optional[float] = None
        self._source_state: Dict[str, Dict] = {
            eid: {"start": None, "state_class": None} for eid in source_entity_ids
        }
        self._today: date = dt_util.now().date()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            try:
                self._value = float(last.state)
                self._today = date.fromisoformat(
                    last.attributes.get("date", dt_util.now().date().isoformat())
                )
                for eid in self._source_entity_ids:
                    saved = last.attributes.get(f"day_start_{eid.replace('.', '_')}")
                    if saved is not None:
                        self._source_state[eid]["start"] = float(saved)
            except (ValueError, TypeError):
                pass

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._source_entity_ids, self._handle_source_change
            )
        )
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._handle_midnight, hour=0, minute=0, second=5
            )
        )
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
        if dt_util.now().date() != self._today:
            self._reset()
            return
        # If a total_increasing source drops below our captured start (e.g. a
        # utility meter that resets a few seconds after our midnight callback),
        # treat it as a new baseline so the sensor doesn't stay at 0 all day.
        if (
            src["start"] is not None
            and src.get("state_class") == "total_increasing"
            and current_val < src["start"]
        ):
            _LOGGER.debug(
                "PV daily meter: source %s dropped from %s to %s – updating baseline",
                entity_id,
                src["start"],
                current_val,
            )
            src["start"] = current_val
        if src["start"] is None:
            src["start"] = current_val
        self._value = round(self._calculate_total(), 3)
        self.async_write_ha_state()

    def _calculate_total(self) -> float:
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
        self._reset()

    def _reset(self) -> None:
        _LOGGER.debug("PV daily meter reset for new day")
        self._today = dt_util.now().date()
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
        sw_version=_MANIFEST_VERSION,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Fused daily total  (today / tomorrow)
# ──────────────────────────────────────────────────────────────────────────────

class FusedForecastSensor(CoordinatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator, entry, day: str) -> None:
        super().__init__(coordinator)
        self._day = day
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_fused_{day}"
        self._attr_name = _entity_name(entry, f"Forecast – {day.capitalize()}")
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
        quality = data.get("source_quality", {})

        # Compact per-source summary for the Card – single entity is enough
        sources = {}
        for sid, vals in raw.items():
            q = quality.get(sid, {})
            rmse = q.get("rmse")
            sources[sid] = {
                "name": SOURCE_NAMES.get(sid, sid),
                "today_kwh": vals.get("today_kwh"),
                "tomorrow_kwh": vals.get("tomorrow_kwh"),
                "weight": round(weights.get(sid, 0), 3),
                "rmse_kwh": rmse,
                "mae_kwh": q.get("mae"),
                "bias_kwh": q.get("bias"),
                "days_evaluated": q.get("days_evaluated", 0),
                "calibration_mode": q.get("calibration_mode", "none"),
                "quality_label": _quality_label(rmse) if rmse is not None else None,
            }

        return {
            "sources": sources,
            "fused_today_kwh": data.get("fused_today_kwh"),
            "fused_tomorrow_kwh": data.get("fused_tomorrow_kwh"),
            "uncertainty_pct": data.get("uncertainty_pct"),
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
            "history": self.coordinator.history[-30:],
        }


# ──────────────────────────────────────────────────────────────────────────────
# Hourly forecast sensors (this hour / next hour / in 2 hours)
# ──────────────────────────────────────────────────────────────────────────────

_HOURLY_SENSOR_META = {
    # offset → (unique_id_suffix, display_name)
    0: ("fused_hourly",       "Forecast – This Hour"),
    1: ("fused_hourly_plus1", "Forecast – Next Hour"),
    2: ("fused_hourly_plus2", "Forecast – In 2 Hours"),
}


class FusedHourlySensor(CoordinatorEntity, SensorEntity):
    """Fused forecast for a specific hour offset (0 = current, 1 = next, 2 = in 2 h)."""

    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, entry, hour_offset: int = 0) -> None:
        super().__init__(coordinator)
        self._hour_offset = hour_offset
        uid_suffix, display = _HOURLY_SENSOR_META[hour_offset]
        self._attr_unique_id = f"{entry.entry_id}_{uid_suffix}"
        self._attr_name = _entity_name(entry, display)
        self._attr_device_info = _device(entry)

    def _forecast_slot(self) -> tuple[str, Optional[float]]:
        """Return (slot_str, wh_or_None) for this sensor's hour offset."""
        from datetime import timedelta
        data = self.coordinator.data
        if not data:
            return "", None
        target = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=self._hour_offset)
        slot_str = target.strftime("%Y-%m-%dT%H:00")
        combined = {**data.get("fused_today", {}), **data.get("fused_tomorrow", {})}
        return slot_str, combined.get(slot_str)

    @property
    def native_value(self) -> Optional[float]:
        """Return the fused forecast for the target hour in kWh."""
        _, wh = self._forecast_slot()
        if wh is None:
            return None
        return round(wh / 1000, 3)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        slot_str, _ = self._forecast_slot()
        attrs: Dict[str, Any] = {"forecast_slot": slot_str}
        # Full hourly breakdown only on the "this hour" sensor to avoid redundancy
        if self._hour_offset == 0:
            today_h = data.get("fused_today", {})
            tomorrow_h = data.get("fused_tomorrow", {})
            combined = {**today_h, **tomorrow_h}
            attrs["forecast"] = {k: round(v, 0) for k, v in sorted(combined.items())}
            attrs["today_kwh"] = data.get("fused_today_kwh")
            attrs["tomorrow_kwh"] = data.get("fused_tomorrow_kwh")
        return attrs


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
        self._attr_name = _entity_name(entry, "Forecast – Uncertainty")
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
        self._attr_name = _entity_name(entry, f"Quality – {display}")
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
            "calibration_mode": q.get("calibration_mode", "none"),
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
    _attr_icon = "mdi:weather-sunset-up"
    _attr_native_unit_of_measurement = None

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_morning_snapshot"
        self._attr_name = _entity_name(entry, "Diagnostics – Morning Snapshot")
        self._attr_device_info = _device(entry)

    @property
    def native_value(self) -> str:
        today_str = dt_util.now().date().isoformat()
        snapshots = self.coordinator.morning_snapshots
        if today_str in snapshots:
            return f"{today_str}T06:00"
        return "pending"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        snapshots = self.coordinator.morning_snapshots
        today_str = dt_util.now().date().isoformat()
        today_snap = snapshots.get(today_str, {})

        attrs: Dict[str, Any] = {
            "snapshot_taken": today_str in snapshots,
            "snapshot_time": f"{today_str}T06:00" if today_str in snapshots else None,
        }
        for source_id, kwh in today_snap.items():
            label = SOURCE_NAMES.get(source_id, source_id)
            attrs[f"{label.lower().replace(' ', '_').replace('.', '')}_kwh"] = round(kwh, 3)
        attrs["history"] = {
            d: {SOURCE_NAMES.get(s, s): round(v, 3) for s, v in vals.items()}
            for d, vals in sorted(snapshots.items(), reverse=True)
        }
        return attrs


# ──────────────────────────────────────────────────────────────────────────────
# Label helpers
# ──────────────────────────────────────────────────────────────────────────────

def _uncertainty_label(pct: float) -> str:
    labels = {
        "low": "Low – sources agree well",
        "moderate": "Moderate – some disagreement",
        "high": "High – sources diverge significantly",
        "very_high": "Very high – forecast unreliable",
    }
    if pct < 10:
        key = "low"
    elif pct < 25:
        key = "moderate"
    elif pct < 50:
        key = "high"
    else:
        key = "very_high"
    return labels[key]


def _quality_label(rmse: float) -> str:
    labels = {"excellent": "Top", "good": "Good", "fair": "Okay", "poor": "Bad"}
    if rmse < 0.5:
        key = "excellent"
    elif rmse < 1.0:
        key = "good"
    elif rmse < 2.0:
        key = "fair"
    else:
        key = "poor"
    return labels[key]