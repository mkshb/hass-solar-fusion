"""Sensor-Attribute und Warnungen beim Anlegen."""
from homeassistant.helpers import entity_registry as er

from common import DOMAIN, ENTRY_ID, STORE_KEY, make_entry, set_sources, setup_entry, state


async def test_forecast_sensors_have_no_invalid_state_class(berlin, caplog):
    hass = berlin
    set_sources(hass)
    await setup_entry(hass)
    for suffix in ("forecast_today", "forecast_tomorrow"):
        attrs = state(hass, suffix).attributes
        assert attrs.get("device_class") is None
        assert attrs["state_class"] == "measurement"
        assert attrs["unit_of_measurement"] == "kWh"
    assert "impossible considering device class" not in caplog.text


async def test_entity_ids_and_names_without_doubled_prefix(berlin):
    hass = berlin
    set_sources(hass)
    await setup_entry(hass)
    ids = sorted(e for e in hass.states.async_entity_ids("sensor") if e.startswith("sensor.solar_fusion"))
    assert "sensor.solar_fusion_test_forecast_today" in ids
    assert "sensor.solar_fusion_test_diagnostics_shading" in ids
    assert not any("solar_fusion_test_solar_fusion" in e for e in ids), ids
    friendly = hass.states.get("sensor.solar_fusion_test_forecast_today").attributes["friendly_name"]
    assert friendly == "Solar Fusion – Test Forecast – Today"


async def test_existing_entity_ids_are_kept(berlin):
    hass = berlin
    set_sources(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{ENTRY_ID}_fused_today",
        suggested_object_id="my_custom_forecast", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.my_custom_forecast") is not None
    assert hass.states.get("sensor.solar_fusion_test_forecast_today") is None


async def test_doubled_prefix_is_renamed(berlin):
    hass = berlin
    set_sources(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    reg.async_get_or_create(
        "sensor", DOMAIN, f"{ENTRY_ID}_shading",
        suggested_object_id="solar_fusion_test_solar_fusion_test_diagnostics_shading",
        config_entry=entry)
    # Ziel belegt → nicht umbenennen
    reg.async_get_or_create(
        "sensor", DOMAIN, f"{ENTRY_ID}_uncertainty",
        suggested_object_id="solar_fusion_test_solar_fusion_test_forecast_uncertainty",
        config_entry=entry)
    hass.states.async_set("sensor.solar_fusion_test_forecast_uncertainty", "taken")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{ENTRY_ID}_shading") == \
        "sensor.solar_fusion_test_diagnostics_shading"
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{ENTRY_ID}_uncertainty") == \
        "sensor.solar_fusion_test_solar_fusion_test_forecast_uncertainty"


async def test_history_attribute_covers_14_days_for_all_sources(berlin, hass_storage):
    # Früher die letzten 30 Datensätze: bei drei Quellen nur 10 Tage
    hass = berlin
    days = [f"2026-09-{d:02d}" for d in range(14, 31)] + [f"2026-10-{d:02d}" for d in range(1, 4)]
    history = [
        {"date": d, "source": sid, "forecast_kwh": 20.0, "actual_kwh": 19.0}
        for d in days for sid in ("open_meteo_solar_forecast", "solcast", "forecast_solar")
    ]
    hass_storage[STORE_KEY] = {"version": 2, "minor_version": 1, "key": STORE_KEY, "data": {
        "history": history, "calibration_state": {}, "morning_snapshots": {},
    }}
    set_sources(hass)
    await setup_entry(hass)
    records = state(hass, "forecast_today").attributes["history"]
    dates = sorted({r["date"] for r in records})
    assert dates[0] == "2026-09-20" and dates[-1] == "2026-10-03"
    assert len(dates) == 14 and len(records) == 42
