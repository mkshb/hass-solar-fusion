# Solar Fusion

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that **reads data from your already-installed solar forecast integrations** and combines them into a single, statistically optimised forecast — with no API calls of its own.

---

## How it works

Solar Fusion does **not** contact any external service. Instead, it reads the sensor entities that your existing forecast integrations (Forecast.Solar, Open-Meteo Solar, Solcast) have already created in your Home Assistant. It then:

1. Reads daily totals **and hourly breakdowns** from each source's entities
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

**Solcast entity discovery:** Solar Fusion automatically searches the HA entity registry for Solcast sensors, including localised names (e.g. German: `prognose_heute` / `prognose_morgen`). Manual overrides are only needed in unusual setups.

### Step 3 – Settings
- **PV production sensor(s)** *(optional)*: Select your actual generation sensor(s). Multiple sensors are supported and summed automatically (e.g. roof + garage). This enables accuracy tracking, adaptive weighting and isotonic calibration. Without it, equal weights are used permanently.
- **Update interval**: How often Solar Fusion re-reads the source entities (default: 60 min).

All settings can be changed later via **Settings → Devices & Services → Solar Fusion → Configure**.

---

## Sensors created

With an instance named `Dach`, sensors are named `Solar Fusion Dach – …`. Without a name, the prefix is simply `Solar Fusion –`. All sensors belonging to an instance are grouped under a single **device** entry in HA (model: *Adaptive Ensemble Forecaster*).

| Sensor | Unit | Description |
|--------|------|-------------|
| `… Forecast – Today` | kWh | Calibrated, optimised forecast for today |
| `… Forecast – Tomorrow` | kWh | Calibrated, optimised forecast for tomorrow |
| `… Forecast – Hourly` | kWh | Combined today+tomorrow hourly breakdown (in attributes) |
| `… Forecast – Uncertainty` | % | Disagreement between sources as % of hourly mean |
| `… Quality – <Source>` | kWh | Per-source accuracy metrics and calibration status |
| `… Diagnostics – Morning Snapshot` | — | 06:00 forecast snapshot used as RMSE reference |
| `… Diagnostics – PV Daily Production` | kWh | Built-in daily production meter (resets at midnight) |

---

### Forecast – Today / Forecast – Tomorrow

State: calibrated fused daily total in kWh.

Key attributes:

```yaml
source_weights:
  Forecast.Solar: 0.412
  Open-Meteo Solar Forecast: 0.271
  Solcast PV Forecast: 0.317
source_values_kwh:
  Forecast.Solar: 18.4
  Open-Meteo Solar Forecast: 17.1
  Solcast PV Forecast: 19.2
hourly_forecast_wh:
  "2026-03-12T06:00": 28.0
  "2026-03-12T07:00": 165.6
  ...
active_sources: [Forecast.Solar, Solcast PV Forecast]
missing_sources: [Open-Meteo Solar Forecast]
last_updated: "2026-03-11T14:00:00"
```

---

### Forecast – Hourly

State: combined today + tomorrow total in kWh.

Key attributes:

```yaml
forecast:
  "2026-03-11T08:00": 694.0
  "2026-03-11T09:00": 2374.0
  ...
  "2026-03-12T08:00": 521.0
  ...
today_kwh: 32.4
tomorrow_kwh: 28.1
```

The `forecast` attribute contains both days in a single dict, keyed by ISO hour strings, making it directly usable in ApexCharts or template cards.

---

### Forecast – Uncertainty

State: weighted standard deviation of sources as % of the fused hourly mean.

| Range | Interpretation |
|-------|---------------|
| < 10 % | Low – sources agree well |
| 10–25 % | Moderate – some disagreement |
| 25–50 % | High – sources diverge significantly |
| ≥ 50 % | Very high – forecast unreliable |

Key attributes:

```yaml
interpretation: "Low – sources agree well"
source_weights:
  Forecast.Solar: 0.412
  Open-Meteo Solar Forecast: 0.271
  Solcast PV Forecast: 0.317
```

---

### Quality – &lt;Source&gt; (one per source)

