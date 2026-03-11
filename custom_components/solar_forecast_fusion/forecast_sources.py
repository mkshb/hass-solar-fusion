"""Forecast source API clients for Solar Forecast Fusion."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import aiohttp

from .const import (
    FORECAST_SOLAR_BASE,
    OPEN_METEO_BASE,
    PVGIS_BASE,
    SOURCE_FORECAST_SOLAR,
    SOURCE_OPEN_METEO,
    SOURCE_PVGIS,
)

_LOGGER = logging.getLogger(__name__)

# Type alias: mapping of hour-timestamp (ISO string) -> watt-hours forecasted
HourlyForecast = Dict[str, float]


class ForecastSourceError(Exception):
    """Raised when a forecast source fails."""


async def fetch_forecast_solar(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    declination: int,
    azimuth: int,
    kwp: float,
) -> HourlyForecast:
    """
    Fetch hourly PV forecast from Forecast.Solar (free, non-commercial tier).
    Returns dict of {ISO-hour-string: Wh} for today and tomorrow.
    Docs: https://doc.forecast.solar/api:estimate
    """
    # Forecast.Solar azimuth: -180..180, 0=south. Convert from compass bearing.
    fs_azimuth = azimuth - 180  # 180°(S) -> 0, 270°(W) -> 90, 90°(E) -> -90

    url = (
        f"{FORECAST_SOLAR_BASE}/{latitude}/{longitude}"
        f"/{declination}/{fs_azimuth}/{kwp}"
    )
    params = {"time": "iso8601", "damping": "0"}

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                raise ForecastSourceError(
                    f"Forecast.Solar returned HTTP {resp.status}"
                )
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise ForecastSourceError(f"Forecast.Solar connection error: {err}") from err

    result: HourlyForecast = {}
    hourly = data.get("result", {}).get("watt_hours_period", {})
    for ts_str, wh in hourly.items():
        result[ts_str[:16]] = float(wh)  # truncate to "YYYY-MM-DDTHH:MM"
    return result


async def fetch_open_meteo(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    declination: int,
    azimuth: int,
    kwp: float,
) -> HourlyForecast:
    """
    Fetch hourly PV forecast from Open-Meteo (open-source, free).
    Uses direct normal irradiance + diffuse + temperature to estimate yield.
    Docs: https://open-meteo.com/en/docs
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "direct_normal_irradiance,diffuse_radiation,temperature_2m,cloudcover",
        "forecast_days": 2,
        "timezone": "auto",
    }

    try:
        async with session.get(OPEN_METEO_BASE, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                raise ForecastSourceError(f"Open-Meteo returned HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise ForecastSourceError(f"Open-Meteo connection error: {err}") from err

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    dni = hourly.get("direct_normal_irradiance", [])
    dhi = hourly.get("diffuse_radiation", [])
    temps = hourly.get("temperature_2m", [])

    result: HourlyForecast = {}
    decl_rad = _deg_to_rad(declination)
    azimuth_rad = _deg_to_rad(azimuth - 180)  # convert to south=0

    for i, ts in enumerate(times):
        if i >= len(dni) or i >= len(dhi):
            break

        dni_val = float(dni[i] or 0)
        dhi_val = float(dhi[i] or 0)
        temp = float(temps[i] if i < len(temps) else 25)

        # Simplified plane-of-array irradiance (no full solar geometry)
        # Uses cos(declination) factor as rough transposition for tilted surface
        import math
        poa = (
            dni_val * math.cos(decl_rad)
            + dhi_val * (1 + math.cos(decl_rad)) / 2
        )

        # Temperature derating: -0.4%/°C above 25°C STC
        temp_factor = 1.0 - max(0, (temp - 25) * 0.004)

        # System efficiency assumption: 80% (inverter + wiring losses)
        system_efficiency = 0.80

        wh = max(0.0, poa * kwp * temp_factor * system_efficiency)
        result[ts[:16]] = round(wh, 1)

    return result


async def fetch_pvgis(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    declination: int,
    azimuth: int,
    kwp: float,
) -> HourlyForecast:
    """
    Fetch typical meteorological year (TMY) hourly data from PVGIS (EU JRC).
    Used as a climatological baseline / prior for the fusion weights.
    Returns today's and tomorrow's hour slots filled with TMY averages.
    Docs: https://re.jrc.ec.europa.eu/api/v5_2/
    """
    # PVGIS azimuth: 0=south, -90=east, 90=west
    pvgis_azimuth = azimuth - 180

    params = {
        "lat": latitude,
        "lon": longitude,
        "angle": declination,
        "aspect": pvgis_azimuth,
        "peakpower": kwp,
        "loss": 14,           # default system losses %
        "outputformat": "json",
        "startyear": 2015,
        "endyear": 2020,
        "pvcalculation": 1,
    }

    try:
        async with session.get(
            f"{PVGIS_BASE}",
            params=params,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                raise ForecastSourceError(f"PVGIS returned HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise ForecastSourceError(f"PVGIS connection error: {err}") from err

    # Build average hourly profile per (month, hour) from TMY data
    hourly_profile: Dict[tuple, list] = {}
    outputs = data.get("outputs", {}).get("hourly", [])
    for entry in outputs:
        ts_str = entry.get("time", "")  # "20150101:0010"
        try:
            month = int(ts_str[4:6])
            hour = int(ts_str[9:11])
            p_w = float(entry.get("P", 0))
            key = (month, hour)
            hourly_profile.setdefault(key, []).append(p_w)
        except (ValueError, IndexError):
            continue

    avg_profile: Dict[tuple, float] = {
        k: sum(v) / len(v) for k, v in hourly_profile.items()
    }

    # Project onto today and tomorrow
    result: HourlyForecast = {}
    for day_offset in range(2):
        target_date = date.today() + timedelta(days=day_offset)
        month = target_date.month
        for hour in range(24):
            wh = avg_profile.get((month, hour), 0.0)
            ts = f"{target_date.isoformat()}T{hour:02d}:00"
            result[ts] = round(wh, 1)

    return result


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def fetch_source(
    source_id: str,
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    declination: int,
    azimuth: int,
    kwp: float,
) -> Optional[HourlyForecast]:
    """Fetch forecast from a named source. Returns None on failure."""
    fetchers = {
        SOURCE_FORECAST_SOLAR: fetch_forecast_solar,
        SOURCE_OPEN_METEO: fetch_open_meteo,
        SOURCE_PVGIS: fetch_pvgis,
    }
    fetcher = fetchers.get(source_id)
    if fetcher is None:
        _LOGGER.warning("Unknown source: %s", source_id)
        return None
    try:
        return await fetcher(session, latitude, longitude, declination, azimuth, kwp)
    except ForecastSourceError as err:
        _LOGGER.warning("Source %s failed: %s", source_id, err)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deg_to_rad(degrees: float) -> float:
    import math
    return degrees * math.pi / 180.0
