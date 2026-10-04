"""Sensor-Attribute und Warnungen beim Anlegen."""
from common import set_sources, setup_entry, state


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
