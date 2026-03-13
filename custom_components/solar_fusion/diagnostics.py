"""Diagnostics support for Solar Fusion."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """
    Return diagnostics for a Solar Fusion config entry.

    Accessible via Settings → Devices & Services → Solar Fusion → ⋮ → Download diagnostics.
    Contains all data needed to debug forecast, calibration, and history issues.
    No sensitive data is stored or redacted.
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data or {}

    return {
        "config_entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_updated": data.get("last_updated"),
            "active_sources": data.get("active_sources", []),
            "missing_sources": data.get("missing_sources", []),
            "weights": data.get("weights", {}),
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
            "snapshot_count": len(coordinator.morning_snapshots),
            "snapshots": coordinator.morning_snapshots,
        },
    }
