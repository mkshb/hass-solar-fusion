"""
Source Reader for Solar Fusion.

Reads forecast data *exclusively* from entities that are already present in
Home Assistant – i.e. from other installed forecast integrations.
No direct API calls are made here.

Supported upstream integrations:
  • Forecast.Solar    (built-in, domain: forecast_solar)
  • Open-Meteo Solar  (HACS,     domain: open_meteo_solar_forecast)
  • Solcast           (HACS,     domain: solcast_solar)

Each reader returns a SourceReading dataclass or raises SourceUnavailable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    FORECAST_SOLAR_ATTR_HOURLY,
    FORECAST_SOLAR_TODAY,
    FORECAST_SOLAR_TOMORROW,
    OPEN_METEO_ATTR_HOURLY,
    OPEN_METEO_TODAY,
    OPEN_METEO_TOMORROW,
    SOLCAST_ATTR_DETAILED_TODAY,
    SOLCAST_ATTR_DETAILED_TOMORROW,
    SOLCAST_ATTR_ESTIMATE,
    SOLCAST_ATTR_PERIOD_START,
    SOLCAST_TODAY,
    SOLCAST_TOMORROW,
    SOURCE_FORECAST_SOLAR,
    SOURCE_NAMES,
    SOURCE_OPEN_METEO,
    SOURCE_SOLCAST,
)

_LOGGER = logging.getLogger(__name__)

# Mapping: ISO-hour-string → Wh  (e.g. "2024-07-15T08:00" → 1200.0)
HourlyWh = Dict[str, float]


class SourceUnavailable(Exception):
    """Raised when a source's entities are missing or unavailable."""


@dataclass
class SourceReading:
    """Forecast data read from a single upstream HA integration."""
    source_id: str
    today_kwh: float
    tomorrow_kwh: float
    hourly_today: HourlyWh = field(default_factory=dict)
    hourly_tomorrow: HourlyWh = field(default_factory=dict)

    @property
    def hourly_all(self) -> HourlyWh:
        return {**self.hourly_today, **self.hourly_tomorrow}


# ──────────────────────────────────────────────────────────────────────────────
# Public interface
# ──────────────────────────────────────────────────────────────────────────────

def read_source(hass: HomeAssistant, source_id: str, entity_map: Dict[str, str]) -> SourceReading:
    """
    Read a SourceReading for the given source_id.

    entity_map allows the user to override default entity IDs:
      { "today": "sensor.my_custom_today", "tomorrow": "sensor.my_custom_tomorrow" }
    """
    readers = {
        SOURCE_FORECAST_SOLAR: _read_forecast_solar,
        SOURCE_OPEN_METEO: _read_open_meteo,
        SOURCE_SOLCAST: _read_solcast,
    }
    reader = readers.get(source_id)
    if reader is None:
        raise SourceUnavailable(f"Unknown source: {source_id}")
    return reader(hass, entity_map)


