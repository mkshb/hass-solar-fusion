"""Config-Flow (Ersteinrichtung) und Options-Flow."""
import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er

from common import DOMAIN, OM, OM_TOM, PV, SC, SC_TOM, set_sources, setup_entry


def _register_sources(hass):
    """Quellen so anlegen, wie die Integrationen sie in der Registry eintragen."""
    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", "solcast_solar", "sc_today",
                            suggested_object_id="solcast_pv_forecast_forecast_today")
    reg.async_get_or_create("sensor", "open_meteo_solar_forecast", "om_today",
                            suggested_object_id="om_energy_production_today")
    set_sources(hass)


async def _user_step(hass, sources, name="Dach"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return result, await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sources": sources, "instance_name": name})


async def test_user_flow_creates_entry(berlin):
    hass = berlin
    _register_sources(hass)
    first, result = await _user_step(hass, ["open_meteo_solar_forecast", "solcast"])
    assert "Open-Meteo" in first["description_placeholders"]["detected"]
    assert "Solcast" in first["description_placeholders"]["detected"]
    assert result["step_id"] == "entities"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "open_meteo_solar_forecast_today": OM, "open_meteo_solar_forecast_tomorrow": OM_TOM,
        "solcast_today": SC, "solcast_tomorrow": SC_TOM,
    })
    assert result["step_id"] == "settings"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pv_entities": [PV], "update_interval": 30})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Solar Fusion – Dach"
    data = result["data"]
    assert data["sources"] == ["open_meteo_solar_forecast", "solcast"]
    assert data["entity_map"]["solcast"] == {"today": SC, "tomorrow": SC_TOM}
    assert data["pv_entities"] == [PV] and data["pv_entity"] == PV
    assert data["update_interval"] == 30


async def test_user_flow_requires_a_source(berlin):
    hass = berlin
    _, result = await _user_step(hass, [])
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_sources"}


async def test_options_flow_updates_entry_and_reloads(berlin):
    hass = berlin
    set_sources(hass)
    entry, coord = await setup_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"sources": ["open_meteo_solar_forecast", "solcast"],
                            "instance_name": "Dach"})
    assert result["step_id"] == "entities"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "open_meteo_solar_forecast_today": OM, "open_meteo_solar_forecast_tomorrow": OM_TOM,
        "solcast_today": SC, "solcast_tomorrow": SC_TOM,
    })
    assert result["step_id"] == "settings"
    # Vorbelegung aus der bisherigen Konfiguration
    defaults = {str(k): k.default() for k in result["data_schema"].schema if callable(k.default)}
    assert defaults["pv_entities"] == [PV]
    assert defaults["shading_learn"] is False and defaults["shading_apply"] is True
    assert defaults["horizon_sources"] == []

    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "pv_entities": [PV], "update_interval": 60, "exclusion_factor": 2.5,
        "min_eval_days": 5, "shading_learn": True, "shading_apply": False,
        "horizon_sources": ["open_meteo_solar_forecast"],
    })
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    assert entry.title == "Solar Fusion – Dach"
    assert entry.data["shading_learn"] is True
    assert entry.data["shading_apply"] is False
    assert entry.data["horizon_sources"] == ["open_meteo_solar_forecast"]
    assert entry.data["exclusion_factor"] == 2.5 and entry.data["min_eval_days"] == 5
    # Neu geladen: neuer Coordinator mit den neuen Einstellungen
    new = hass.data[DOMAIN][entry.entry_id]
    assert new is not coord
    assert new.shading_settings["learn"] is True
    assert new.shading_settings["horizon_sources"] == ["open_meteo_solar_forecast"]


async def test_options_flow_limits_horizon_sources_to_selected(berlin):
    hass = berlin
    set_sources(hass)
    entry, _ = await setup_entry(hass, horizon_sources=["open_meteo_solar_forecast"])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"sources": ["solcast"], "instance_name": ""})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solcast_today": SC, "solcast_tomorrow": SC_TOM})
    defaults = {str(k): k.default() for k in result["data_schema"].schema if callable(k.default)}
    assert defaults["horizon_sources"] == []
    settings = {"pv_entities": [PV], "update_interval": 60, "exclusion_factor": 2.0,
                "min_eval_days": 7, "shading_learn": False, "shading_apply": True}
    # Nicht mehr gewählte Quelle lässt schon das Formular nicht zu
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**settings, "horizon_sources": ["open_meteo_solar_forecast"]})
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**settings, "horizon_sources": []})
    await hass.async_block_till_done()
    assert entry.data["sources"] == ["solcast"]
    assert entry.data["horizon_sources"] == []
    assert entry.title == "Solar Fusion"
