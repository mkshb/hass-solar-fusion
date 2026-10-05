"""Diagnostics support for Solar Fusion."""
from __future__ import annotations

from typing import Any, Dict, Optional

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant

from .const import CONF_INSTANCE_NAME, CONF_PV_ENTITIES, CONF_PV_ENTITY
from .coordinator import SolarFusionConfigEntry
from .source_reader import resolve_entities

# Benutzerdefinierte Namen und Entity-IDs (verraten Anlagen- und Gerätenamen)
TO_REDACT_DATA = {CONF_INSTANCE_NAME, CONF_PV_ENTITY, CONF_PV_ENTITIES, "today", "tomorrow"}


def _entity_info(hass: HomeAssistant, entity_id: str) -> Dict[str, Any]:
    """Technische Eckdaten einer Entität ohne ihre ID."""
    state = hass.states.get(entity_id)
    if state is None:
        return {"found": False}
    attrs = state.attributes
    return {
        "found": True,
        "available": state.state not in ("unknown", "unavailable"),
        "unit": attrs.get(ATTR_UNIT_OF_MEASUREMENT),
        "device_class": attrs.get(ATTR_DEVICE_CLASS),
        "state_class": attrs.get("state_class"),
        "attributes": sorted(attrs),
    }


def _source_entities(hass: HomeAssistant, entry: SolarFusionConfigEntry) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    entity_map = entry.data.get("entity_map", {})
    for source_id in entry.data.get("sources", []):
        resolved: Optional[tuple[str, str]] = resolve_entities(
            hass, source_id, entity_map.get(source_id, {})
        )
        if resolved is None:
            continue
        out[source_id] = {
            day: _entity_info(hass, entity_id)
            for day, entity_id in zip(("today", "tomorrow"), resolved)
        }
    return out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SolarFusionConfigEntry
) -> dict[str, Any]:
    """
    Return diagnostics for a Solar Fusion config entry.

    Accessible via Settings → Devices & Services → Solar Fusion → ⋮ → Download diagnostics.
    Contains the data needed to debug forecast, calibration, and history issues.
    Entry title, instance name and entity IDs are redacted; the forecast and
    PV entities are described by unit, classes and attribute names only.
    """
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    pv_entities = entry.data.get(CONF_PV_ENTITIES) or (
        [entry.data[CONF_PV_ENTITY]] if entry.data.get(CONF_PV_ENTITY) else []
    )

    return {
        "config_entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "minor_version": entry.minor_version,
            "title": "**REDACTED**",
            "data": async_redact_data(dict(entry.data), TO_REDACT_DATA),
            "options": dict(entry.options),
        },
        "entities": {
            "sources": _source_entities(hass, entry),
            "pv_sensors": [_entity_info(hass, entity_id) for entity_id in pv_entities],
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_updated": data.get("last_updated"),
            "active_sources": data.get("active_sources", []),
            "missing_sources": data.get("missing_sources", []),
            "weights": data.get("weights", {}),
            "weight_details": data.get("weight_details", {}),
            "fused_today_kwh": data.get("fused_today_kwh"),
            "fused_tomorrow_kwh": data.get("fused_tomorrow_kwh"),
            "uncertainty_pct": data.get("uncertainty_pct"),
            "source_quality": data.get("source_quality", {}),
            "raw_readings": data.get("raw_readings", {}),
        },
        "history": {
            "record_count": len(coordinator.history),
            "records": coordinator.history,
        },
        "morning_snapshots": {
            "snapshot_count": len(coordinator.morning_snapshots_full),
            "snapshots": coordinator.morning_snapshots_full,
        },
        "shading": {
            **coordinator.shading_settings,
            "used_days": coordinator.shading.get("used_days", 0),
            "last_run": coordinator.shading.get("last_run"),
            "shading_ratios": data.get("shading_ratios", {}),
            "cells": coordinator.shading.get("cells", {}),
            "days": coordinator.shading.get("days", {}),
        },
    }