def detect_available_sources(hass: HomeAssistant) -> List[str]:
    """
    Scan the HA entity registry for entities from known forecast integrations.
    Uses domain-based lookup so localised entity IDs are found correctly.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    available = []

    # Forecast.Solar – built-in domain
    if _domain_has_states(hass, registry, "forecast_solar"):
        available.append(SOURCE_FORECAST_SOLAR)

    # Open-Meteo Solar Forecast – HACS domain
    if _domain_has_states(hass, registry, "open_meteo_solar_forecast"):
        available.append(SOURCE_OPEN_METEO)

    # Solcast – HACS domain
    if _domain_has_states(hass, registry, "solcast_solar"):
        available.append(SOURCE_SOLCAST)

    return available


def _domain_has_states(hass: HomeAssistant, registry, domain: str) -> bool:
    """Return True if any entity from the given integration domain has a valid state."""
    for entry in registry.entities.values():
        if entry.platform == domain:
            state = hass.states.get(entry.entity_id)
            if state is not None and state.state not in ("unknown", "unavailable"):
                return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Per-source readers
# ──────────────────────────────────────────────────────────────────────────────

def _read_forecast_solar(hass: HomeAssistant, entity_map: Dict[str, str]) -> SourceReading:
    """
    Read from the built-in Forecast.Solar integration.

    Entities used:
      sensor.energy_production_today      → state = kWh today
      sensor.energy_production_tomorrow   → state = kWh tomorrow
      attribute "wh_hours" on today/tomorrow  → {ISO-ts: Wh} hourly breakdown
    """
    today_id = entity_map.get("today", FORECAST_SOLAR_TODAY)
    tomorrow_id = entity_map.get("tomorrow", FORECAST_SOLAR_TOMORROW)

    today_state = _require_state(hass, today_id, SOURCE_FORECAST_SOLAR)
    tomorrow_state = _require_state(hass, tomorrow_id, SOURCE_FORECAST_SOLAR)

    today_kwh = _parse_float(today_state.state, today_id)
    tomorrow_kwh = _parse_float(tomorrow_state.state, tomorrow_id)

    hourly_today = _extract_wh_hours(today_state.attributes.get(FORECAST_SOLAR_ATTR_HOURLY, {}))
    hourly_tomorrow = _extract_wh_hours(tomorrow_state.attributes.get(FORECAST_SOLAR_ATTR_HOURLY, {}))

    return SourceReading(
        source_id=SOURCE_FORECAST_SOLAR,
        today_kwh=today_kwh,
        tomorrow_kwh=tomorrow_kwh,
        hourly_today=hourly_today,
        hourly_tomorrow=hourly_tomorrow,
    )


def _find_open_meteo_entities(hass: HomeAssistant) -> tuple[Optional[str], Optional[str]]:
    """
    Find Open-Meteo today/tomorrow entities via entity registry.

    Open-Meteo Solar Forecast (domain: open_meteo_solar_forecast) is a fork of
    Forecast.Solar and may register entities with the same default name
    (sensor.energy_production_today). Resolving via the registry ensures we
    read the correct entity and never collide with Forecast.Solar.

    Falls back to hardcoded OPEN_METEO_TODAY/TOMORROW constants if no matching
    entity is found in the registry.
    """
    from homeassistant.helpers import entity_registry as er
    registry = er.async_get(hass)

    today_id = None
    tomorrow_id = None

    for entry in registry.entities.values():
        if entry.platform != "open_meteo_solar_forecast" or entry.domain != "sensor":
            continue
        eid_lower = entry.entity_id.lower()
        if any(k in eid_lower for k in ("_today", "_heute", "_energy_today")):
            today_id = entry.entity_id
        elif any(k in eid_lower for k in ("_tomorrow", "_morgen", "_energy_tomorrow")):
            tomorrow_id = entry.entity_id

    return today_id or OPEN_METEO_TODAY, tomorrow_id or OPEN_METEO_TOMORROW


def _read_open_meteo(hass: HomeAssistant, entity_map: Dict[str, str]) -> SourceReading:
    """
    Read from the Open-Meteo Solar Forecast HACS integration.

    The integration is a fork of forecast_solar and exposes the same entity
    structure and attribute names, but registers under its own domain
    (open_meteo_solar_forecast). Entities are resolved via the entity registry
    to avoid reading the same entity as Forecast.Solar when both share the
    default name 'sensor.energy_production_today'.
    """
    # Prefer explicit user overrides, then registry lookup, then hardcoded fallback
    if entity_map.get("today") and entity_map.get("tomorrow"):
        today_id = entity_map["today"]
        tomorrow_id = entity_map["tomorrow"]
    else:
        today_id, tomorrow_id = _find_open_meteo_entities(hass)
        # Allow partial override
        today_id = entity_map.get("today") or today_id
        tomorrow_id = entity_map.get("tomorrow") or tomorrow_id

    today_state = _require_state(hass, today_id, SOURCE_OPEN_METEO)
    tomorrow_state = _require_state(hass, tomorrow_id, SOURCE_OPEN_METEO)

    today_kwh = _parse_float(today_state.state, today_id)
    tomorrow_kwh = _parse_float(tomorrow_state.state, tomorrow_id)

    hourly_today = _extract_wh_hours(today_state.attributes.get(OPEN_METEO_ATTR_HOURLY, {}))
    hourly_tomorrow = _extract_wh_hours(tomorrow_state.attributes.get(OPEN_METEO_ATTR_HOURLY, {}))

    return SourceReading(
        source_id=SOURCE_OPEN_METEO,
        today_kwh=today_kwh,
        tomorrow_kwh=tomorrow_kwh,
        hourly_today=hourly_today,
        hourly_tomorrow=hourly_tomorrow,
    )


def _find_solcast_entities(hass: HomeAssistant) -> tuple[Optional[str], Optional[str]]:
    """
    Find Solcast today/tomorrow entities via entity registry.
    Falls back to hardcoded IDs if registry lookup fails.
    Returns (today_entity_id, tomorrow_entity_id).
    """
    from homeassistant.helpers import entity_registry as er
    registry = er.async_get(hass)

    today_id = None
    tomorrow_id = None

    for entry in registry.entities.values():
        if entry.platform != "solcast_solar" or entry.domain != "sensor":
            continue
        eid = entry.entity_id
        # Match any entity whose ID contains "today" or "prognose_heute" etc.
        eid_lower = eid.lower()
        if any(k in eid_lower for k in ("_today", "_heute", "_forecast_today", "_prognose_heute")):
            today_id = eid
        elif any(k in eid_lower for k in ("_tomorrow", "_morgen", "_forecast_tomorrow", "_prognose_morgen")):
            tomorrow_id = eid

    return today_id or SOLCAST_TODAY, tomorrow_id or SOLCAST_TOMORROW


def _read_solcast(hass: HomeAssistant, entity_map: Dict[str, str]) -> SourceReading:
    """
    Read from the Solcast PV Forecast HACS integration.

    Entity IDs are resolved via the entity registry so localised names
    (e.g. German: prognose_heute / prognose_morgen) are found automatically.
    """
    # Prefer user-configured overrides, then registry lookup, then hardcoded defaults
    if entity_map.get("today") and entity_map.get("tomorrow"):
        today_id = entity_map["today"]
        tomorrow_id = entity_map["tomorrow"]
    else:
        today_id, tomorrow_id = _find_solcast_entities(hass)
        # Allow partial override
        today_id = entity_map.get("today") or today_id
        tomorrow_id = entity_map.get("tomorrow") or tomorrow_id

    today_state = _require_state(hass, today_id, SOURCE_SOLCAST)
    tomorrow_state = _require_state(hass, tomorrow_id, SOURCE_SOLCAST)

    today_kwh = _parse_float(today_state.state, today_id)
    tomorrow_kwh = _parse_float(tomorrow_state.state, tomorrow_id)

    # Each sensor exposes its own detailedHourly attribute with hourly slots (kWh each).
    hourly_today = _extract_solcast_hourly(
        today_state.attributes.get(SOLCAST_ATTR_DETAILED_TODAY, [])
    )
    hourly_tomorrow = _extract_solcast_hourly(
        tomorrow_state.attributes.get(SOLCAST_ATTR_DETAILED_TOMORROW, [])
    )

    return SourceReading(
        source_id=SOURCE_SOLCAST,
        today_kwh=today_kwh,
        tomorrow_kwh=tomorrow_kwh,
        hourly_today=hourly_today,
        hourly_tomorrow=hourly_tomorrow,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _state_exists(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state not in ("unknown", "unavailable")


def _require_state(hass: HomeAssistant, entity_id: str, source_id: str):
    state = hass.states.get(entity_id)
    if state is None:
        raise SourceUnavailable(
            f"[{SOURCE_NAMES.get(source_id, source_id)}] Entity not found: {entity_id}. "
            f"Is the integration installed and configured?"
        )
    if state.state in ("unknown", "unavailable"):
        raise SourceUnavailable(
            f"[{SOURCE_NAMES.get(source_id, source_id)}] Entity {entity_id} is {state.state}."
        )
    return state


def _parse_float(value: str, entity_id: str) -> float:
    try:
        return float(value)
    except (ValueError, TypeError) as err:
        raise SourceUnavailable(f"Cannot parse float from {entity_id}: {value!r}") from err


def _extract_solcast_hourly(slots: list) -> HourlyWh:
    """
    Convert a Solcast detailedHourly list to a normalised HourlyWh dict.

    Each slot is a dict with keys: period_start (ISO str), pv_estimate (kWh).
    Values are converted to Wh and keyed by hour-aligned local ISO string.
    """
    result: HourlyWh = {}
    for slot in slots:
        period_start = slot.get(SOLCAST_ATTR_PERIOD_START, "")
        pv_kwh = slot.get(SOLCAST_ATTR_ESTIMATE, 0.0)
        if not period_start:
            continue
        ts = _normalise_ts(period_start)
        result[ts] = result.get(ts, 0.0) + float(pv_kwh) * 1000.0  # kWh → Wh
    return result


def _extract_wh_hours(raw: dict) -> HourlyWh:
    """
    Convert the "wh_hours" attribute dict to a normalised HourlyWh dict.
    Keys may be ISO strings or datetime objects; values are Wh (float).
    """
    result: HourlyWh = {}
    for k, v in raw.items():
        ts = _normalise_ts(str(k))
        try:
            result[ts] = float(v)
        except (ValueError, TypeError):
            pass
    return result


def _normalise_ts(ts_raw) -> str:
    """
    Normalise a timestamp to "YYYY-MM-DDTHH:00" (hour-aligned, local time, no tz).
    Accepts: datetime object, ISO string (with/without tz), unix timestamp (int/float).
    """
    # datetime object
    if isinstance(ts_raw, datetime):
        dt = dt_util.as_local(ts_raw) if ts_raw.tzinfo is not None else ts_raw
        return dt.strftime("%Y-%m-%dT%H:00")

    # Unix timestamp
    if isinstance(ts_raw, (int, float)):
        from datetime import timezone
        dt = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
        return dt_util.as_local(dt).strftime("%Y-%m-%dT%H:00")

    # ISO string
    ts = str(ts_raw).replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            dt = datetime.strptime(ts[:25], fmt)
            if dt.tzinfo is not None:
                dt = dt_util.as_local(dt)
            return dt.strftime("%Y-%m-%dT%H:00")
        except ValueError:
            continue
    return ts[:16] if len(ts) >= 16 else ts