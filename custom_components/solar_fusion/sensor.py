"""Sensor platform for Solar Fusion."""
from __future__ import annotations

import json as _json
import logging
import pathlib as _pathlib
from datetime import date, datetime, timedelta
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
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import calc
from .const import (
    CONF_INSTANCE_NAME,
    CONF_PV_ENTITIES,
    CONF_PV_ENTITY,
    DOMAIN,
    HISTORY_WINDOW_DAYS,
    SOURCE_NAMES,
    device_name,
)
from .coordinator import SolarForecastCoordinator

_LOGGER = logging.getLogger(__name__)
CONF_SOURCES_KEY = "sources"


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
        ShadingSensor(coordinator, config_entry),
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

# Leistungssensoren (W/kW) werden über die Zeit integriert
_POWER_TO_KW = {"W": 0.001, "kW": 1.0}


def _power_factor(state) -> Optional[float]:
    """kW je Einheit, wenn ``state`` ein Leistungssensor ist, sonst None."""
    return _POWER_TO_KW.get(state.attributes.get("unit_of_measurement"))


class PVDailyMeterSensor(RestoreEntity, SensorEntity):
    """Tagesertrag aus einem oder mehreren PV-Sensoren.

    Energiezähler: Zuwachs seit Tagesbeginn (oder Tageswert bei last_reset
    heute). Leistungssensoren: Integral der Leistung seit Mitternacht (links,
    wie der Integral-Helfer). Nach einem Neustart ohne gespeichertes Integral
    von heute wird es aus der Langzeitstatistik bis jetzt nachgeholt.
    """
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power-variant"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, source_entity_ids: List[str]) -> None:
        self._entry = entry
        self._source_entity_ids = source_entity_ids
        self._attr_unique_id = f"{entry.entry_id}_pv_daily_meter"
        self._attr_name = "Diagnostics – PV Daily Production"
        self._attr_device_info = _device(entry)
        self._value: Optional[float] = None
        self._source_state: Dict[str, Dict] = {
            eid: {"start": None, "state_class": None, "power": None,
                  "energy": 0.0, "last_kw": None, "last_ts": None}
            for eid in source_entity_ids
        }
        self._today: date = dt_util.now().date()

    @staticmethod
    def _last_reset_is_today(state) -> bool:
        """Return True if the source entity's last_reset attribute is from today (local time)."""
        raw = state.attributes.get("last_reset")
        if not raw:
            return False
        try:
            last_reset = dt_util.parse_datetime(raw)
            if last_reset is None:
                return False
            return dt_util.as_local(last_reset).date() == dt_util.now().date()
        except (ValueError, TypeError):
            return False

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
                    energy = last.attributes.get(f"day_energy_{eid.replace('.', '_')}")
                    if energy is not None:
                        self._source_state[eid]["energy"] = float(energy)
                        self._source_state[eid]["restored"] = True
            except (ValueError, TypeError):
                pass

        # Gespeicherter Zustand vom Vortag (Neustart nach Mitternacht, bevor sich
        # eine Quelle geändert hat): Tagesbeginn verwerfen und unten aus den
        # aktuellen Quellwerten neu setzen. Sonst stünde bis zur ersten Änderung
        # der Quelle – nachts also stundenlang – der Vortagesertrag hier.
        if self._today != dt_util.now().date():
            self._today = dt_util.now().date()
            self._value = None
            for src in self._source_state.values():
                src["start"] = None
                src["energy"] = 0.0
                src.pop("restored", None)

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
        recalculate = False
        for eid in self._source_entity_ids:
            state = self.hass.states.get(eid)
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    val = float(state.state)
                    src = self._source_state[eid]
                    factor = _power_factor(state)
                    if factor is not None:
                        src["power"] = factor
                        if not src.pop("restored", False):
                            src["energy"] = await self._async_energy_so_far(eid)
                        src["last_kw"] = val * factor
                        src["last_ts"] = dt_util.utcnow()
                        recalculate = True
                        continue
                    src["state_class"] = state.attributes.get("state_class", "")
                    if self._last_reset_is_today(state):
                        if src["start"] != 0.0:
                            _LOGGER.debug("PV daily meter startup: source %s last_reset is today, setting start=0", eid)
                            src["start"] = 0.0
                            recalculate = True
                    elif src["start"] is None:
                        src["start"] = val
                        recalculate = True
                except (ValueError, TypeError):
                    pass
        if recalculate and dt_util.now().date() == self._today:
            self._value = round(self._calculate_total(), 3)
            self.async_write_ha_state()

    async def _async_energy_so_far(self, entity_id: str) -> float:
        """Integral eines Leistungssensors seit Mitternacht aus der Langzeitstatistik (kWh).

        Volle Stunden aus den Stundenmitteln, die laufende Stunde aus den
        5-Minuten-Mitteln. Ohne Statistik 0 (gezählt wird dann ab jetzt).
        """
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import statistics_during_period

            now = dt_util.utcnow()
            midnight = dt_util.as_utc(dt_util.start_of_local_day())
            hour = now.replace(minute=0, second=0, microsecond=0)
            total = 0.0
            for period, start, end, hours in (
                ("hour", midnight, hour, 1.0),
                ("5minute", max(hour, midnight), now, 5 / 60),
            ):
                if end <= start:
                    continue
                stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period, self.hass, start, end, {entity_id},
                    period, {"power": "kW"}, {"mean"},
                )
                total += sum(
                    max(0.0, row["mean"]) * hours
                    for row in stats.get(entity_id, []) if row.get("mean") is not None
                )
            return total
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("PV daily meter: no statistics for %s: %s", entity_id, err)
            return 0.0

    @staticmethod
    def _integrate(src: Dict, now: datetime) -> None:
        """Leistung seit der letzten Änderung aufsummieren (Rechteck links)."""
        if src["last_kw"] is not None and src["last_ts"] is not None:
            hours = (now - src["last_ts"]).total_seconds() / 3600
            src["energy"] += max(0.0, src["last_kw"]) * max(0.0, hours)
        src["last_ts"] = now

    @callback
    def _handle_source_change(self, event) -> None:
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if not entity_id or new_state is None:
            return
        src = self._source_state[entity_id]
        if src["power"] is not None or (
            new_state.state not in ("unknown", "unavailable") and _power_factor(new_state) is not None
        ):
            self._handle_power_change(src, new_state)
            return
        if new_state.state in ("unknown", "unavailable"):
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
        if self._last_reset_is_today(new_state):
            if src["start"] != 0.0:
                _LOGGER.debug("PV daily meter: source %s last_reset is today, setting start=0", entity_id)
                src["start"] = 0.0
        elif src["start"] is None:
            src["start"] = current_val
        self._value = round(self._calculate_total(), 3)
        self.async_write_ha_state()

    def _handle_power_change(self, src: Dict, new_state) -> None:
        if dt_util.now().date() != self._today:
            self._reset()
        now = dt_util.utcnow()
        self._integrate(src, now)
        try:
            factor = _power_factor(new_state) or src["power"]
            src["power"] = factor
            src["last_kw"] = float(new_state.state) * factor
        except (ValueError, TypeError):
            src["last_kw"] = None   # unavailable: Lücke nicht mitzählen
        self._value = round(self._calculate_total(), 3)
        self.async_write_ha_state()

    def _calculate_total(self) -> float:
        total = 0.0
        for eid in self._source_entity_ids:
            src = self._source_state[eid]
            if src["power"] is not None:
                total += src["energy"]
                continue
            state = self.hass.states.get(eid)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            try:
                val = float(state.state)
            except (ValueError, TypeError):
                continue
            src = self._source_state[eid]
            if self._last_reset_is_today(state):
                total += val
            elif src["state_class"] == "total_increasing":
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
            src = self._source_state[eid]
            if src["power"] is not None:
                # Bis jetzt gehört zum Vortag; neuer Tag beginnt bei 0
                src["energy"] = 0.0
                src["last_ts"] = dt_util.utcnow()
                continue
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    val = float(state.state)
                    if self._last_reset_is_today(state):
                        _LOGGER.debug("PV daily meter reset: source %s last_reset is today, start=0", eid)
                        src["start"] = 0.0
                    else:
                        src["start"] = val
                except (ValueError, TypeError):
                    src["start"] = None
            else:
                src["start"] = None
        self._value = round(self._calculate_total(), 3)
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
            src = self._source_state[eid]
            if src["power"] is not None:
                attrs[f"day_energy_{eid.replace('.', '_')}"] = round(src["energy"], 4)
            else:
                attrs[f"day_start_{eid.replace('.', '_')}"] = src["start"]
        return attrs


