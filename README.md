# Solar Fusion

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that **reads data from your already-installed Solar integrations** and combines them into a single, statistically optimised forecast — with no API calls of its own.

---

## How it works

Solar Fusion does **not** contact any external service. Instead, it reads the sensor entities that your existing forecast integrations (Forecast.Solar, Open-Meteo Solar, Solcast) have already created in your Home Assistant. It then:

1. Reads the daily totals and hourly breakdowns from each source's entities
2. Tracks how accurate each source has been against your actual PV production
3. Combines the forecasts using adaptive weighted averaging (better sources get higher weights)
4. Applies per-source bias correction to remove systematic over/under-estimation
5. Exposes the result as new HA sensors

---

## Prerequisites

Install **at least one** of the following integrations first:

| Integration | Type | Link |
|-------------|------|------|
| **Forecast.Solar** | Built-in HA | Settings → Integrations → Add → "Forecast Solar" |
| **Open-Meteo Solar** | HACS | [rany2/ha-open-meteo-solar-forecast](https://github.com/rany2/ha-open-meteo-solar-forecast) |
| **Solcast PV Forecast** | HACS | [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) |

Solar Fusion is most useful when **two or more** sources are installed, but works with a single source too (in that case it still applies bias correction).

---

## Installation

1. In HACS → Integrations → ⋮ → Custom repositories:
   Add `https://github.com/yourusername/solar-forecast-fusion` as an **Integration**
2. Install **Solar Fusion** and restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration** → Search *Solar Fusion*

---

## Setup (3-step config flow)

### Step 1 – Select sources
The integration scans your HA state machine and **auto-detects** which forecast integrations are active. Detected sources are pre-selected. You can deselect any you don't want included.

### Step 2 – Confirm entity IDs
The default entity IDs used by each integration are pre-filled. If you have renamed entities or run multiple instances, adjust them here.

| Source | Default entity (today) | Default entity (tomorrow) |
|--------|------------------------|--------------------------|
| Forecast.Solar | `sensor.energy_production_today` | `sensor.energy_production_tomorrow` |
| Open-Meteo Solar | `sensor.energy_production_today` | `sensor.energy_production_tomorrow` |
| Solcast | `sensor.solcast_pv_forecast_forecast_today` | `sensor.solcast_pv_forecast_forecast_tomorrow` |

### Step 3 – Settings
- **PV production sensor** *(optional)*: Select your actual generation sensor (kWh). This enables historical accuracy tracking and adaptive weighting. Without it, equal weights are used permanently.
- **Update interval**: How often Solar Fusion re-reads the source entities (default: 60 min).

---

## Sensors created

| Sensor | Unit | Description |
|--------|------|-------------|
| `sensor.solar_forecast_fusion_fused_today` | kWh | Optimised forecast for today |
| `sensor.solar_forecast_fusion_fused_tomorrow` | kWh | Optimised forecast for tomorrow |
| `sensor.solar_forecast_fusion_hourly_forecast` | kWh | Combined hourly breakdown (in attributes) |
| `sensor.solar_forecast_fusion_forecast_uncertainty` | % | Disagreement between sources |
| `sensor.solar_forecast_fusion_<source>_rmse` | kWh | Per-source RMSE over last 14 days |

### Key attributes (Fused Today / Tomorrow)

```yaml
source_weights:
  Forecast.Solar: 0.52
  Open-Meteo Solar: 0.31
  Solcast PV Forecast: 0.17
source_values_kwh:
  Forecast.Solar: 18.4
  Open-Meteo Solar: 15.1
  Solcast PV Forecast: 16.8
hourly_forecast_wh:
  "2024-07-15T07:00": 320.0
  "2024-07-15T08:00": 1100.0
  ...
active_sources: ["Forecast.Solar", "Open-Meteo Solar"]
missing_sources: []
```

---

## Fusion algorithm

```
For each source (at every update interval):
  1. Read today/tomorrow kWh and hourly Wh from its HA entities
  2. Apply bias correction factor = mean(actual) / mean(forecast)
     over last 14 days (capped at ±40 %)

For each hour slot:
  3. Weighted average: weight_i = 1 / RMSE_i
     (equal weights when fewer than 3 days of history available)
  4. Normalise weights to sum to 1

Uncertainty metric:
  5. Weighted standard deviation of source values
     expressed as % of fused hourly mean
```

After approximately **2 weeks** of history, the weights stabilise and reflect each source's actual performance for your location and system.

---

## Use in automations

```yaml
# Charge EV overnight only if tomorrow looks weak
automation:
  alias: "EV charge if tomorrow < 10 kWh solar"
  trigger:
    platform: time
    at: "22:00:00"
  condition:
    condition: numeric_state
    entity_id: sensor.solar_forecast_fusion_fused_tomorrow
    below: 10
  action:
    service: switch.turn_on
    target:
      entity_id: switch.ev_charger
```

---

## Data flow

```
Forecast.Solar entities  ──┐
Open-Meteo Solar entities ─┼──► Solar Fusion ──► Fused sensors
Solcast entities          ──┘         │
                                      └──► Quality / uncertainty sensors
Actual PV sensor (optional) ─────────────► Adaptive weight update (nightly)
```

All processing is local. No data leaves Home Assistant.

---

## Requirements

- Home Assistant 2023.6 or newer
- The `recorder` integration (enabled by default)
- At least one supported Solar integration installed and providing data

---

## License

MIT
