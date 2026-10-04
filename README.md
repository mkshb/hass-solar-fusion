# Solar Fusion

> **Note:** This is a very early version of the integration. Features and configuration are subject to significant change. Use at your own risk.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![HACS Validate](https://github.com/mkshb/hass-solar-fusion/actions/workflows/hacs-validate.yaml/badge.svg)](https://github.com/mkshb/hass-solar-fusion/actions/workflows/hacs-validate.yaml) [![GitHub Stars](https://img.shields.io/github/stars/mkshb/hass-solar-fusion?style=flat)](https://github.com/mkshb/hass-solar-fusion/stargazers) [![Last Commit](https://img.shields.io/github/last-commit/mkshb/hass-solar-fusion)](https://github.com/mkshb/hass-solar-fusion/commits/main) [![Open Issues](https://img.shields.io/github/issues/mkshb/hass-solar-fusion)](https://github.com/mkshb/hass-solar-fusion/issues)

A Home Assistant custom integration that **reads data from your already-installed solar forecast integrations** and combines them into a single, statistically optimised forecast — with no API calls of its own, no account required, and no data ever leaving your home.

---

## Why open source — and why it matters for energy data

Your solar production data is more revealing than it looks. Combined with consumption patterns, it tells a detailed story: when you are home, when you sleep, when you go on holiday, what appliances you run. This is personal data worth protecting.

**The problem with closed-source forecast tools:**

- You cannot verify what they do with your data. The algorithm, the data flow, and any telemetry are invisible.
- Machine learning models often require training data — and that data may be *yours*, collected silently in the background.
- Even if no data is collected today, a single update can change that — with no way for you to notice.
- "It works well" is not the same as "it is safe". Both things need to be true.

**What Solar Fusion guarantees instead:**

- **Full auditability.** Every line of code is public. The calibration algorithm, the weighting logic, and the storage format are documented in this README and visible in the source.
- **Zero external communication.** Solar Fusion never opens a network connection of its own. All data stays inside your Home Assistant instance.
- **No account, no cloud, no telemetry.** There is nothing to sign up for and nowhere for data to go.
- **Community oversight.** Open source means bugs, privacy issues, and design decisions are reviewed publicly — not decided behind closed doors.

If you choose tools for your home automation that process your energy data, you should be able to read their source code. If you cannot, you are trusting a promise you cannot verify.

---

## How it works

Solar Fusion does **not** contact any external service. Instead, it reads the sensor entities that your existing forecast integrations (Forecast.Solar, Open-Meteo Solar, Solcast) have already created in your Home Assistant. It then:

1. Reads daily totals **and hourly breakdowns** from each source's entities
2. Tracks how accurate each source has been against your actual PV production
3. Calibrates forecasts using **isotonic regression** (seasonal, non-linear correction) or linear bias correction depending on available history
4. Combines the calibrated forecasts using **inverse-error-variance weighting** (weight ∝ 1 / RMSE² of the calibrated forecast; a source far worse than the best one is excluded)
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
   Add `https://github.com/mkshb/hass-solar-fusion` as an **Integration**
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

**Open-Meteo entity discovery:** Open-Meteo Solar Forecast uses the same approach — Solar Fusion resolves its entities via the registry to avoid collisions with Forecast.Solar (both share the default name `sensor.energy_production_today`). Localised names (e.g. `_heute` / `_morgen` or `_energy_today` / `_energy_tomorrow`) are found automatically.

### Step 3 – Settings
- **PV production sensor(s)** *(optional)*: Select your actual generation sensor(s). Multiple sensors are supported and summed automatically (e.g. roof + garage). This enables accuracy tracking, adaptive weighting and isotonic calibration. Without it, equal weights are used permanently.
- **Update interval**: How often Solar Fusion re-reads the source entities (default: 60 min).

All settings can be changed later via **Settings → Devices & Services → Solar Fusion → Configure**. The **Configure** dialog additionally offers two weighting options:
- **Exclusion threshold k** (default 2.0): a source whose RMSE exceeds k × the RMSE of the best source gets weight 0. It keeps being evaluated and returns automatically once its error drops below the threshold.
- **Minimum evaluated days** (default 7): until a source has this many evaluated days, it is weighted neutrally; if no source has enough days, all sources are weighted equally.

and three shading options (see [Shading by sun position](#shading-by-sun-position)):
- **Learn shading from hourly production** (default off)
- **Apply learned shading to the forecast** (default on – has no effect until cells are learned)
- **Sources that already include a horizon profile** (default none), e.g. Open-Meteo with `use_horizon` enabled. These sources are neither corrected nor used for learning.

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
fused_today_kwh: 18.4
fused_tomorrow_kwh: 16.1
uncertainty_pct: 8.2
hourly_forecast_wh:
  "2026-03-12T06:00": 28.0
  "2026-03-12T07:00": 165.6
  ...
active_sources: [Forecast.Solar, Solcast PV Forecast]
missing_sources: [Open-Meteo Solar Forecast]
last_updated: "2026-03-11T14:00:00"
sources:                          # compact per-source summary (used by the companion card)
  forecast_solar:
    name: "Forecast.Solar"
    today_kwh: 18.4
    tomorrow_kwh: 16.8
    weight: 0.412
    excluded: false
    exclusion_reason: null        # e.g. "RMSE 11.9 kWh > 2 × beste Quelle (4.0 kWh)"
    calibration_active: true      # false → calibration made this source worse, fused raw
    rmse_calibrated_kwh: 1.31     # RMSE of the calibrated value
    rmse_kwh: 1.24
    mae_kwh: 0.98
    bias_kwh: -0.31
    days_evaluated: 12
    calibration_mode: "isotonic (23 seasonal pts)"
    quality_label: "Fair"
  solcast:
    ...
history:                          # last 30 raw history records
  - date: "2026-03-11"
    source: "forecast_solar"
    forecast_kwh: 18.4
    actual_kwh: 17.9
  - ...
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
excluded: false        # true → weight 0 (see exclusion_reason)
exclusion_reason: null
calibration_active: true   # false → fused raw, because calibration increased the error
rmse_calibrated_kwh: 1.31  # weight uses this when calibration_active, else rmse_kwh
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

### Diagnostics – Shading

State: number of learned shading cells (sun azimuth × elevation, see [Shading by sun position](#shading-by-sun-position)). Diagnostic entity.

Key attributes:

```yaml
learn: true
apply: true
horizon_sources: []
active: true                 # apply on and at least one learned cell
shaded_cells:                # learned cells with factor < 0.9
  - {azimuth: 247.5, elevation: 13.0, factor: 0.43, samples: 3}
  - {azimuth: 262.5, elevation: 5.0, factor: 0.11, samples: 4}
learning_days_used: 4        # days that passed the cloud filter
learning_days_stored: 7
last_learning_run: "2026-10-04T00:05:00+02:00"
energy_kept:                 # share of each source's daily energy left after correction
  "2026-10-04": {Open-Meteo Solar Forecast: 0.948, Solcast PV Forecast: 0.951}
```

The complete map (all cells with sample counts) and the stored learning days are part of the integration's diagnostics download.

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

## Energy Dashboard integration

Solar Fusion registers as a native **solar forecast provider** for the HA Energy Dashboard. Once installed, it appears in the forecast dropdown alongside Forecast.Solar and Solcast.

**Setup:**

1. Go to **Settings → Energy**
2. Under *Solar panels*, click the pencil icon next to an existing solar panel entry
3. Under *Forecast*, select **Solar Fusion** (or *Solar Fusion – \<Name\>* for named instances) from the dropdown
4. Click *Update*

The Energy Dashboard will now display Solar Fusion's combined hourly forecast as the shaded prediction band on the solar production graph.

> **Note:** Each Solar Fusion config entry (instance) registers independently. If you run multiple instances (e.g. one per array), each appears separately in the forecast dropdown.

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

## Companion card

A dedicated Lovelace card for Solar Fusion is maintained in a separate repository:
**[mkshb/hass-solar-fusion-card](https://github.com/mkshb/hass-solar-fusion-card)**

---

## Hourly data sources

Each integration exposes hourly data in a different way. Solar Fusion reads them as follows:

| Source | Attribute | Timestamp means | Location |
|--------|-----------|-----------------|----------|
| Forecast.Solar | `wh_hours` (dict `{ISO-ts: Wh}`) | **end** of the period (Forecast.Solar API) | Not set by the HA core integration – only used if a custom entity provides it |
| Open-Meteo Solar | `wh_period` (dict `{ISO-ts: Wh}`), legacy `wh_hours` | start of the hour | On both today and tomorrow sensor |
| Solcast | `detailedHourly` (list `[{period_start, pv_estimate}]`) | start of the hour | On both today and tomorrow sensor |

All values are stored under the local hour in which the period **starts**. Up to v0.2.3 Solar Fusion looked for `wh_hours` on Open-Meteo, which the sensor does not have, so Open-Meteo's hourly shape was never used; the fused hourly forecast followed Solcast's shape alone.

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

  Calibration gating per source:
  2d. Over the last 14 days, compare the RMSE of the raw forecast with the RMSE
      of the calibrated forecast. Each day is calibrated using only the history
      available before that day (an in-sample error would be too optimistic).
      Calibration is applied only if it lowers the RMSE; otherwise the source
      is fused raw (calibration_active: false). With < 7 evaluated days the
      source is always calibrated.
      Hysteresis: once decided, a source only switches when the other variant
      is better by at least 10 %, so it does not flip daily when both are close.
      The last decision is persisted with the history.

  Weighting per source:
  3. RMSE of the value that actually enters the fusion (raw or calibrated,
     see 2d), floored at 0.5 kWh.
     Source with RMSE > k × RMSE of the best source → weight 0 (default k = 2.0)
     Remaining sources: weight_i ∝ 1 / RMSE_i², normalised to sum 1
     Sources with < 7 evaluated days get the mean weight of the others;
     equal weights while no source has 7 days. Missing sources are dropped
     and the rest renormalised. Daily and hourly fusion use the same weights.

  Shading (only with "apply" on and a learned map):
  3a. Each source's hourly Wh × shading factor of the slot's sun position
      (mean over the four quarter-hour positions); horizon sources untouched.
      The source's daily total shrinks by the same share before step 5.

  Hourly fusion:
  4. Calibrated hourly Wh values fused as weighted average per slot
  5. Fused hourly total normalised to match weighted average of calibrated
     daily totals after shading (ensures hourly sum = expected day total
     without undoing the hourly shading)

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

## Shading by sun position

Forecast models know the sky, not your neighbour's roof. If a building or a tree shades your panels in the late afternoon, every source over-forecasts those hours – and the hour of the drop moves by about a minute per day with the season, so a correction per clock time goes stale. Solar Fusion therefore learns a correction **per sun position**: a map of cells of 5° azimuth × 2° elevation, each with a factor between 0.05 and 1.0. One map applies to all sources. No horizon file is needed.

**Learning** (option *Learn shading from hourly production*, runs after midnight):

1. Hourly production of each finished day comes from the recorder's **long-term statistics** (`hour`, `change`) of the PV Daily Production meter, or the configured PV sensors summed. Long-term statistics are kept indefinitely, unlike the 10-day state history.
2. The reference forecast is the **hourly** 06:00 morning snapshot of each source (stored since v0.3.0; older snapshots without hourly values are skipped).
3. For every hour: ratio = actual / forecast. Clouds are removed by dividing by the day's ratio in the safely unshaded hours (sun elevation > 25°). Only days whose ratio is **stable** across those reference hours (coefficient of variation ≤ 0.15, at least 3 hours) are used – on a changeable day the hourly ratio is cloud noise, not shading. At 53° N, days with three hours above 25° exist from about March to mid-October; cells learned in autumn carry over.
4. Each hour is placed by the sun position at the slot's midpoint. A cell's factor is the median of all samples in the cell **and its eight neighbours**, clamped to [0.05, 1.0]; it counts as learned with at least 3 samples. The neighbourhood matters: the midpoint of a given slot moves 0.5–0.7° in elevation per day, so a single cell is hit on only a few clear days. In a rolling simulation (learn from all previous days, forecast the next day, 40 % clear days) the hourly error dropped by 49 % with the neighbourhood versus 10 % with single cells.
5. Factors of 0.9 or more count as 1.0 (normalisation noise, not shading). Factors above 1.0 (reflection) are not used, since they cannot be told apart from noise.

**Applying** (option *Apply learned shading*): each hour of each source is multiplied by the mean factor at its four quarter-hour sun positions (bilinear between learned cells; unknown areas stay at 1.0) **before** weighting, and the source's daily total is reduced by the same share, so `Forecast – Today/Tomorrow` equals the sum of the corrected hours. Quarter-hour positions whose surroundings have not been learned yet are left out of the mean instead of counting as 1.0. Quarter-hours beat the slot midpoint at realistic weather (20–33 % lower hourly error with 30–50 % clear days); only during the first weeks with nearly all-clear days is the midpoint marginally better.

**Calibration order.** The isotonic/linear calibration learns daily bias from `forecast_kwh` vs. `actual_kwh`. A raw forecast's bias already contains the average shading loss, so calibrating raw values and then shading would subtract the loss twice. Therefore:

- Once a source has *min evaluated days* history records with a shading-corrected morning forecast (`forecast_corrected_kwh`, stored while *apply* is on), calibration, gating and weights for that source run on the **corrected** values; daily total = calibrated(raw × kept share).
- Until then they run on the **raw** values. If the source's calibration is active and already learned, its daily total stays calibrated(raw) and shading only shifts energy between hours; otherwise the daily total is raw × kept share.

**Double correction.** Mark sources that already model the horizon (e.g. Open-Meteo with `use_horizon`) under *Sources that already include a horizon profile*.

**Retroactive learning.** `solar_fusion.learn_shading` (optional `days`, default 10) rebuilds the 06:00 hourly forecast of past days from the recorder's state history and learns from them. This only works for sources whose hourly attribute is recorded: Open-Meteo's `wh_period` is, Solcast's `detailedHourly` is excluded from recording by that integration. It reaches back as far as the recorder keeps states (default 10 days). The action returns a summary per instance.

---

## Data flow

**Forecast sources** — Forecast.Solar, Open-Meteo Solar and Solcast each provide daily kWh totals and hourly Wh breakdowns, which Solar Fusion reads directly from their HA entities. These are calibrated, weighted and fused into four output sensors: **Forecast – Today**, **Forecast – Tomorrow**, **Forecast – Hourly** and **Forecast – Uncertainty**. The **Quality – \<Source\>** sensors and the **Diagnostics – Morning Snapshot** sensor are updated as part of the same process.

**Actual production** — If one or more PV production sensors are configured, Solar Fusion creates the **Diagnostics – PV Daily Production** meter, which accumulates the day's output and resets at midnight. After midnight, this value is compared against the morning snapshot to record accuracy history, which in turn drives the isotonic regression calibration and seasonal source weights.

---

## Storage & persistence

All history records, morning snapshots, and isotonic regression caches are persisted in HA's built-in storage (`.storage/solar_fusion_history_<entry_id>`). Data survives HA restarts automatically. Morning snapshots are pruned after 30 days; history records follow the configured rolling window (default: 14 days for RMSE, all seasonal data retained for isotonic fitting).

Storage version 2 (v0.3.0) stores morning snapshots as `{daily, daily_corrected, hourly}` per day; version-1 data is migrated automatically. Hourly actuals and forecasts for shading are kept for 400 days, so every sun position recurs once.

---

## Requirements

- Home Assistant 2023.6 or newer
- The `recorder` integration (enabled by default in HA)
- At least one supported solar forecast integration installed and providing data

---

## Development & tests

Two test suites, both run in CI (`.github/workflows/tests.yaml`):

- **`tests/`** – unit tests without Home Assistant (calculation, shading, fusion with a stub for `homeassistant.util.dt`, storage migration). Run each file as a script (`python3 tests/test_calc.py`) or all with `pytest`.
- **`tests_ha/`** – integration tests with a real Home Assistant core and recorder via [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component) and synthetic data. Python 3.14:

  ```bash
  pip install -r requirements_test.txt
  pytest tests_ha
  ```

Run the two suites separately: the unit tests replace `homeassistant` modules with stubs.

---

## License

MIT
