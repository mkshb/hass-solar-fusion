"""Sensor-Attribute und Warnungen beim Anlegen."""
from homeassistant.helpers import entity_registry as er

from common import DOMAIN, ENTRY_ID, make_entry, set_sources, setup_entry, state


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