State: RMSE in kWh (root mean square error of daily forecast vs. actual production).

> **Note:** The `quality_label` attribute only appears once sufficient history has been collected (≥ 3 days). Before that, `rmse_kwh` is `null` and no label is shown.

| Quality label | RMSE range |
|---------------|-----------|
| Excellent | < 0.5 kWh |
| Good | 0.5–1.0 kWh |
| Fair | 1.0–2.0 kWh |
| Poor | ≥ 2.0 kWh |

Key attributes:

```yaml
rmse_kwh: 1.24
mae_kwh: 0.98
bias_kwh: -0.31        # negative = source consistently over-forecasts
days_evaluated: 12
calibration_mode: "isotonic (23 seasonal pts)"
weight: 0.63
today_kwh: 14.2        # raw (uncalibrated) value from this source
tomorrow_kwh: 11.8
quality_label: "Fair"
```

`calibration_mode` shows which calibration is active:

| Mode | Condition |
|------|-----------|
| `isotonic (N seasonal pts)` | ≥ 20 seasonal records — full non-linear seasonal calibration |
| `linear_bias (N recent pts)` | ≥ 3 recent records — multiplicative correction capped at ±40 % |
| `none (insufficient data)` | < 3 records — no correction yet, more history needed |

---

### Diagnostics – Morning Snapshot

State: ISO timestamp of today's 06:00 snapshot (`2026-03-11T06:00`), or `pending` if not yet taken.

At **06:00 every day**, Solar Fusion records the raw (uncalibrated) `today_kwh` value from every active source. This frozen value — not the continuously-updated intraday reading — is later used as the reference forecast when calculating RMSE after midnight. This prevents the "cheating effect" where providers silently refine their same-day forecast throughout the day.

If Home Assistant starts **after 06:00** and no snapshot exists yet for today, one is taken on the first data update.

Snapshots are retained for **30 days** and persisted across HA restarts.

Key attributes:

```yaml
snapshot_taken: true
snapshot_time: "2026-03-11T06:00"
forecast_solar_kwh: 16.8
solcast_pv_forecast_kwh: 12.55
open-meteo_solar_forecast_kwh: 15.3
history:
  "2026-03-11": {Forecast.Solar: 16.8, Solcast PV Forecast: 12.55, Open-Meteo Solar Forecast: 15.3}
  "2026-03-10": {Forecast.Solar: 14.2, Solcast PV Forecast: 11.9, Open-Meteo Solar Forecast: 13.7}
  ...
```

The `history` dict is directly usable in ApexCharts / template cards to plot how each source's morning forecast has evolved over time.

---

### Diagnostics – PV Daily Production

A built-in daily production meter sensor that replaces the need for an external `utility_meter` helper. It resets automatically at midnight and persists its value across HA restarts.

> **Only created when PV production sensor(s) are configured** in Step 3 of the setup. Without a configured PV sensor, this entity does not exist and Solar Fusion uses equal weights permanently.

Supports both sensor types:
- **`total_increasing`** (lifetime kWh counter): tracks the delta since midnight
- **Daily-resetting sensors**: passes through the current value directly

When multiple PV sensors are configured, their values are **summed** into a single daily total. This sensor is also used internally by Solar Fusion as the preferred source for nightly accuracy recording — taking priority over reading the raw PV sensors directly from the HA recorder.

Key attributes:

```yaml
date: "2026-03-11"
source_count: 2
source_entities:
  - sensor.pv_dach
  - sensor.pv_garage
day_start_sensor_pv_dach: 12453.2
day_start_sensor_pv_garage: 3821.7
```

---

## Hourly data sources

Each integration exposes hourly data in a different way. Solar Fusion reads them as follows:

| Source | Attribute | Location |
|--------|-----------|----------|
| Forecast.Solar | `wh_hours` (dict `{ISO-ts: Wh}`) | On both today and tomorrow sensor |
| Open-Meteo Solar | `wh_hours` (dict `{ISO-ts: Wh}`) | On both today and tomorrow sensor |
| Solcast | `detailedHourly` (list `[{period_start, pv_estimate}]`) | On both today and tomorrow sensor |

