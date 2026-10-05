"""Prüfung der gewählten Entitäten im Config Flow."""
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from common import DOMAIN, OM, OM_TOM, PV, SC, SC_TOM, set_sources
from custom_components.solar_fusion.validation import forecast_entity_error, pv_entity_error

KWH = {"unit_of_measurement": "kWh"}
SOLCAST_MAP = {"solcast_today": SC, "solcast_tomorrow": SC_TOM}


async def _to_entities(hass, sources=("solcast",)):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sources": list(sources), "instance_name": ""})
    assert result["step_id"] == "entities"
    return result


async def _to_settings(hass):
    result = await _to_entities(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SOLCAST_MAP)
    assert result["step_id"] == "settings"
    return result


async def test_forecast_entity_errors_are_shown_per_field(berlin):
    hass = berlin
    set_sources(hass)
    hass.states.async_set(SC, 30.0, {"unit_of_measurement": "Wh"})
    result = await _to_entities(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"solcast_today": SC, "solcast_tomorrow": "sensor.missing"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "entities"
    assert result["errors"] == {"solcast_today": "forecast_unit",
                                "solcast_tomorrow": "entity_not_found"}

    hass.states.async_set(SC, 30.0, KWH)
    hass.states.async_set("sensor.missing", 31.0, KWH)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"solcast_today": SC, "solcast_tomorrow": "sensor.missing"})
    assert result["step_id"] == "settings"


async def test_open_meteo_is_checked_where_it_is_read(berlin):
    """Formular zeigt den Standardnamen, gelesen wird die Entität aus der Registry."""
    hass = berlin
    er.async_get(hass).async_get_or_create(
        "sensor", "open_meteo_solar_forecast", "om_today",
        suggested_object_id="om_energy_production_today")
    set_sources(hass)
    # Gleichnamige Forecast.Solar-Entität mit fremder Einheit stört nicht
    hass.states.async_set("sensor.energy_production_today", 5000, {"unit_of_measurement": "Wh"})
    result = await _to_entities(hass, ["open_meteo_solar_forecast"])
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "open_meteo_solar_forecast_today": "sensor.energy_production_today",
        "open_meteo_solar_forecast_tomorrow": OM_TOM,
    })
    assert result["step_id"] == "settings"

    hass.states.async_set(OM, "not a number", KWH)
    result = await _to_entities(hass, ["open_meteo_solar_forecast"])
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "open_meteo_solar_forecast_today": "sensor.energy_production_today",
        "open_meteo_solar_forecast_tomorrow": OM_TOM,
    })
    assert result["errors"] == {"open_meteo_solar_forecast_today": "forecast_not_numeric"}


async def test_pv_sensor_errors(berlin):
    hass = berlin
    set_sources(hass)
    hass.states.async_set("sensor.temperature", 21.0, {"unit_of_measurement": "°C"})
    result = await _to_settings(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pv_entities": ["sensor.temperature"], "update_interval": 60})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"pv_entities": "pv_unit"}

    hass.states.async_set(PV, 1500, {"unit_of_measurement": "W"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pv_entities": [PV], "update_interval": 60})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["pv_entities"] == [PV]


async def test_unavailable_entities_with_matching_metadata_are_accepted(berlin):
    hass = berlin
    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", "solcast_solar", "sc_today",
                            suggested_object_id="solcast_pv_forecast_forecast_today",
                            unit_of_measurement="kWh")
    reg.async_get_or_create("sensor", "inverter", "pv",
                            suggested_object_id="pv_energy", unit_of_measurement="kWh")
    hass.states.async_set(SC, "unavailable", {})
    hass.states.async_set(SC_TOM, 31.0, KWH)
    assert forecast_entity_error(hass, SC) is None
    assert pv_entity_error(hass, PV) is None               # noch kein Zustand, Registry passt
    hass.states.async_set(PV, "unavailable", {})
    assert pv_entity_error(hass, PV) is None

    reg.async_get_or_create("sensor", "other", "wh",
                            suggested_object_id="forecast_wh", unit_of_measurement="Wh")
    assert forecast_entity_error(hass, "sensor.forecast_wh") == "forecast_unit"


async def test_entity_checks(berlin):
    hass = berlin
    assert forecast_entity_error(hass, "sensor.nothing") == "entity_not_found"
    assert pv_entity_error(hass, "sensor.nothing") == "entity_not_found"
    hass.states.async_set("sensor.forecast", 12.5, {})
    assert forecast_entity_error(hass, "sensor.forecast") is None   # ohne Einheit: kWh
    for unit in ("kWh", "W", "kW"):
        hass.states.async_set("sensor.pv", 1.0, {"unit_of_measurement": unit})
        assert pv_entity_error(hass, "sensor.pv") is None, unit
    hass.states.async_set("sensor.pv", 1.0, {})
    assert pv_entity_error(hass, "sensor.pv") == "pv_unit"           # Energie oder Leistung?
    hass.states.async_set("sensor.pv", 1.0, {"unit_of_measurement": "Wh"})
    assert pv_entity_error(hass, "sensor.pv") == "pv_unit"
    hass.states.async_set("sensor.pv", "on", {"unit_of_measurement": "kWh"})
    assert pv_entity_error(hass, "sensor.pv") == "pv_not_numeric"
