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
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    FORECAST_SOLAR_ATTR_HOURLY,
    FORECAST_SOLAR_TODAY,
    FORECAST_SOLAR_TOMORROW,
    OPEN_METEO_ATTR_HOURLY,
    OPEN_METEO_ATTR_HOURLY_LEGACY,
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

# Numerischer Suffix, den HA bei Namenskollisionen anhängt ("…_today_2")
_OM_ID_SUFFIX = re.compile(r"_\d+$")


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
                                                 (Schlüssel = Periodenende)

    Die Core-Integration setzt dieses Attribut nicht; Stundenwerte gibt es nur,
    wenn eine eigene Entität es bereitstellt.
    """
    today_id = entity_map.get("today", FORECAST_SOLAR_TODAY)
    tomorrow_id = entity_map.get("tomorrow", FORECAST_SOLAR_TOMORROW)

    today_state = _require_state(hass, today_id, SOURCE_FORECAST_SOLAR)
    tomorrow_state = _require_state(hass, tomorrow_id, SOURCE_FORECAST_SOLAR)

    today_kwh = _parse_float(today_state.state, today_id)
    tomorrow_kwh = _parse_float(tomorrow_state.state, tomorrow_id)

    hourly_today = _extract_wh_hours(
        today_state.attributes.get(FORECAST_SOLAR_ATTR_HOURLY, {}), period_end=True
    )
    hourly_tomorrow = _extract_wh_hours(
        tomorrow_state.attributes.get(FORECAST_SOLAR_ATTR_HOURLY, {}), period_end=True
    )

    return SourceReading(
        source_id=SOURCE_FORECAST_SOLAR,
        today_kwh=today_kwh,
        tomorrow_kwh=tomorrow_kwh,
        hourly_today=hourly_today,
        hourly_tomorrow=hourly_tomorrow,
    )


def _find_open_meteo_entities(hass: HomeAssistant) -> tuple[Optional[str], Optional[str]]:
    """
    Find Open-Meteo energy_production_today/tomorrow entities via entity registry.

    Open-Meteo Solar Forecast (domain: open_meteo_solar_forecast) is a fork of
    Forecast.Solar and may register entities with the same default name
    (sensor.energy_production_today). Resolving via the registry ensures we
    read the correct entity and never collide with Forecast.Solar.

    Matching uses the registry's ``translation_key`` (``energy_production_today``
    / ``energy_production_tomorrow``), which does not depend on the entity ID.
    When Forecast.Solar is installed first, Open-Meteo's entity gets a suffix
    (``sensor.energy_production_today_2``) and an ID-based match would miss it
    and fall back to Forecast.Solar's entity. Entries without a translation key
    (older versions) are matched by an ID ending in "energy_production_today",
    optionally followed by a numeric suffix. This avoids false matches on other
    Open-Meteo sensors such as "power_highest_peak_time_today".

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
        kind = entry.translation_key or _OM_ID_SUFFIX.sub("", entry.entity_id.lower())
        if kind.endswith("energy_production_today"):
            today_id = today_id or entry.entity_id
        elif kind.endswith("energy_production_tomorrow"):
            tomorrow_id = tomorrow_id or entry.entity_id
        if today_id and tomorrow_id:
            break

    _LOGGER.debug(
        "Open-Meteo entity lookup → today=%s, tomorrow=%s",
        today_id or f"(not found, fallback: {OPEN_METEO_TODAY})",
        tomorrow_id or f"(not found, fallback: {OPEN_METEO_TOMORROW})",
    )
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
    # Always resolve via entity registry to avoid reading Forecast.Solar entities.
    # Both integrations share the same default entity name, so the registry lookup
    # is the only reliable way to get the correct Open-Meteo entity.
    # Entity map values override the registry result only when explicitly set to
    # a value different from the ambiguous default constant.
    today_id, tomorrow_id = _find_open_meteo_entities(hass)
    override_today = entity_map.get("today", "")
    override_tomorrow = entity_map.get("tomorrow", "")
    if override_today and override_today != OPEN_METEO_TODAY:
        today_id = override_today
    if override_tomorrow and override_tomorrow != OPEN_METEO_TOMORROW:
        tomorrow_id = override_tomorrow

    today_state = _require_state(hass, today_id, SOURCE_OPEN_METEO)
    tomorrow_state = _require_state(hass, tomorrow_id, SOURCE_OPEN_METEO)

    today_kwh = _parse_float(today_state.state, today_id)
    tomorrow_kwh = _parse_float(tomorrow_state.state, tomorrow_id)
    _LOGGER.debug(
        "Open-Meteo reading → entity=%s state=%s kWh (tomorrow: %s → %s kWh)",
        today_id, today_state.state, tomorrow_id, tomorrow_state.state,
    )

    hourly_today = _extract_wh_hours(_open_meteo_hourly_attr(today_state))
    hourly_tomorrow = _extract_wh_hours(_open_meteo_hourly_attr(tomorrow_state))

    return SourceReading(
        source_id=SOURCE_OPEN_METEO,
        today_kwh=today_kwh,
        tomorrow_kwh=tomorrow_kwh,
        hourly_today=hourly_today,
        hourly_tomorrow=hourly_tomorrow,
    )