# ──────────────────────────────────────────────────────────────────────────────

def _device(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=device_name(entry.data.get(CONF_INSTANCE_NAME, "")),
        manufacturer="Solar Fusion",
        model="Adaptive Ensemble Forecaster",
        sw_version=_MANIFEST_VERSION,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Fused daily total  (today / tomorrow)
# ──────────────────────────────────────────────────────────────────────────────

class FusedForecastSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    # Keine Device-Class „energy“: Sie verlangt state_class total/total_increasing,
    # eine Prognose ist aber kein Zähler. measurement behält die Langzeitstatistik.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power"
    # Für die Karte; im Recorder nur Ballast bei jedem Update
    _unrecorded_attributes = frozenset(
        {"sources", "history", "hourly_forecast_wh", "unshaded_hourly_wh"}
    )

    def __init__(self, coordinator, entry, day: str) -> None:
        super().__init__(coordinator)
        self._day = day
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_fused_{day}"
        self._attr_name = f"Forecast – {day.capitalize()}"
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
        details = data.get("weight_details", {})

        # Compact per-source summary for the Card – single entity is enough
        sources = {}
        for sid, vals in raw.items():
            q = quality.get(sid, {})
            sources[sid] = {
                "name": SOURCE_NAMES.get(sid, sid),
                "today_kwh": vals.get("today_kwh"),
                "tomorrow_kwh": vals.get("tomorrow_kwh"),
                "weight": round(weights.get(sid, 0), 3),
                **_weight_attrs(details.get(sid, {})),
                "rmse_kwh": q.get("rmse"),
                "mae_kwh": q.get("mae"),
                "bias_kwh": q.get("bias"),
                "std_kwh": q.get("std"),
                "bias_pct": q.get("bias_pct"),
                "days_evaluated": q.get("days_evaluated", 0),
                "calibration_mode": q.get("calibration_mode", "none"),
                "quality_label": calc.quality_label(q.get("std_pct"), q.get("bias_pct")),
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
            # Stunden, in denen die Verschattung greift: Prognose ohne Verschattung
            "unshaded_hourly_wh": {
                k: round(v, 0) for k, v in sorted(data.get(f"unshaded_{self._day}", {}).items())
            },
            "active_sources": [SOURCE_NAMES.get(s, s) for s in data.get("active_sources", [])],
            "missing_sources": [SOURCE_NAMES.get(s, s) for s in data.get("missing_sources", [])],
            "last_updated": data.get("last_updated"),
            "history": _recent_history(self.coordinator.history),
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

    _attr_has_entity_name = True
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _unrecorded_attributes = frozenset({"forecast"})
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, entry, hour_offset: int = 0) -> None:
        super().__init__(coordinator)
        self._hour_offset = hour_offset
        uid_suffix, display = _HOURLY_SENSOR_META[hour_offset]
        self._attr_unique_id = f"{entry.entry_id}_{uid_suffix}"
        self._attr_name = display
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
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-areaspline-variant"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_uncertainty"
        self._attr_name = "Forecast – Uncertainty"
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
        pct = data.get("uncertainty_pct")
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
    _attr_has_entity_name = True
    _attr_icon = "mdi:check-decagram-outline"
    _attr_translation_key = "source_quality"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry, source_id: str) -> None:
        super().__init__(coordinator)
        self._source_id = source_id
        display = SOURCE_NAMES.get(source_id, source_id)
        self._attr_unique_id = f"{entry.entry_id}_quality_{source_id}"
        self._attr_name = f"Quality – {display}"
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
            "std_kwh": q.get("std"),
            "bias_pct": q.get("bias_pct"),
            "scatter_pct": q.get("std_pct"),
            "days_evaluated": q.get("days_evaluated", 0),
            "calibration_mode": q.get("calibration_mode", "none"),
            "weight": round(data.get("weights", {}).get(self._source_id, 0), 3),
            **_weight_attrs(data.get("weight_details", {}).get(self._source_id, {})),
            "today_kwh": raw.get("today_kwh"),
            "tomorrow_kwh": raw.get("tomorrow_kwh"),
        }
        label = calc.quality_label(q.get("std_pct"), q.get("bias_pct"))
        if label is not None:
            attrs["quality_label"] = label
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
    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-sunset-up"
    _unrecorded_attributes = frozenset({"history"})
    _attr_native_unit_of_measurement = None

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_morning_snapshot"
        self._attr_name = "Diagnostics – Morning Snapshot"
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
# Shading sensor
# ──────────────────────────────────────────────────────────────────────────────

