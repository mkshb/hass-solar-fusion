"""Ende der Sommerzeit (25.10.2026, 25 Stunden): Tageszähler, Tages-Ist, Energie-Plattform."""
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from common import PV, STORE_KEY, at, set_sources, setup_entry
from custom_components.solar_fusion.energy import async_get_solar_forecast
from test_pv_meter import WATTS, _meter, _set, _setup

DST_DAY = "2026-10-25"


def _utc_hours_of_local_day(day: str) -> list[datetime]:
    start = dt_util.as_utc(at(day, 0))
    end = dt_util.as_utc(at((datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat(), 0))
    hours = []
    while start < end:
        hours.append(start)
        start += timedelta(hours=1)
    return hours


async def test_power_meter_counts_the_repeated_hour(berlin, freezer):
    hass = berlin
    freezer.move_to(at(DST_DAY, 0, 30))
    await _setup(hass, [PV], {PV: (1000.0, WATTS)})
    freezer.move_to(at(DST_DAY, 4, 30))       # 00:30 CEST → 04:30 CET: 5 Stunden
    await _set(hass, PV, 1500.0, WATTS)        # gleicher Wert löst kein Ereignis aus
    assert _meter(hass) == pytest.approx(5.0)  # 1 kW × 5 h, nicht × 4 h


async def test_daily_actual_of_the_25_hour_day(berlin, freezer, hass_storage):
    hass = berlin
    freezer.move_to(at(DST_DAY, 10))
    hass.states.async_set(PV, 1000.0, WATTS)
    await hass.async_block_till_done()
    hours = _utc_hours_of_local_day(DST_DAY)
    assert len(hours) == 25
    async_import_statistics(hass, {
        "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "name": None,
        "source": "recorder", "statistic_id": PV, "unit_class": "power",
        "unit_of_measurement": "W",
    }, [{"start": h, "mean": 1000.0, "min": 1000.0, "max": 1000.0}
        for h in hours])
    await async_wait_recording_done(hass)
    hass_storage[STORE_KEY] = {"version": 2, "minor_version": 1, "key": STORE_KEY, "data": {
        "history": [], "calibration_state": {},
        "morning_snapshots": {DST_DAY: {"daily": {"solcast": 20.0}}},
    }}

    freezer.move_to(at("2026-10-26", 0, 30))
    set_sources(hass, day="2026-10-26")
    _, coord = await setup_entry(hass)
    records = [r for r in coord.history if r["date"] == DST_DAY]
    assert [r["actual_kwh"] for r in records] == [25.0]


async def test_energy_forecast_on_the_dst_day(berlin, freezer):
    hass = berlin
    freezer.move_to(at(DST_DAY, 1))
    set_sources(hass, day=DST_DAY)
    entry, coord = await setup_entry(hass)
    forecast = (await async_get_solar_forecast(hass, entry.entry_id))["wh_hours"]
    keys = list(forecast)
    assert all(k.tzinfo is timezone.utc for k in keys)
    assert len(set(keys)) == len(keys)
    today = {k: v for k, v in forecast.items() if dt_util.as_local(k).date().isoformat() == DST_DAY}
    assert sum(today.values()) / 1000 == pytest.approx(coord.data["fused_today_kwh"], abs=0.01)
    # Stunden um 12:00 Ortszeit liegen nach der Umstellung bei 11:00 UTC
    noon = datetime(2026, 10, 25, 11, tzinfo=timezone.utc)
    assert forecast[noon] == pytest.approx(coord.data["fused_today"]["2026-10-25T12:00"], abs=0.1)
