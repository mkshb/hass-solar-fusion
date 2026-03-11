# Solar Fusion

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that **reads data from your already-installed solar forecast integrations** and combines them into a single, statistically optimised forecast — with no API calls of its own.

---

## How it works

Solar Fusion does **not** contact any external service. Instead, it reads the sensor entities that your existing forecast integrations (Forecast.Solar, Open-Meteo Solar, Solcast) have already created in your Home Assistant. It then:

1. Reads daily totals and hourly breakdowns from each source's entities
2. Tracks how accurate each source has been against your actual PV production
3. Calibrates forecasts using **isotonic regression** (seasonal, non-linear correction) or linear bias correction depending on available history
4. Combines the calibrated forecasts using **seasonally-weighted averaging** (better sources in the current season get higher weights)
5. Exposes the result as new HA sensors

All processing is local. No data leaves Home Assistant.

---

## Prerequisites

Install **at least one** of the following integrations first:

| Integration | Type | Link |
|-------------|------|------|
| **Forecast.Solar** | Built-in HA | Settings → Integrations → Add → "Forecast Solar" |
| **Open-Meteo Solar Forecast** | HACS | [rany2/ha-open-meteo-solar-forecast](https://github.com/rany2/ha-open-meteo-solar-forecast) |
| **Solcast PV Forecast** | HACS | [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) |

Solar Fusion is most useful when **two or more** sources are installed, but works with a single source too (bias correction and isotonic calibration still apply).

> **Multiple PV arrays?** Run Solar Fusion as separate instances — once per array, or once with a combined PV sensor. See [Multiple instances](#multiple-instances) below.

---

## Installation

1. In HACS → Integrations → ⋮ → Custom repositories:
   Add `https://github.com/yourusername/solar-fusion` as an **Integration**
2. Install **Solar Fusion** and restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration** → Search *Solar Fusion*

---

## Setup (3-step config flow)

### Step 1 – Name & sources
Give this instance a name (e.g. `Dach` or `Garage`) and select which forecast integrations to combine. Detected sources are pre-selected automatically.

### Step 2 – Confirm entity IDs
The default entity IDs used by each integration are pre-filled. Adjust only if you have renamed entities or run multiple instances of the same integration.

| Source | Default entity (today) | Default entity (tomorrow) |
|--------|------------------------|--------------------------|
| Forecast.Solar | `sensor.energy_production_today` | `sensor.energy_production_tomorrow` |
| Open-Meteo Solar | `sensor.energy_production_today` | `sensor.energy_production_tomorrow` |
| Solcast | `sensor.solcast_pv_forecast_forecast_today` | `sensor.solcast_pv_forecast_forecast_tomorrow` |

### Step 3 – Settings
- **PV production sensor(s)** *(optional)*: Select your actual generation sensor(s). Multiple sensors are supported and summed automatically (e.g. roof + garage). This enables accuracy tracking, adaptive weighting and isotonic calibration. Without it, equal weights are used permanently.
- **Update interval**: How often Solar Fusion re-reads the source entities (default: 60 min).

All settings can be changed later via **Settings → Devices & Services → Solar Fusion → Configure**.

---

## Sensors created

With an instance named `Dach`, sensors are named `Solar Fusion Dach – …`. Without a name, the prefix is simply `Solar Fusion –`.

| Sensor | Unit | Description |
|--------|------|-------------|
| `…Fused Today` | kWh | Calibrated, optimised forecast for today |
| `…Fused Tomorrow` | kWh | Calibrated, optimised forecast for tomorrow |
| `…Hourly Forecast` | kWh | Combined hourly breakdown (in attributes) |
| `…Forecast Uncertainty` | % | Disagreement between sources |
| `…<Source> RMSE` | kWh | Per-source RMSE, bias, MAE and calibration mode |
| `…Morning Snapshot` | — | 06:00 forecast snapshot used as RMSE reference |
| `…PV Tagesproduktion` | kWh | Built-in daily production meter (resets at midnight) |

### Key attributes (RMSE sensor)

```yaml
rmse_kwh: 1.24
mae_kwh: 0.98
bias_kwh: -0.31
days_evaluated: 12
calibration_mode: "isotonic (23 seasonal pts)"
weight: 0.63
today_kwh: 14.2
tomorrow_kwh: 11.8
```

`calibration_mode` shows which calibration is active:
- `isotonic (N seasonal pts)` — full non-linear seasonal calibration
- `linear_bias (N recent pts)` — simple multiplicative correction
- `none (insufficient data)` — no correction yet, more history needed

### Key attributes (Morning Snapshot sensor)

```yaml
state: "2026-03-11T06:00"
snapshot_taken: true
forecast_solar_kwh: 16.8
solcast_kwh: 12.55
history:
  "2026-03-11": {Forecast.Solar: 16.8, Solcast PV Forecast: 12.55}
  "2026-03-10": {Forecast.Solar: 14.2, Solcast PV Forecast: 11.9}
```

---

## Fusion algorithm

```
Every update interval:
  1. Read today/tomorrow kWh and hourly Wh from source HA entities

  Calibration per source (priority order):
  2a. Isotonic regression  — if ≥ 20 seasonal data points available
       Fits a monotone step curve to (forecast → actual) pairs.
       Seasonal window: current month ± 1 month across all years.
  2b. Linear bias correction — if ≥ 3 recent data points
       factor = mean(actual) / mean(forecast), capped at ±40%
  2c. No correction — insufficient history

  Weighting per source:
  3. Seasonal RMSE = RMSE over months within ±1 of current month
     weight_i = 1 / seasonal_RMSE_i
     Falls back to equal weights if any source has < 3 data points

  Fusion:
  4. Weighted average per hour slot
  5. Normalise hourly total to match weighted average of daily totals

  Uncertainty:
  6. Weighted standard deviation of source values
     expressed as % of the fused hourly mean

  Nightly (after midnight):
  7. Compare yesterday's 06:00 morning snapshot against actual production
  8. Store result in history for future calibration
```

After approximately **3 weeks** of history, seasonal weighting and isotonic calibration begin to activate. After **one full year**, seasonal calibration covers all months independently.

---

## Multiple instances

Solar Fusion supports multiple instances — one per array, or one combined instance.

**Scenario: two arrays with separate production sensors**

The recommended approach is a single combined instance using a [template sensor](https://www.home-assistant.io/integrations/template/) that sums both arrays, or by selecting both PV sensors directly in the settings step (Solar Fusion sums them automatically).

```yaml
# configuration.yaml – optional combined sensor
template:
  - sensor:
      - name: "PV Gesamt"
        unit_of_measurement: kWh
        state: >
          {{ states('sensor.pv_dach') | float(0)
           + states('sensor.pv_garage') | float(0) }}
```

Note: Solcast combines all configured rooftop sites into a single set of sensors. A separate Solar Fusion instance per array only makes sense if each array has its own Forecast.Solar or Open-Meteo instance configured with the correct azimuth and tilt.

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
    entity_id: sensor.solar_fusion_fused_tomorrow
    below: 10
  action:
    service: switch.turn_on
    target:
      entity_id: switch.ev_charger
```

---

## Data flow

```
Forecast.Solar entities   ──┐
Open-Meteo Solar entities  ─┼──► Solar Fusion ──► Fused sensors (calibrated)
Solcast entities           ──┘         │
                                       ├──► RMSE / uncertainty sensors
                                       └──► Morning snapshot sensor

Actual PV sensor(s) (opt.) ──────────────► Nightly accuracy recording
                                           → isotonic regression update
                                           → seasonal weight update
```

---

## Requirements

- Home Assistant 2023.6 or newer
- The `recorder` integration (enabled by default)
- At least one supported solar forecast integration installed and providing data

---

## License

MIT