class ShadingSensor(CoordinatorEntity, SensorEntity):
    """Gelernte Verschattung: Zustand = Zahl gelernter Zellen (Azimut × Höhe)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-partly-cloudy"
    _unrecorded_attributes = frozenset({"shaded_cells", "raised_cells", "energy_kept"})
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SolarForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_shading"
        self._attr_name = "Diagnostics – Shading"
        self._attr_device_info = _device(entry)

    @property
    def native_value(self) -> int:
        cells = self.coordinator.shading.get("cells", {})
        return sum(1 for c in cells.values() if c.get("learned"))

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        shading = self.coordinator.shading
        cells = shading.get("cells", {})
        def _cells(select):
            return sorted(
                (
                    {
                        "azimuth": c["az"],
                        "elevation": c["el"],
                        "factor": c["factor"],
                        "samples": c["n"],
                    }
                    for c in cells.values()
                    if c.get("learned") and not calc.is_neutral(c["factor"]) and select(c["factor"])
                ),
                key=lambda c: (c["azimuth"], c["elevation"]),
            )

        shaded = _cells(lambda f: f < 1.0)
        raised = _cells(lambda f: f > 1.0)
        data = self.coordinator.data or {}
        ratios = data.get("shading_ratios", {})
        return {
            **self.coordinator.shading_settings,
            "shaded_cells": shaded,
            # Faktor über 1: Prognose unterschätzt diese Sonnenstände regelmäßig
            "raised_cells": raised,
            "learning_days_used": shading.get("used_days", 0),
            "learning_days_stored": len(shading.get("days", {})),
            "last_learning_run": shading.get("last_run"),
            # Anteil der Tagesenergie, der je Quelle nach der Korrektur bleibt
            "energy_kept": {
                d: {SOURCE_NAMES.get(sid, sid): round(r, 3) for sid, r in vals.items()}
                for d, vals in sorted(ratios.items())
            },
        }


# ──────────────────────────────────────────────────────────────────────────────
# Weight helpers
# ──────────────────────────────────────────────────────────────────────────────

def _recent_history(history: List[Dict]) -> List[Dict]:
    """History records of the last HISTORY_WINDOW_DAYS days (all sources), oldest first.

    A fixed number of records would cover fewer days the more sources an
    instance has (30 records = 10 days with three sources).
    """
    cutoff = (dt_util.now().date() - timedelta(days=HISTORY_WINDOW_DAYS)).isoformat()
    return sorted((r for r in history if r.get("date", "") >= cutoff), key=lambda r: r["date"])


def _weight_attrs(info: Dict) -> Dict[str, Any]:
    """Gewichtungs-Attribute einer Quelle; leere Info (Quelle nicht aktiv) → Standardwerte."""
    return {
        "excluded": info.get("excluded", False),
        "exclusion_reason": info.get("exclusion_reason"),
        # False → Kalibrierung verschlechtert diese Quelle, sie geht roh in die Fusion ein
        "calibration_active": info.get("calibration_active", True),
        "rmse_calibrated_kwh": info.get("rmse_calibrated"),
        # True → Kalibrierung/Gewicht aus der verschattungskorrigierten Morgenprognose
        "shading_corrected_history": info.get("shading_corrected_history", False),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Label helpers
# ──────────────────────────────────────────────────────────────────────────────

def _uncertainty_label(pct: Optional[float]) -> str:
    if pct is None:
        return "Unknown – only one source, no cross-validation"
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


# The per-source quality label now lives in calc.quality_label (categorical,
# separating correctable bias from irreducible scatter).