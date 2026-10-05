"""Tages-Ist nach Mitternacht, repair_history und Abgleich beim Start (echter Recorder)."""
import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from common import DOMAIN, PV, STORE_KEY, at, set_sources, setup_entry

PV2 = "sensor.pv_garage_energy"
DAY = "2026-10-03"
KWH = {"unit_of_measurement": "kWh", "state_class": "total_increasing", "device_class": "energy"}


def _store(snapshots=None, history=None):
    return {"version": 2, "minor_version": 1, "key": STORE_KEY, "data": {
        "history": history or [], "calibration_state": {},
        "morning_snapshots": snapshots if snapshots is not None else {
            DAY: {"daily": {"open_meteo_solar_forecast": 30.0, "solcast": 28.0}}},
    }}


async def _write_states(hass, freezer, entity_id, series, attrs=KWH):
    """series: [(day, hour, minute, value)] in zeitlicher Reihenfolge."""
    for day, hour, minute, value in series:
        freezer.move_to(at(day, hour, minute))
        hass.states.async_set(entity_id, value, attrs)
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def _start_after_midnight(hass, freezer, **extra):
    freezer.move_to(at("2026-10-04", 0, 30))
    set_sources(hass)
    return await setup_entry(hass, **extra)


# Tageszähler mit Reset kurz nach Mitternacht: am Vorabend 31,4 kWh (Übertrag
# ins Fenster des 03.10.), dann Reset und 25,0 kWh Tagesertrag.
DAILY_RESET = [
    ("2026-10-02", 18, 0, 31.4),
    (DAY, 0, 1, 0.0),
    (DAY, 9, 0, 3.0),
    (DAY, 13, 0, 15.5),
    (DAY, 18, 0, 25.0),
    ("2026-10-04", 0, 0, 0.0),
]


@pytest.fixture
async def hass_tz(berlin):
    return berlin


async def test_records_yesterday_against_morning_snapshot(hass_tz, freezer, hass_storage):
    hass = hass_tz
    await _write_states(hass, freezer, PV, DAILY_RESET)
    hass_storage[STORE_KEY] = _store()
    _, coord = await _start_after_midnight(hass, freezer)
    records = {r["source"]: r for r in coord.history if r["date"] == DAY}
    assert set(records) == {"open_meteo_solar_forecast", "solcast"}
    assert records["open_meteo_solar_forecast"]["forecast_kwh"] == 30.0
    assert records["solcast"]["forecast_kwh"] == 28.0
    # Übertrag vom Vortag (31,4) zählt nicht
    assert all(r["actual_kwh"] == 25.0 for r in records.values())


async def test_reset_at_exactly_midnight(hass_tz, freezer, hass_storage):
    # HA liefert nur Änderungen nach dem Abfragebeginn; die Abfrage beginnt
    # deshalb kurz vor Mitternacht, sonst fehlte das erste Delta (22 statt 25).
    hass = hass_tz
    await _write_states(hass, freezer, PV, [
        ("2026-10-02", 18, 0, 31.4),
        (DAY, 0, 0, 0.0),
        (DAY, 9, 0, 3.0),
        (DAY, 18, 0, 25.0),
    ])
    hass_storage[STORE_KEY] = _store()
    _, coord = await _start_after_midnight(hass, freezer)
    assert {r["actual_kwh"] for r in coord.history} == {25.0}


async def test_daily_meter_without_total_increasing_ignores_carryover(
    hass_tz, freezer, hass_storage
):
    # Ohne total_increasing zählt das Maximum des Tages; der Übertrag vom
    # Vortag (31,4) darf nicht das Maximum sein.
    hass = hass_tz
    attrs = {"unit_of_measurement": "kWh", "device_class": "energy"}
    await _write_states(hass, freezer, PV, DAILY_RESET, attrs=attrs)
    hass_storage[STORE_KEY] = _store()
    _, coord = await _start_after_midnight(hass, freezer)
    assert {r["actual_kwh"] for r in coord.history} == {25.0}


async def test_lifetime_counter(hass_tz, freezer, hass_storage):
    hass = hass_tz
    await _write_states(hass, freezer, PV, [
        ("2026-10-02", 18, 0, 8200.0), (DAY, 9, 0, 8203.0), (DAY, 18, 0, 8222.5),
    ])
    hass_storage[STORE_KEY] = _store()
    _, coord = await _start_after_midnight(hass, freezer)
    assert {r["actual_kwh"] for r in coord.history} == {22.5}


async def test_multiple_pv_sensors_are_summed(hass_tz, freezer, hass_storage):
    hass = hass_tz
    await _write_states(hass, freezer, PV, DAILY_RESET)
    await _write_states(hass, freezer, PV2, [
        ("2026-10-02", 18, 0, 4.0), (DAY, 0, 1, 0.0), (DAY, 12, 0, 2.0), (DAY, 18, 0, 3.5),
    ])
    hass_storage[STORE_KEY] = _store()
    _, coord = await _start_after_midnight(hass, freezer, pv_entities=[PV, PV2], pv_entity="")
    assert {r["actual_kwh"] for r in coord.history} == {28.5}


async def test_power_sensor_uses_mean(hass_tz, freezer, hass_storage):
    hass = hass_tz
    watts = {"unit_of_measurement": "W", "state_class": "measurement", "device_class": "power"}
    await _write_states(hass, freezer, PV, [
        (DAY, 10, 0, 1000.0), (DAY, 12, 0, 3000.0),
    ], attrs=watts)
    hass_storage[STORE_KEY] = _store()
    _, coord = await _start_after_midnight(hass, freezer)
    # Ohne Langzeitstatistik (Rückfall): Mittel der Messwerte × 24 h,
    # 2000 W → 48 kWh (keine Zeitgewichtung)
    assert {r["actual_kwh"] for r in coord.history} == {48.0}


