"""Verschattung mit echtem Recorder: Migration, Snapshot, Lernen, Anwenden, Aktion."""
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from common import (
    CLOUDY,
    DAYS,
    DOMAIN,
    ENTRY_ID,
    OM,
    SC,
    STORE_KEY,
    at,
    import_pv_statistics,
    set_sources,
    setup_entry,
    state,
    synthetic_day,
    wh_period,
)

CLEAR_DAYS = [d for d in DAYS if d not in CLOUDY]


@pytest.fixture
async def ready(berlin):
    await import_pv_statistics(berlin)
    set_sources(berlin)
    return berlin


def _v2_store(snapshots):
    return {"version": 2, "minor_version": 1, "key": STORE_KEY,
            "data": {"history": [], "calibration_state": {}, "morning_snapshots": snapshots}}


async def test_v1_storage_is_migrated(ready, hass_storage):
    hass = ready
    daily = {"solcast": 31.0, "open_meteo_solar_forecast": 30.0}
    hass_storage[STORE_KEY] = {
        "version": 1, "minor_version": 1, "key": STORE_KEY,
        "data": {"history": [], "calibration_state": {}, "morning_snapshots": {"2026-10-03": daily}},
    }
    _, coord = await setup_entry(hass)
    assert coord.morning_snapshots["2026-10-03"] == daily
    assert coord.shading["days"] == {}          # Snapshot ohne Stundenwerte: kein Lernen
    saved = hass_storage[STORE_KEY]
    assert saved["version"] == 2
    assert saved["data"]["morning_snapshots"]["2026-10-03"] == {"daily": daily}
    # Open-Meteo-Stundenwerte (wh_period) gehen in die Fusion ein
    assert state(hass, "forecast_today").attributes["hourly_forecast_wh"]["2026-10-04T12:00"] > 0
    assert state(hass, "diagnostics_shading").state == "0"


async def test_learns_from_snapshots_and_applies(ready, hass_storage):
    hass = ready
    snapshots = {}
    for d in DAYS:
        fc, _ = synthetic_day(d)
        snapshots[d] = {"daily": {"open_meteo_solar_forecast": sum(fc.values()) / 1000},
                        "hourly": {"open_meteo_solar_forecast": fc}}
    hass_storage[STORE_KEY] = _v2_store(snapshots)
    entry, coord = await setup_entry(hass, shading_learn=True, shading_apply=True)

    days = coord.shading["days"]
    assert set(days) == set(DAYS)
    _, actual = synthetic_day("2026-09-30")
    assert days["2026-09-30"]["actual"]["17"] == pytest.approx(actual["17"], abs=0.2)
    assert coord.shading["used_days"] == len(CLEAR_DAYS)   # Wolkentage fallen heraus

    sensor = state(hass, "diagnostics_shading")
    assert int(sensor.state) > 0
    assert sensor.attributes["active"] is True
    assert any(240 <= c["azimuth"] <= 255 and c["factor"] < 0.6
               for c in sensor.attributes["shaded_cells"])

    shaded = state(hass, "forecast_today")
    hass.config_entries.async_update_entry(entry, data={**entry.data, "shading_apply": False})
    await hass.async_block_till_done()
    plain = state(hass, "forecast_today")
    slot = "2026-10-04T17:00"
    assert shaded.attributes["hourly_forecast_wh"][slot] < 0.6 * plain.attributes["hourly_forecast_wh"][slot]
    assert float(shaded.state) < float(plain.state)
    hourly = shaded.attributes["hourly_forecast_wh"]
    assert sum(hourly.values()) / 1000 == pytest.approx(float(shaded.state), abs=0.05)


async def test_snapshot_stores_hourly_and_corrected(ready):
    hass = ready
    _, coord = await setup_entry(hass, shading_apply=True)
    await coord.async_take_snapshot_now()
    snap = coord.morning_snapshots_full["2026-10-04"]
    assert set(snap) == {"daily", "hourly", "daily_corrected"}
    fc, _ = synthetic_day("2026-10-02")
    assert snap["hourly"]["open_meteo_solar_forecast"]["12"] == pytest.approx(fc["12"], abs=0.1)
    assert snap["hourly"]["solcast"]["12"] > 0
    # Ohne gelernte Karte: korrigiert = roh
    assert snap["daily_corrected"] == {k: round(v, 3) for k, v in snap["daily"].items()}


async def test_learn_shading_action_from_recorder(berlin, freezer):
    hass = berlin
    # Recorder-Historie: Open-Meteo-Zustand um 05:30 (mit wh_period), Solcast ohne Stunden
    for d in DAYS:
        fc, _ = synthetic_day(d)
        freezer.move_to(at(d, 5, 30))
        hass.states.async_set(OM, sum(fc.values()) / 1000, {"wh_period": wh_period(d, fc)})
        hass.states.async_set(SC, 40.0, {"estimate": 40.0})
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    freezer.move_to(at("2026-10-04", 12))
    await import_pv_statistics(hass)
    set_sources(hass)
    await setup_entry(hass)

    resp = await hass.services.async_call(DOMAIN, "learn_shading", {"days": 10},
                                          blocking=True, return_response=True)
    result = resp[ENTRY_ID]
    assert [a["date"] for a in result["added"]] == DAYS
    assert all(a["sources"] == ["open_meteo_solar_forecast"] for a in result["added"])
    assert result["used_days"] == len(CLEAR_DAYS)
    assert result["learned_cells"] > 0
    assert result["warnings"] == []