When no hourly data is available for a day (source provides daily totals only), Solar Fusion builds a synthetic hourly profile: it averages the available `hourly_today` profiles from all sources, or falls back to a Gaussian bell curve peaking at 13:00 with σ = 3 h.

---

## Fusion algorithm

```
Every update interval:
  1. Read today/tomorrow kWh and hourly Wh from source HA entities

  Calibration per source (priority order):
  2a. Isotonic regression  — if ≥ 20 seasonal data points available
       Fits a monotone non-decreasing step curve to (forecast → actual) pairs
       using the pool-adjacent-violators algorithm. No external dependencies.
       Seasonal window: months within ±1 of the current month, all years.
  2b. Linear bias correction — if ≥ 3 recent data points
       factor = mean(actual) / mean(forecast), capped at ±40 %
  2c. No correction — insufficient history

  Weighting per source:
  3. Seasonal RMSE = RMSE over months within ±1 of current month
     weight_i = 1 / seasonal_RMSE_i
     Falls back to equal weights if any source has < 3 data points

  Hourly fusion:
  4. Calibrated hourly Wh values fused as weighted average per slot
  5. Fused hourly total normalised to match weighted average of calibrated
     daily totals (ensures hourly sum = expected day total)

  Uncertainty:
  6. Weighted standard deviation of source values per slot,
     expressed as % of the fused hourly mean

  Nightly (after midnight, on first update of the new day):
  7. Read yesterday's actual production from HA recorder
     (Diagnostics – PV Daily Production meter preferred; falls back to summing PV sensors)
  8. Compare actual against the 06:00 morning snapshot for each source
  9. Store (forecast_kwh, actual_kwh) pair in history for each source
 10. Invalidate isotonic cache for affected seasonal windows
```

After approximately **3 weeks** of history, seasonal weighting and isotonic calibration begin to activate. After **one full year**, seasonal calibration covers all months independently.

---

## Multiple instances

Solar Fusion supports multiple config entries — one per array, or one combined instance.

**Scenario: two arrays with separate production sensors**

The recommended approach is a single combined instance, selecting both PV sensors in the settings step (Solar Fusion sums them automatically into the `Diagnostics – PV Daily Production` sensor).

Alternatively, use a template sensor:

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

> **Note:** Solcast combines all configured rooftop sites into a single set of sensors. A separate Solar Fusion instance per array only makes sense if each array has its own Forecast.Solar or Open-Meteo instance configured with the correct azimuth and tilt.

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
    entity_id: sensor.solar_fusion_dach_forecast_tomorrow
    below: 10
  action:
    service: switch.turn_on
    target:
      entity_id: switch.ev_charger
```

---

## Data flow

**Forecast sources** — Forecast.Solar, Open-Meteo Solar and Solcast each provide daily kWh totals and hourly Wh breakdowns, which Solar Fusion reads directly from their HA entities. These are calibrated, weighted and fused into four output sensors: **Forecast – Today**, **Forecast – Tomorrow**, **Forecast – Hourly** and **Forecast – Uncertainty**. The **Quality – \<Source\>** sensors and the **Diagnostics – Morning Snapshot** sensor are updated as part of the same process.

**Actual production** — If one or more PV production sensors are configured, Solar Fusion creates the **Diagnostics – PV Daily Production** meter, which accumulates the day's output and resets at midnight. After midnight, this value is compared against the morning snapshot to record accuracy history, which in turn drives the isotonic regression calibration and seasonal source weights.

---

## Storage & persistence

All history records, morning snapshots, and isotonic regression caches are persisted in HA's built-in storage (`.storage/solar_fusion_history_<entry_id>`). Data survives HA restarts automatically. Morning snapshots are pruned after 30 days; history records follow the configured rolling window (default: 14 days for RMSE, all seasonal data retained for isotonic fitting).

---

## Requirements

- Home Assistant 2023.6 or newer
- The `recorder` integration (enabled by default in HA)
- At least one supported solar forecast integration installed and providing data

---

## License

MIT