async def test_no_record_without_morning_snapshot(hass_tz, freezer, hass_storage):
    hass = hass_tz
    await _write_states(hass, freezer, PV, DAILY_RESET)
    hass_storage[STORE_KEY] = _store(snapshots={})
    _, coord = await _start_after_midnight(hass, freezer)
    assert coord.history == []


async def test_no_record_without_pv_data(hass_tz, freezer, hass_storage):
    hass = hass_tz
    hass_storage[STORE_KEY] = _store()
    _, coord = await _start_after_midnight(hass, freezer)
    assert coord.history == []


def _bad_history(actual):
    return [{"date": DAY, "source": s, "forecast_kwh": 30.0, "actual_kwh": actual}
            for s in ("open_meteo_solar_forecast", "solcast")]


async def test_startup_reconciles_corrupted_history(hass_tz, freezer, hass_storage):
    hass = hass_tz
    await _write_states(hass, freezer, PV, DAILY_RESET)
    # Altfehler: Übertrag des Vortags (31,4) als Ist gespeichert
    hass_storage[STORE_KEY] = _store(history=_bad_history(31.4))
    freezer.move_to(at("2026-10-04", 12))
    set_sources(hass)
    _, coord = await setup_entry(hass)
    await hass.async_block_till_done()
    assert {r["actual_kwh"] for r in coord.history if r["date"] == DAY} == {25.0}


async def test_repair_history_action_with_date_range(hass_tz, freezer, hass_storage):
    hass = hass_tz
    await _write_states(hass, freezer, PV, DAILY_RESET)
    freezer.move_to(at("2026-10-04", 12))
    set_sources(hass)
    hass_storage[STORE_KEY] = _store()
    _, coord = await setup_entry(hass)
    await hass.async_block_till_done()

    coord.history[:] = _bad_history(99.0) + [
        {"date": "2026-09-20", "source": "solcast", "forecast_kwh": 20.0, "actual_kwh": 11.0}]
    # Bereich ohne den 03.10.: nichts ändert sich
    await hass.services.async_call(DOMAIN, "repair_history",
                                   {"date_from": "2026-09-01", "date_to": "2026-09-30"},
                                   blocking=True)
    assert {r["actual_kwh"] for r in coord.history if r["date"] == DAY} == {99.0}
    # Mit dem 03.10.: korrigiert; 20.09. hat keine Recorder-Daten und bleibt
    await hass.services.async_call(DOMAIN, "repair_history", {"date_from": DAY}, blocking=True)
    assert {r["actual_kwh"] for r in coord.history if r["date"] == DAY} == {25.0}
    assert [r["actual_kwh"] for r in coord.history if r["date"] == "2026-09-20"] == [11.0]


WATTS = {"unit_of_measurement": "W", "state_class": "measurement", "device_class": "power"}


def _import_power_statistics(hass, hours: dict, day: str = DAY):
    """Stundenmittel in W für ``day``; nicht genannte Stunden 0 W."""
    async_import_statistics(hass, {
        "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "name": None,
        "source": "recorder", "statistic_id": PV, "unit_class": "power",
        "unit_of_measurement": "W",
    }, [{"start": at(day, h), "mean": hours.get(h, 0.0), "min": 0.0, "max": hours.get(h, 0.0)}
        for h in hours.get("range", range(24))])


async def test_power_sensor_daily_actual_from_statistics(hass_tz, freezer, hass_storage):
    # Zeitgewichtet aus den Stundenmitteln: 5 h × 2 kW = 10 kWh – nicht das
    # Mittel der Zustandsänderungen × 24 h (48 kWh, siehe Rückfall oben)
    hass = hass_tz
    await _write_states(hass, freezer, PV, [(DAY, 10, 0, 1000.0), (DAY, 12, 0, 3000.0)], attrs=WATTS)
    _import_power_statistics(hass, {h: 2000.0 for h in range(10, 15)})
    await async_wait_recording_done(hass)
    hass_storage[STORE_KEY] = _store()
    _, coord = await _start_after_midnight(hass, freezer)
    assert {r["actual_kwh"] for r in coord.history} == {10.0}


async def test_waits_while_last_hours_are_missing(hass_tz, freezer, hass_storage):
    # Kurz nach Mitternacht fehlt die Statistik der letzten Stunden noch:
    # nicht aus dem Tageszähler raten, sondern beim nächsten Update erneut lesen
    hass = hass_tz
    await _write_states(hass, freezer, PV, [(DAY, 10, 0, 1000.0)], attrs=WATTS)
    _import_power_statistics(hass, {"range": range(22), 12: 2000.0})
    await async_wait_recording_done(hass)
    hass_storage[STORE_KEY] = _store()
    _, coord = await _start_after_midnight(hass, freezer)
    assert coord.history == []


async def test_gap_in_statistics_falls_back_to_states(hass_tz, freezer, hass_storage):
    hass = hass_tz
    await _write_states(hass, freezer, PV, DAILY_RESET)
    async_import_statistics(hass, {
        "mean_type": StatisticMeanType.NONE, "has_sum": True, "name": None,
        "source": "recorder", "statistic_id": PV, "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }, [{"start": at(DAY, h), "state": 1.0, "sum": float(h)} for h in range(24) if h != 12])
    await async_wait_recording_done(hass)
    hass_storage[STORE_KEY] = _store()
    _, coord = await _start_after_midnight(hass, freezer)
    assert {r["actual_kwh"] for r in coord.history} == {25.0}
