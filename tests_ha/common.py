"""Hilfen für die Integrationstests: synthetische Anlage, Quellen, Statistik."""
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.solar_fusion import calc

ROOT_DIR = Path(__file__).resolve().parent.parent
DOMAIN = "solar_fusion"
ENTRY_ID = "e1"
STORE_KEY = f"solar_fusion_history_{ENTRY_ID}"
TZ = ZoneInfo("Europe/Berlin")
# Neutraler Standort (Hamburg, Rathausmarkt), nicht der einer echten Anlage
LAT, LON = 53.55, 9.99

OM = "sensor.om_energy_production_today"
OM_TOM = "sensor.om_energy_production_tomorrow"
SC = "sensor.solcast_pv_forecast_forecast_today"
SC_TOM = "sensor.solcast_pv_forecast_forecast_tomorrow"
PV = "sensor.pv_energy"

# Horizont: zwischen 235° und 260° Azimut unter 14° nur Diffuslicht
HORIZON = (235.0, 260.0, 14.0)
DAYS = [(date(2026, 9, 24) + timedelta(days=i)).isoformat() for i in range(10)]
CLOUDY = {"2026-09-29", "2026-10-01"}


def at(day: str, hour: int, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}").replace(tzinfo=TZ)


def _clear_sky(az, el):
    if el <= 0:
        return 0.0, 0.0
    zen, tilt = math.radians(90 - el), math.radians(40)
    cos_i = (math.cos(zen) * math.cos(tilt)
             + math.sin(zen) * math.sin(tilt) * math.cos(math.radians(az - 205)))
    air_mass = 1 / max(math.sin(math.radians(el)), 0.05)
    beam = 1000 * 0.7 ** (air_mass ** 0.678) * max(cos_i, 0.0) * 10.0
    diffuse = 120 * math.sin(math.radians(el)) ** 0.5 * 10.0
    return beam, diffuse


def synthetic_day(day: str, seed: int = 0):
    """(Prognose ohne Horizont, Ist mit Horizont) je lokaler Stunde {"HH": Wh}."""
    rng = random.Random(f"{day}-{seed}")
    cloudy = day in CLOUDY
    day_factor = rng.uniform(0.7, 1.0)
    forecast, actual = {}, {}
    for h in range(24):
        fc = ac = 0.0
        for az, el in calc.slot_sun_positions(at(day, h), LAT, LON, quarters=True):
            beam, diffuse = _clear_sky(az, el)
            shaded = HORIZON[0] < az < HORIZON[1] and el < HORIZON[2]
            fc += (beam + diffuse) / 4
            ac += ((0.0 if shaded else beam) + diffuse) / 4
        hour_factor = rng.uniform(0.15, 1.3) if cloudy else day_factor * rng.uniform(0.98, 1.02)
        forecast[f"{h:02d}"] = round(fc * day_factor, 1)
        actual[f"{h:02d}"] = round(ac * hour_factor, 1)
    return forecast, actual


def wh_period(day: str, hourly: dict) -> dict:
    return {at(day, int(hh)).isoformat(): wh for hh, wh in hourly.items()}


def set_sources(hass, day: str = "2026-10-04", shape_day: str = "2026-10-02") -> None:
    """Open-Meteo und Solcast für ``day`` und den Folgetag (Form eines klaren Tages)."""
    fc, _ = synthetic_day(shape_day)
    total = sum(fc.values()) / 1000
    tom = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    for eid, d in ((OM, day), (OM_TOM, tom)):
        hass.states.async_set(eid, round(total, 3),
                              {"wh_period": wh_period(d, fc), "unit_of_measurement": "kWh"})
    for eid, d in ((SC, day), (SC_TOM, tom)):
        detailed = [{"period_start": at(d, int(hh)).isoformat(), "pv_estimate": wh / 1000 * 1.05}
                    for hh, wh in fc.items()]
        hass.states.async_set(eid, round(total * 1.05, 3),
                              {"detailedHourly": detailed, "unit_of_measurement": "kWh"})


async def import_pv_statistics(hass, statistic_id: str = PV, days=DAYS) -> None:
    """Stündliche Energie-Statistik (sum) aus den synthetischen Ist-Werten."""
    rows, total = [], 0.0
    for day in days:
        _, actual = synthetic_day(day)
        for h in range(24):
            total += actual[f"{h:02d}"] / 1000
            rows.append({"start": at(day, h), "state": total, "sum": total})
    async_import_statistics(hass, {
        "mean_type": StatisticMeanType.NONE, "has_sum": True, "name": None,
        "source": "recorder", "statistic_id": statistic_id, "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }, rows)
    await async_wait_recording_done(hass)


def make_entry(**extra) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, entry_id=ENTRY_ID, title="Solar Fusion – Test", data={
        "sources": ["open_meteo_solar_forecast", "solcast"],
        "instance_name": "Test",
        "entity_map": {
            "open_meteo_solar_forecast": {"today": OM, "tomorrow": OM_TOM},
            "solcast": {"today": SC, "tomorrow": SC_TOM},
        },
        "pv_entities": [PV], "pv_entity": PV, "update_interval": 60, **extra,
    })


async def setup_entry(hass, **extra):
    entry = make_entry(**extra)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][ENTRY_ID]


def state(hass, suffix: str):
    """Zustand einer Solar-Fusion-Entität über das Ende der Entity-ID."""
    ids = [e for e in hass.states.async_entity_ids("sensor")
           if e.startswith("sensor.solar_fusion") and e.endswith("_" + suffix)]
    assert len(ids) == 1, ids
    return hass.states.get(ids[0])
