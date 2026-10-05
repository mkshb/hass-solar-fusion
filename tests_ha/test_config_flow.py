"""Config-Flow (Ersteinrichtung) und Options-Flow."""
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from common import DOMAIN, OM, OM_TOM, PV, SC, SC_TOM, make_entry, set_sources, setup_entry
from custom_components.solar_fusion.const import OPTION_KEYS


def _register_sources(hass):
    """Quellen so anlegen, wie die Integrationen sie in der Registry eintragen."""
    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", "solcast_solar", "sc_today",
                            suggested_object_id="solcast_pv_forecast_forecast_today")
    reg.async_get_or_create("sensor", "open_meteo_solar_forecast", "om_today",
                            suggested_object_id="om_energy_production_today")
    set_sources(hass)
    hass.states.async_set(PV, 12.3, {"unit_of_measurement": "kWh",
                                     "state_class": "total_increasing"})


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
    assert "update_interval" not in data
    assert result["options"] == {"update_interval": 30}
    assert result["result"].minor_version == 2


async def test_user_flow_requires_a_source(berlin):
    hass = berlin
    _, result = await _user_step(hass, [])
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_sources"}


def _defaults(result):
    return {str(k): k.default() for k in result["data_schema"].schema if callable(k.default)}


async def test_options_flow_updates_options_and_reloads(berlin):
    hass = berlin
    set_sources(hass)
    entry, coord = await setup_entry(hass)
    data_before = dict(entry.data)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    defaults = _defaults(result)
    assert defaults["update_interval"] == 60
    assert defaults["shading_learn"] is False and defaults["shading_apply"] is True
    assert defaults["horizon_sources"] == []
    assert "sources" not in defaults and "pv_entities" not in defaults

    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "update_interval": 30, "exclusion_factor": 2.5, "min_eval_days": 5,
        "shading_learn": True, "shading_apply": False,
        "horizon_sources": ["open_meteo_solar_forecast"],
    })
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    assert entry.options == {
        "update_interval": 30, "exclusion_factor": 2.5, "min_eval_days": 5,
        "shading_learn": True, "shading_apply": False,
        "horizon_sources": ["open_meteo_solar_forecast"],
    }
    assert dict(entry.data) == data_before
    # Neu geladen: neuer Coordinator mit den neuen Einstellungen
    new = entry.runtime_data
    assert new is not coord
    assert new.update_interval.total_seconds() == 30 * 60
    assert new.shading_settings["learn"] is True
    assert new.shading_settings["horizon_sources"] == ["open_meteo_solar_forecast"]


async def test_options_flow_offers_only_configured_sources_as_horizon(berlin):
    hass = berlin
    set_sources(hass)
    entry, _ = await setup_entry(hass, sources=["solcast"],
                                 entity_map={"solcast": {"today": SC, "tomorrow": SC_TOM}})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"horizon_sources": ["open_meteo_solar_forecast"]})


async def _reconfigure(hass, entry):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id})


async def test_reconfigure_changes_sources_entities_and_pv(berlin):
    hass = berlin
    _register_sources(hass)
    entry, coord = await setup_entry(hass, horizon_sources=["open_meteo_solar_forecast"],
                                     shading_learn=True)

    result = await _reconfigure(hass, entry)
    assert result["step_id"] == "reconfigure"
    defaults = _defaults(result)
    assert defaults["instance_name"] == "Test"
    assert defaults["sources"] == ["open_meteo_solar_forecast", "solcast"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sources": ["solcast"], "instance_name": "Dach"})
    assert result["step_id"] == "entities"
    assert _defaults(result) == {"solcast_today": SC, "solcast_tomorrow": SC_TOM}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"solcast_today": SC, "solcast_tomorrow": SC_TOM})
    assert result["step_id"] == "settings"
    assert _defaults(result) == {"pv_entities": [PV]}  # kein Intervall: das ist eine Option
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pv_entities": []})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Solar Fusion – Dach"
    assert entry.data["sources"] == ["solcast"]
    assert entry.data["entity_map"] == {"solcast": {"today": SC, "tomorrow": SC_TOM}}
    assert entry.data["pv_entities"] == [] and entry.data["pv_entity"] == ""
    # Optionen bleiben, Horizont-Quellen nur noch aus den gewählten Quellen
    assert entry.options["shading_learn"] is True
    assert entry.options["horizon_sources"] == []
    assert entry.runtime_data is not coord
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_legacy_entry_is_migrated_to_options(berlin):
    hass = berlin
    set_sources(hass)
    legacy = make_entry()
    legacy = MockConfigEntry(
        domain=DOMAIN, entry_id=legacy.entry_id, title=legacy.title, version=1,
        data={**legacy.data, "update_interval": 30, "exclusion_factor": 2.5,
              "shading_learn": True, "horizon_sources": ["solcast"]},
    )
    legacy.add_to_hass(hass)
    assert await hass.config_entries.async_setup(legacy.entry_id)
    await hass.async_block_till_done()

    assert legacy.state is ConfigEntryState.LOADED
    assert legacy.minor_version == 2
    assert legacy.options == {"update_interval": 30, "exclusion_factor": 2.5,
                              "shading_learn": True, "horizon_sources": ["solcast"]}
    assert not set(OPTION_KEYS) & set(legacy.data)
    assert legacy.data["entity_map"]["solcast"] == {"today": SC, "tomorrow": SC_TOM}
    # Nicht gesetzte Werte bleiben auf ihrem Standard
    coord = legacy.runtime_data
    assert coord.update_interval.total_seconds() == 30 * 60
    assert coord.shading_settings["learn"] is True and coord.shading_settings["apply"] is True


async def test_entry_from_newer_version_is_not_loaded(berlin):
    hass = berlin
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def _complete_user_flow(hass, sources, name, entity_map, pv):
    _, result = await _user_step(hass, sources, name)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], entity_map)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pv_entities": pv, "update_interval": 30})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return result["result"]


async def test_second_user_flow_leaves_first_entry_alone(berlin):
    hass = berlin
    _register_sources(hass)
    first = await _complete_user_flow(
        hass, ["solcast"], "Dach", {"solcast_today": SC, "solcast_tomorrow": SC_TOM}, [PV])
    await _complete_user_flow(
        hass, ["open_meteo_solar_forecast"], "Garage",
        {"open_meteo_solar_forecast_today": OM, "open_meteo_solar_forecast_tomorrow": OM_TOM}, [])

    assert first.data["instance_name"] == "Dach"
    assert first.data["sources"] == ["solcast"]
    assert first.data["pv_entities"] == [PV]
