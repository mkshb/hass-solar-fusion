"""PV-Tageszähler (PVDailyMeterSensor): Summe, Reset, last_reset, Neustart."""
from datetime import timedelta

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from common import DOMAIN, ENTRY_ID, PV, at, make_entry, set_sources

PV2 = "sensor.pv_garage_energy"
METER = "sensor.pv_meter"
INCREASING = {"unit_of_measurement": "kWh", "state_class": "total_increasing"}


async def _setup(hass, sources, states):
    """Quellzustände setzen, Zähler-Entity-ID festlegen, Integration starten."""
    for eid, (value, attrs) in states.items():
        hass.states.async_set(eid, value, attrs)
    set_sources(hass)
    entry = make_entry(pv_entities=sources, pv_entity=sources[0] if len(sources) == 1 else "")
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{ENTRY_ID}_pv_daily_meter",
        suggested_object_id="pv_meter", config_entry=entry,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _meter(hass) -> float:
    return float(hass.states.get(METER).state)


async def _set(hass, eid, value, attrs):
    hass.states.async_set(eid, value, attrs)
    await hass.async_block_till_done()


async def test_sums_deltas_of_increasing_counters(berlin):
    hass = berlin
    await _setup(hass, [PV, PV2], {PV: (1000.0, INCREASING), PV2: (500.0, INCREASING)})
    assert _meter(hass) == 0.0
    await _set(hass, PV, 1003.0, INCREASING)
    await _set(hass, PV2, 502.25, INCREASING)
    assert _meter(hass) == pytest.approx(5.25)
    attrs = hass.states.get(METER).attributes
    assert attrs["source_count"] == 2
    assert attrs["day_start_sensor_pv_energy"] == 1000.0
    assert attrs["date"] == "2026-10-04"


async def test_daily_resetting_source_passes_through(berlin):
    hass = berlin
    daily = {"unit_of_measurement": "kWh", "state_class": "total"}
    await _setup(hass, [PV], {PV: (7.5, daily)})
    assert _meter(hass) == 7.5
    await _set(hass, PV, 9.0, daily)
    assert _meter(hass) == 9.0


async def test_last_reset_today_counts_from_zero(berlin):
    hass = berlin
    attrs = {**INCREASING, "last_reset": at("2026-10-04", 0, 0).isoformat()}
    await _setup(hass, [PV], {PV: (12.0, attrs)})
    assert _meter(hass) == 12.0
    assert hass.states.get(METER).attributes["day_start_sensor_pv_energy"] == 0.0


async def test_reset_at_midnight(berlin, freezer):
    hass = berlin
    await _setup(hass, [PV], {PV: (1000.0, INCREASING)})
    await _set(hass, PV, 1020.0, INCREASING)
    assert _meter(hass) == 20.0

    midnight = at("2026-10-05", 0, 0) + timedelta(seconds=5)
    freezer.move_to(midnight)
    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()
    state = hass.states.get(METER)
    assert float(state.state) == 0.0
    assert state.attributes["date"] == "2026-10-05"
    assert state.attributes["day_start_sensor_pv_energy"] == 1020.0

    freezer.move_to(at("2026-10-05", 10, 0))
    await _set(hass, PV, 1023.5, INCREASING)
    assert _meter(hass) == 3.5


async def test_restart_restores_day_start(berlin):
    hass = berlin
    mock_restore_cache(hass, [State(METER, "12.3", {
        "date": "2026-10-04", "day_start_sensor_pv_energy": 1000.0,
    })])
    await _setup(hass, [PV], {PV: (1012.3, INCREASING)})
    assert _meter(hass) == pytest.approx(12.3)
    await _set(hass, PV, 1013.0, INCREASING)
    assert _meter(hass) == pytest.approx(13.0)



async def test_restart_after_midnight_with_state_from_yesterday(berlin, freezer):
    hass = berlin
    freezer.move_to(at("2026-10-04", 2, 0))
    mock_restore_cache(hass, [State(METER, "25.0", {
        "date": "2026-10-03", "day_start_sensor_pv_energy": 975.0,
    })])
    await _setup(hass, [PV], {PV: (1000.0, INCREASING)})
    state = hass.states.get(METER)
    assert float(state.state) == 0.0
    assert state.attributes["date"] == "2026-10-04"


WATTS = {"unit_of_measurement": "W", "state_class": "measurement", "device_class": "power"}


async def test_power_source_is_integrated(berlin, freezer):
    # Früher wurde der Momentanwert in W als kWh übernommen
    hass = berlin
    await _setup(hass, [PV], {PV: (2000.0, WATTS)})
    assert _meter(hass) == 0.0
    freezer.tick(timedelta(minutes=30))
    await _set(hass, PV, 1000.0, WATTS)
    assert _meter(hass) == pytest.approx(1.0)      # 2 kW × 0,5 h
    freezer.tick(timedelta(hours=1))
    await _set(hass, PV, "unavailable", {})
    assert _meter(hass) == pytest.approx(2.0)      # + 1 kW × 1 h
    freezer.tick(timedelta(hours=1))
    await _set(hass, PV, 500.0, WATTS)
    assert _meter(hass) == pytest.approx(2.0)      # Lücke zählt nicht


async def test_power_source_resets_at_midnight(berlin, freezer):
    hass = berlin
    await _setup(hass, [PV], {PV: (1000.0, WATTS)})
    freezer.move_to(at("2026-10-04", 23, 59))
    await _set(hass, PV, 0.0, WATTS)
    assert _meter(hass) > 11.0
    freezer.move_to(at("2026-10-05", 0, 0) + timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _meter(hass) == 0.0
    freezer.tick(timedelta(hours=1))
    await _set(hass, PV, 100.0, WATTS)
    assert _meter(hass) == 0.0
    freezer.tick(timedelta(hours=2))
    await _set(hass, PV, 0.0, WATTS)
    assert _meter(hass) == pytest.approx(0.2)


async def test_power_source_catches_up_from_statistics_after_restart(berlin, freezer):
    # Ohne gespeichertes Integral von heute (z. B. erster Start mit dem Sensor):
    # bisheriger Tagesertrag aus den Stundenmitteln, 3 h × 1 kW
    hass = berlin
    async_import_statistics(hass, {
        "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "name": None,
        "source": "recorder", "statistic_id": PV, "unit_class": "power",
        "unit_of_measurement": "W",
    }, [{"start": at("2026-10-04", h), "mean": 1000.0 if h in (9, 10, 11) else 0.0,
         "min": 0.0, "max": 1000.0} for h in range(12)])
    await async_wait_recording_done(hass)
    await _setup(hass, [PV], {PV: (1500.0, WATTS)})
    assert _meter(hass) == pytest.approx(3.0)


async def test_power_source_restores_integral(berlin):
    hass = berlin
    mock_restore_cache(hass, [State(METER, "4.2", {
        "date": "2026-10-04", "day_energy_sensor_pv_energy": 4.2,
    })])
    await _setup(hass, [PV], {PV: (1500.0, WATTS)})
    assert _meter(hass) == pytest.approx(4.2)
