"""Quellen finden und lesen über die Entity-Registry (source_reader)."""
import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.solar_fusion.source_reader import (
    SourceUnavailable,
    detect_available_sources,
    read_source,
    today_entity_id,
)

KWH = {"unit_of_measurement": "kWh"}


def _reg(hass, platform, object_id, state=None, attrs=None):
    entry = er.async_get(hass).async_get_or_create(
        "sensor", platform, object_id, suggested_object_id=object_id)
    if state is not None:
        hass.states.async_set(entry.entity_id, state, {**KWH, **(attrs or {})})
    return entry.entity_id


async def test_detects_sources_with_valid_states(hass):
    _reg(hass, "forecast_solar", "energy_production_today", 20.0)
    _reg(hass, "open_meteo_solar_forecast", "om_energy_production_today", "unavailable")
    _reg(hass, "solcast_solar", "solcast_pv_forecast_forecast_today", 30.0)
    assert detect_available_sources(hass) == ["forecast_solar", "solcast"]


async def test_open_meteo_and_forecast_solar_share_the_default_name(berlin):
    hass = berlin
    # Forecast.Solar bekommt den Standardnamen, Open-Meteo den mit Suffix
    fs_today = _reg(hass, "forecast_solar", "energy_production_today", 20.0)
    fs_tom = _reg(hass, "forecast_solar", "energy_production_tomorrow", 21.0)
    om_today = er.async_get(hass).async_get_or_create(
        "sensor", "open_meteo_solar_forecast", "om_today",
        suggested_object_id="energy_production_today",
        translation_key="energy_production_today").entity_id
    om_tom = er.async_get(hass).async_get_or_create(
        "sensor", "open_meteo_solar_forecast", "om_tomorrow",
        suggested_object_id="energy_production_tomorrow",
        translation_key="energy_production_tomorrow").entity_id
    assert om_today == "sensor.energy_production_today_2"
    hass.states.async_set(om_today, 38.6, {**KWH, "wh_period": {
        "2026-10-04T12:00:00+02:00": 5000}})
    hass.states.async_set(om_tom, 44.0, KWH)
    default_map = {"today": fs_today, "tomorrow": fs_tom}   # Standard aus dem Config-Flow

    om = read_source(hass, "open_meteo_solar_forecast", default_map)
    fs = read_source(hass, "forecast_solar", default_map)
    assert (om.today_kwh, om.tomorrow_kwh) == (38.6, 44.0)
    assert om.hourly_today == {"2026-10-04T12:00": 5000.0}
    assert (fs.today_kwh, fs.tomorrow_kwh) == (20.0, 21.0)
    assert today_entity_id(hass, "open_meteo_solar_forecast", default_map) == om_today


async def test_open_meteo_suffix_without_translation_key(hass):
    # Ältere Registry-Einträge ohne translation_key: Suffix "_2" im Entity-ID
    _reg(hass, "forecast_solar", "energy_production_today", 20.0)
    om_today = er.async_get(hass).async_get_or_create(
        "sensor", "open_meteo_solar_forecast", "om_today",
        suggested_object_id="energy_production_today").entity_id
    _reg(hass, "open_meteo_solar_forecast", "energy_production_today_remaining", 0.4)
    assert om_today == "sensor.energy_production_today_2"
    hass.states.async_set(om_today, 38.6, KWH)
    assert today_entity_id(hass, "open_meteo_solar_forecast", {}) == om_today


async def test_open_meteo_explicit_override_wins(hass):
    _reg(hass, "open_meteo_solar_forecast", "energy_production_today", 38.6)
    _reg(hass, "open_meteo_solar_forecast", "energy_production_tomorrow", 44.0)
    hass.states.async_set("sensor.custom_om_today", 12.0, KWH)
    reading = read_source(hass, "open_meteo_solar_forecast",
                          {"today": "sensor.custom_om_today"})
    assert reading.today_kwh == 12.0 and reading.tomorrow_kwh == 44.0


async def test_solcast_german_names_and_no_remaining_sensor(berlin):
    hass = berlin
    # „verbleibende … heute“ zählt herunter und darf nicht als Prognose gelten
    _reg(hass, "solcast_solar", "solcast_pv_forecast_prognose_verbleibende_leistung_heute", 1.5)
    _reg(hass, "solcast_solar", "solcast_pv_forecast_prognose_heute", 41.25, {
        "detailedHourly": [{"period_start": "2026-10-04T12:00:00+02:00", "pv_estimate": 4.9}]})
    _reg(hass, "solcast_solar", "solcast_pv_forecast_prognose_morgen", 41.45)
    reading = read_source(hass, "solcast", {})
    assert (reading.today_kwh, reading.tomorrow_kwh) == (41.25, 41.45)
    assert reading.hourly_today == {"2026-10-04T12:00": pytest.approx(4900.0)}
    assert today_entity_id(hass, "solcast", {}) == "sensor.solcast_pv_forecast_prognose_heute"


async def test_missing_or_unavailable_source_raises(hass):
    with pytest.raises(SourceUnavailable, match="not found"):
        read_source(hass, "solcast", {"today": "sensor.nope", "tomorrow": "sensor.nope2"})
    hass.states.async_set("sensor.sc_today", "unavailable")
    hass.states.async_set("sensor.sc_tomorrow", 10.0)
    with pytest.raises(SourceUnavailable, match="unavailable"):
        read_source(hass, "solcast", {"today": "sensor.sc_today", "tomorrow": "sensor.sc_tomorrow"})
    hass.states.async_set("sensor.sc_today", "abc")
    with pytest.raises(SourceUnavailable, match="Cannot parse"):
        read_source(hass, "solcast", {"today": "sensor.sc_today", "tomorrow": "sensor.sc_tomorrow"})
    with pytest.raises(SourceUnavailable, match="Unknown source"):
        read_source(hass, "other", {})
