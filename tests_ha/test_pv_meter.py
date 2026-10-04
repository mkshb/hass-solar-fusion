"""PV-Tageszähler (PVDailyMeterSensor): Summe, Reset, last_reset, Neustart."""
from datetime import timedelta

import pytest
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
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

