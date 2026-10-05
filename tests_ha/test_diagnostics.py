"""Diagnostics: technisch vollständig, ohne Namen und Entity-IDs."""
import json

from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from common import PV, set_sources, setup_entry


async def test_diagnostics_redact_names_and_entity_ids(berlin, hass_client):
    hass = berlin
    set_sources(hass)
    hass.states.async_set(PV, 1500, {"unit_of_measurement": "W", "device_class": "power",
                                     "friendly_name": "Wechselrichter Müller"})
    assert await async_setup_component(hass, "diagnostics", {})
    entry, coord = await setup_entry(hass, instance_name="Dach Müller", shading_learn=True)

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = json.dumps(diag)
    assert "sensor." not in text
    assert "Müller" not in text and "Dach" not in text

    cfg = diag["config_entry"]
    assert cfg["title"] == "**REDACTED**"
    assert cfg["data"]["instance_name"] == "**REDACTED**"
    assert cfg["data"]["pv_entities"] == "**REDACTED**"
    assert cfg["data"]["entity_map"]["solcast"] == {"today": "**REDACTED**",
                                                    "tomorrow": "**REDACTED**"}
    assert cfg["data"]["sources"] == ["open_meteo_solar_forecast", "solcast"]
    assert cfg["options"]["shading_learn"] is True

    # Technische Angaben bleiben
    assert diag["entities"]["pv_sensors"] == [{
        "found": True, "available": True, "unit": "W", "device_class": "power",
        "state_class": None, "attributes": ["device_class", "friendly_name",
                                            "unit_of_measurement"]}]
    solcast = diag["entities"]["sources"]["solcast"]["today"]
    assert solcast["unit"] == "kWh" and "detailedHourly" in solcast["attributes"]
    assert diag["coordinator"]["last_update_success"] is True
    assert diag["coordinator"]["weights"] == coord.data["weights"]
    assert diag["coordinator"]["fused_today_kwh"] == coord.data["fused_today_kwh"]
