"""Energy platform – exposes Solar Fusion as an Energy Dashboard forecast provider.

HA discovers this file via async_process_integration_platforms(hass, "energy", …).
It checks hasattr(module, "async_get_solar_forecast") and registers the function.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_LOGGER.debug("solar_fusion/energy.py loaded – registering as Energy forecast provider")


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict | None:
    """Return hourly Wh forecast for the HA Energy Dashboard.

    Called by homeassistant/components/energy/websocket_api.py as:
        forecast = await forecast_platforms[domain](hass, config_entry_id)

    Returns:
        {"wh_hours": {datetime_utc_aware: wh_float, ...}}  or  None
    """
    _LOGGER.debug(
        "async_get_solar_forecast called for entry %s", config_entry_id
    )

    coordinator = hass.data.get(DOMAIN, {}).get(config_entry_id)
    if coordinator is None:
        _LOGGER.debug("No coordinator found for %s", config_entry_id)
        return None
    if coordinator.data is None:
        _LOGGER.debug("Coordinator has no data yet for %s", config_entry_id)
        return None

    data = coordinator.data
    wh_hours: dict[datetime, float] = {}

    # Resolve local timezone once
    try:
        import zoneinfo
        local_tz = zoneinfo.ZoneInfo(str(hass.config.time_zone))
    except Exception:
        local_tz = timezone.utc

    for key in ("fused_today", "fused_tomorrow"):
        hourly: dict = data.get(key) or {}
        for slot_str, wh in hourly.items():
            try:
                dt_naive = datetime.fromisoformat(slot_str)
                dt_local = (
                    dt_naive.replace(tzinfo=local_tz)
                    if dt_naive.tzinfo is None
                    else dt_naive
                )
                dt_utc = dt_local.astimezone(timezone.utc)
                wh_hours[dt_utc] = round(float(wh), 1)
            except (ValueError, TypeError, AttributeError):
                continue

    if not wh_hours:
        _LOGGER.debug("No hourly slots available for %s", config_entry_id)
        return None

    _LOGGER.debug(
        "Returning %d hourly slots for Energy Dashboard (entry %s)",
        len(wh_hours),
        config_entry_id,
    )
    return {"wh_hours": wh_hours}