def _open_meteo_hourly_attr(state) -> dict:
    """Stundenwerte einer Open-Meteo-Entität ("wh_period", Schlüssel = Stundenbeginn)."""
    attrs = state.attributes
    return attrs.get(OPEN_METEO_ATTR_HOURLY) or attrs.get(OPEN_METEO_ATTR_HOURLY_LEGACY) or {}


def _find_solcast_entities(hass: HomeAssistant) -> tuple[Optional[str], Optional[str]]:
    """
    Find Solcast forecast_today/forecast_tomorrow entities via entity registry.

    The match is intentionally strict: only entities whose ID contains
    "forecast_today" / "forecast_tomorrow" (or German equivalents) are
    accepted. This avoids false matches on other Solcast sensors that also
    contain "_today", e.g. "remaining_today" (which counts *down* during
    the day and would corrupt the forecast value).

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
        eid_lower = entry.entity_id.lower()
        if any(eid_lower.endswith(k) for k in ("forecast_today", "prognose_heute")):
            today_id = entry.entity_id
        elif any(eid_lower.endswith(k) for k in ("forecast_tomorrow", "prognose_morgen")):
            tomorrow_id = entry.entity_id
        if today_id and tomorrow_id:
            break

    _LOGGER.debug(
        "Solcast entity lookup → today=%s, tomorrow=%s",
        today_id or f"(not found, fallback: {SOLCAST_TODAY})",
        tomorrow_id or f"(not found, fallback: {SOLCAST_TOMORROW})",
    )
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


def _extract_wh_hours(raw: dict, period_end: bool = False) -> HourlyWh:
    """
    Convert an hourly {timestamp: Wh} attribute dict to a normalised HourlyWh dict.
    Keys may be ISO strings or datetime objects; values are Wh (float).

    ``period_end``: der Schlüssel bezeichnet das Ende der Periode (Forecast.Solar).
    Der Wert gehört dann zu der Stunde, die kurz vor dem Schlüssel liegt
    (08:00 → Slot 07:00, Sonnenaufgang 07:13 → Slot 07:00).

    Mehrere Perioden in derselben lokalen Stunde werden summiert – auch die
    doppelte Stunde beim Ende der Sommerzeit (02:00+02:00 und 02:00+01:00).
    """
    result: HourlyWh = {}
    for k, v in raw.items():
        try:
            wh = float(v)
        except (ValueError, TypeError):
            continue
        ts = _normalise_ts(_shift_back(k) if period_end else str(k))
        result[ts] = result.get(ts, 0.0) + wh
    return result


def _shift_back(ts_raw):
    """Zeitstempel um eine Sekunde zurück (Periodenende → letzte Sekunde der Periode)."""
    from datetime import timedelta
    if isinstance(ts_raw, datetime):
        return ts_raw - timedelta(seconds=1)
    dt = dt_util.parse_datetime(str(ts_raw).replace(" ", "T"))
    return dt - timedelta(seconds=1) if dt is not None else str(ts_raw)


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

# ──────────────────────────────────────────────────────────────────────────────
# Rekonstruktion aus der Recorder-Historie (rückwirkendes Lernen)
# ──────────────────────────────────────────────────────────────────────────────

def today_entity_id(hass: HomeAssistant, source_id: str, entity_map: Dict[str, str]) -> Optional[str]:
    """Entität mit der Heute-Prognose einer Quelle (gleiche Auflösung wie beim Lesen)."""
    if source_id == SOURCE_FORECAST_SOLAR:
        return entity_map.get("today", FORECAST_SOLAR_TODAY)
    if source_id == SOURCE_OPEN_METEO:
        today_id, _ = _find_open_meteo_entities(hass)
        override = entity_map.get("today", "")
        return override if override and override != OPEN_METEO_TODAY else today_id
    if source_id == SOURCE_SOLCAST:
        return entity_map.get("today") or _find_solcast_entities(hass)[0]
    return None


def hourly_from_attributes(source_id: str, attributes) -> HourlyWh:
    """Stundenwerte einer Quelle aus den Attributen ihrer Heute-Entität.

    Funktioniert nur, wenn der Recorder die Attribute speichert: Open-Meteo
    ("wh_period") ja, Solcast ("detailedHourly") nein – die Integration nimmt
    es von der Aufzeichnung aus.
    """
    if source_id == SOURCE_FORECAST_SOLAR:
        return _extract_wh_hours(attributes.get(FORECAST_SOLAR_ATTR_HOURLY, {}), period_end=True)
    if source_id == SOURCE_OPEN_METEO:
        return _extract_wh_hours(_open_meteo_hourly_attr(_AttrState(attributes)))
    if source_id == SOURCE_SOLCAST:
        return _extract_solcast_hourly(attributes.get(SOLCAST_ATTR_DETAILED_TODAY, []))
    return {}


class _AttrState:
    """Adapter: _open_meteo_hourly_attr erwartet ein Objekt mit .attributes."""

    def __init__(self, attributes) -> None:
        self.attributes = attributes
