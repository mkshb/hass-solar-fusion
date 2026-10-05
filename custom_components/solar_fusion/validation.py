"""Prüfung der im Config Flow gewählten Entitäten (ohne externe Abfragen).

Die Fehlerschlüssel stehen unter ``config.error`` in ``strings.json``. Eine
gerade ``unknown``/``unavailable`` Entität ist kein Fehler, solange ihre
Metadaten (Einheit aus Zustand oder Registry) passen: Prognose-Integrationen
und Wechselrichter liefern nach einem Neustart oft erst später Werte.
"""
from __future__ import annotations

from typing import Optional

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er

# Einheiten, die Fusion und PV-Tageszähler ohne Umrechnung verarbeiten
FORECAST_UNIT = "kWh"
PV_ENERGY_UNIT = "kWh"
PV_POWER_UNITS = ("W", "kW")

_NO_VALUE = ("unknown", "unavailable")


def _has_value(state: Optional[State]) -> bool:
    return state is not None and state.state not in _NO_VALUE


def _is_number(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


@callback
def _lookup(hass: HomeAssistant, entity_id: str) -> tuple[bool, Optional[State], Optional[str]]:
    """(bekannt, Zustand, Einheit): Einheit aus dem Zustand, sonst aus der Registry."""
    state = hass.states.get(entity_id)
    entry = er.async_get(hass).async_get(entity_id)
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None
    if unit is None and entry is not None:
        unit = entry.unit_of_measurement
    return state is not None or entry is not None, state, unit


@callback
def forecast_entity_error(hass: HomeAssistant, entity_id: str) -> Optional[str]:
    """Fehlerschlüssel für eine Prognose-Entität (Tagessumme in kWh) oder None.

    Ohne Einheit wird der Wert wie bisher als kWh gelesen.
    """
    known, state, unit = _lookup(hass, entity_id)
    if not known:
        return "entity_not_found"
    if unit is not None and unit != FORECAST_UNIT:
        return "forecast_unit"
    if _has_value(state) and not _is_number(state.state):
        return "forecast_not_numeric"
    return None


@callback
def pv_entity_error(hass: HomeAssistant, entity_id: str) -> Optional[str]:
    """Fehlerschlüssel für einen PV-Sensor (Energie in kWh oder Leistung in W/kW) oder None.

    Ohne Einheit ist nicht zu erkennen, ob Energie oder Leistung gemeint ist;
    das ist nur ein Fehler, wenn der Sensor gerade einen Wert hat.
    """
    known, state, unit = _lookup(hass, entity_id)
    if not known:
        return "entity_not_found"
    if unit is None:
        return "pv_unit" if _has_value(state) else None
    if unit != PV_ENERGY_UNIT and unit not in PV_POWER_UNITS:
        return "pv_unit"
    if _has_value(state) and not _is_number(state.state):
        return "pv_not_numeric"
    return None
