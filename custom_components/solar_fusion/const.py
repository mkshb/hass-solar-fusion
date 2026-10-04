"""Constants for Solar Fusion."""

DOMAIN = "solar_fusion"

# ──────────────────────────────────────────────────────────────────────────────
# Config entry keys (user-configured)
# ──────────────────────────────────────────────────────────────────────────────
CONF_SOURCES = "sources"            # list[str] – selected source IDs
CONF_PV_ENTITY = "pv_entity"        # entity_id of actual PV production sensor (single, legacy)
CONF_PV_ENTITIES = "pv_entities"    # list[str] – multiple PV sensors to sum
CONF_UPDATE_INTERVAL = "update_interval"  # minutes
CONF_INSTANCE_NAME = "instance_name"      # user-defined name for this entry (e.g. "Dach")
CONF_EXCLUSION_FACTOR = "exclusion_factor"  # k: Ausschluss ab k × RMSE der besten Quelle
CONF_MIN_EVAL_DAYS = "min_eval_days"        # Mindestzahl ausgewerteter Tage für die Gewichtung
CONF_SHADING_LEARN = "shading_learn"        # Verschattung aus Stunden-Ist lernen
CONF_SHADING_APPLY = "shading_apply"        # gelernte Verschattung auf die Prognose anwenden
CONF_HORIZON_SOURCES = "horizon_sources"    # list[str] – Quellen, die schon einen Horizont enthalten

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
# Die Sensoren der Core-Integration haben keine Stundenattribute; "wh_hours"
# gibt es nur über die Energie-Plattform. Liefert eine (z. B. per Template
# nachgebaute) Entität trotzdem "wh_hours", bezeichnet der Schlüssel wie in
# der Forecast.Solar-API das ENDE der Periode ("value is always for the period
# from last timestamp to the timestamp in the key", doc.forecast.solar).
FORECAST_SOLAR_TODAY = "sensor.energy_production_today"
FORECAST_SOLAR_TOMORROW = "sensor.energy_production_tomorrow"
FORECAST_SOLAR_ATTR_HOURLY = "wh_hours"

# Open-Meteo Solar Forecast (HACS, domain: open_meteo_solar_forecast)
# Same sensor naming as forecast_solar (it was forked from it)
# Stundenwerte stehen im Attribut "wh_period" (Schlüssel = BEGINN der Stunde,
# siehe open_meteo_solar_forecast: Viertelstunden [t, t+15 min) werden auf die
# volle Stunde abgerundet). "wh_hours" gibt es dort nur in der Energie-Plattform.
OPEN_METEO_TODAY = "sensor.energy_production_today"
OPEN_METEO_TOMORROW = "sensor.energy_production_tomorrow"
OPEN_METEO_ATTR_HOURLY = "wh_period"
OPEN_METEO_ATTR_HOURLY_LEGACY = "wh_hours"

# Solcast PV Forecast (HACS, domain: solcast_solar)
# Sensors: sensor.solcast_pv_forecast_forecast_today / _forecast_tomorrow
# Hourly attribute: "detailedHourly" list of {period_start, pv_estimate (kWh)}
# Both today- and tomorrow-sensors expose their own detailedHourly attribute.
SOLCAST_TODAY = "sensor.solcast_pv_forecast_forecast_today"
SOLCAST_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"
SOLCAST_ATTR_DETAILED_TODAY    = "detailedHourly"   # on today-sensor:    list[{period_start, pv_estimate}]
SOLCAST_ATTR_DETAILED_TOMORROW = "detailedHourly"   # on tomorrow-sensor: list[{period_start, pv_estimate}]
SOLCAST_ATTR_ESTIMATE = "pv_estimate"               # kWh per slot
SOLCAST_ATTR_PERIOD_START = "period_start"

# ──────────────────────────────────────────────────────────────────────────────
# Storage / fusion parameters
# ──────────────────────────────────────────────────────────────────────────────
# Version 2: Morgen-Snapshot je Tag als {"daily", "daily_corrected", "hourly"}
# statt {source: kWh}; dazu Stundendaten und Karte für die Verschattung.
STORAGE_VERSION = 2
STORAGE_KEY = f"{DOMAIN}_history"

DEFAULT_SHADING_LEARN = False
DEFAULT_SHADING_APPLY = True
# Wie lange Stunden-Ist und Stundenprognose je Tag für das Lernen aufbewahrt
# werden. Etwas mehr als ein Jahr, damit jeder Sonnenstand einmal vorkommt.
SHADING_RETENTION_DAYS = 400

DEFAULT_UPDATE_INTERVAL = 60          # minutes

# Minimum days of history before adaptive weighting kicks in
MIN_HISTORY_DAYS = 3
# Rolling window for RMSE/bias calculation
HISTORY_WINDOW_DAYS = 14