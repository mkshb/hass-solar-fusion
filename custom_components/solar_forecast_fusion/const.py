"""Constants for Solar Forecast Fusion."""

DOMAIN = "solar_forecast_fusion"

# ──────────────────────────────────────────────────────────────────────────────
# Config entry keys (user-configured)
# ──────────────────────────────────────────────────────────────────────────────
CONF_SOURCES = "sources"            # list[str] – selected source IDs
CONF_PV_ENTITY = "pv_entity"        # entity_id of actual PV production sensor
CONF_UPDATE_INTERVAL = "update_interval"  # minutes

# ──────────────────────────────────────────────────────────────────────────────
# Source identifiers  (one per supported upstream HA integration)
# ──────────────────────────────────────────────────────────────────────────────
SOURCE_FORECAST_SOLAR = "forecast_solar"
SOURCE_OPEN_METEO = "open_meteo_solar_forecast"
SOURCE_SOLCAST = "solcast"

ALL_SOURCES = [SOURCE_FORECAST_SOLAR, SOURCE_OPEN_METEO, SOURCE_SOLCAST]

SOURCE_NAMES = {
    SOURCE_FORECAST_SOLAR: "Forecast.Solar",
    SOURCE_OPEN_METEO: "Open-Meteo Solar Forecast",
    SOURCE_SOLCAST: "Solcast PV Forecast",
}

SOURCE_DOCS = {
    SOURCE_FORECAST_SOLAR: "https://www.home-assistant.io/integrations/forecast_solar/",
    SOURCE_OPEN_METEO: "https://github.com/rany2/ha-open-meteo-solar-forecast",
    SOURCE_SOLCAST: "https://github.com/BJReplay/ha-solcast-solar",
}

# ──────────────────────────────────────────────────────────────────────────────
# Known entity patterns per source
#
# Each source exposes its forecast data in a slightly different way.
# We support reading:
#   • daily totals (today / tomorrow) – simple numeric sensor state
#   • hourly breakdown – stored as a sensor *attribute* (dict or list)
#
# Entity IDs below use the HA-default naming; users can override them in the
# config flow if their installation uses a custom prefix.
# ──────────────────────────────────────────────────────────────────────────────

# Forecast.Solar (built-in HA integration, domain: forecast_solar)
# Entities: sensor.energy_production_today / _tomorrow
# Hourly attribute: "watts" dict {ISO-hour: W} on sensor.power_production_now
# The integration also exposes "wh_hours" attribute on the today/tomorrow sensors.
FORECAST_SOLAR_TODAY = "sensor.energy_production_today"
FORECAST_SOLAR_TOMORROW = "sensor.energy_production_tomorrow"
# Attribute on today/tomorrow sensors that holds hourly breakdown {ts: Wh}
FORECAST_SOLAR_ATTR_HOURLY = "wh_hours"

# Open-Meteo Solar Forecast (HACS, domain: open_meteo_solar_forecast)
# Same sensor naming as forecast_solar (it was forked from it)
OPEN_METEO_TODAY = "sensor.energy_production_today"
OPEN_METEO_TOMORROW = "sensor.energy_production_tomorrow"
OPEN_METEO_ATTR_HOURLY = "wh_hours"

# Solcast PV Forecast (HACS, domain: solcast_solar)
# Sensors: sensor.solcast_pv_forecast_forecast_today / _forecast_tomorrow
# Hourly attribute: "detailedForecast" list of {period_start, pv_estimate (kWh)}
SOLCAST_TODAY = "sensor.solcast_pv_forecast_forecast_today"
SOLCAST_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"
SOLCAST_ATTR_DETAILED = "detailedForecast"          # list[{period_start, pv_estimate}]
SOLCAST_ATTR_ESTIMATE = "pv_estimate"               # kWh per slot
SOLCAST_ATTR_PERIOD_START = "period_start"

# ──────────────────────────────────────────────────────────────────────────────
# Storage / fusion parameters
# ──────────────────────────────────────────────────────────────────────────────
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_history"

DEFAULT_UPDATE_INTERVAL = 60          # minutes

# Minimum days of history before adaptive weighting kicks in
MIN_HISTORY_DAYS = 3
# Rolling window for RMSE/bias calculation
HISTORY_WINDOW_DAYS = 14
