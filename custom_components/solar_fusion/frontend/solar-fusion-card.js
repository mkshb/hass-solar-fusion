/**
 * Solar Fusion Card
 * Lovelace Custom Card for the Solar Fusion integration.
 *
 * Shipped with the integration since 0.4.0: it serves this file and locales/
 * under /solar_fusion/ and loads the card as a frontend module
 * (?v=<integration version>). No dashboard resource is needed.
 *
 * Card YAML – only one entity required:
 *   type: custom:solar-fusion-card
 *   entity: sensor.solar_fusion_dach_forecast_today
 *   title: Solar Fusion Roof   # optional
 *
 * Colours and font come from the Home Assistant theme (CSS variables), so
 * the card follows light, dark and custom themes.
 */

// Integration version, passed by the integration as ?v=<version>
const CARD_VERSION = new URL(import.meta.url).searchParams.get("v") || "dev";

// Hourly actuals from the recorder are refreshed at most this often
const STATS_REFRESH_MS = 5 * 60 * 1000;
const HISTORY_DAYS = 14;

// Keys match the integration's language-neutral quality_label (calc.quality_label)
const QUALITY_COLOR = {
  accurate: "var(--success-color, #4caf50)",
  skewed:   "var(--info-color, #039be5)",
  noisy:    "var(--warning-color, #ffa600)",
  poor:     "var(--error-color, #db4437)",
};

// Built-in English fallback – used when locale JSON files are unavailable
const DEFAULT_LOCALE = {
  "entity_not_found": "Entity not found:",
  "default_title":    "Solar Fusion",
  "updated":          "Updated {time}",
  "details":          "Details",
  "actual_today":     "Yield today",
  "forecast_today":   "Forecast today",
  "forecast_tomorrow": "Forecast tomorrow",
  "of_forecast":      "{pct} % of forecast",
  "uncertainty":      "±{pct} % uncertainty",
  "from_sources":     "from {n} sources",
  "today":            "Today",
  "tomorrow":         "Tomorrow",
  "forecast":         "Forecast",
  "actual":           "Yield",
  "now":              "now",
  "hour_tip":         "{hour}:00 · forecast {fc} kWh",
  "hour_tip_actual":  "{hour}:00 · forecast {fc} kWh · yield {ac} kWh",
  "no_hourly":        "No hourly forecast available",
  "shading":          "Shading",
  "shading_tip":      "without shading {us} kWh",
  "shading_loss":     "shading −{v}\u00a0kWh",
  "sources_today":    "Sources today",
  "quality_accurate": "Accurate",
  "quality_skewed":   "Skewed",
  "quality_noisy":    "Noisy",
  "quality_poor":     "Poor",
  "excluded":         "excl.",
  "excluded_reason":  "Excluded: RMSE {rmse} kWh too high compared to the best source",
  "quality_tip":      "RMSE {rmse} kWh · MAE {mae} kWh · bias {bias} kWh · {days} days",
  "cal_raw_hint":     "Calibration would increase the error – source is fused uncalibrated",
  "history_title":    "Forecast deviation ({n} days)",
  "avg_over":         "Ø +{v} kWh too high",
  "avg_under":        "Ø −{v} kWh too low",
  "day_over":         "{date}: +{v} kWh too high",
  "day_under":        "{date}: −{v} kWh too low",
  "day_none":         "{date}: no data",
  "no_history":       "No history data yet",
};

const SOURCE_SHORT = {
  "Forecast.Solar":            "Forecast.Solar",
  "Open-Meteo Solar Forecast": "Open-Meteo",
  "Solcast PV Forecast":       "Solcast",
};

const STYLES = `
  :host {
    --sf-solar: var(--energy-solar-color, #ff9800);
    --sf-under: var(--info-color, #039be5);
    --sf-forecast: color-mix(in srgb, var(--sf-solar) 35%, transparent);
    --sf-tile: color-mix(in srgb, var(--primary-text-color) 6%, transparent);
    --sf-track: color-mix(in srgb, var(--primary-text-color) 10%, transparent);
    --sf-divider: var(--divider-color, rgba(127, 127, 127, 0.2));
    --sf-secondary: var(--secondary-text-color);
  }
  ha-card {
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    color: var(--primary-text-color);
  }
  button { font: inherit; color: inherit; }
  .muted { color: var(--sf-secondary); }
  .small { font-size: 12px; }

  /* Header */
  .header { display: flex; align-items: center; gap: 12px; }
  .header-icon {
    width: 40px; height: 40px; flex-shrink: 0; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    color: var(--sf-solar);
    background: color-mix(in srgb, var(--sf-solar) 20%, transparent);
  }
  .header-text { flex: 1; min-width: 0; }
  .title { font-size: 16px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .icon-button {
    width: 44px; height: 44px; flex-shrink: 0; border: 0; border-radius: 50%;
    background: transparent; color: var(--sf-secondary); cursor: pointer;
    display: flex; align-items: center; justify-content: center;
  }
  .icon-button:hover { background: var(--sf-tile); }

  /* Tiles */
  .tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
  .tile {
    background: var(--sf-tile); border: 0; border-radius: 12px; padding: 12px;
    display: flex; flex-direction: column; gap: 4px; text-align: left; cursor: pointer; min-width: 0;
  }
  .tile-value { font-size: 22px; font-weight: 500; white-space: nowrap; }
  .tile-unit { font-size: 13px; font-weight: 400; color: var(--sf-secondary); }
  .progress { display: block; height: 4px; border-radius: 2px; background: var(--sf-track); overflow: hidden; }
  .progress > * { display: block; height: 100%; background: var(--sf-solar); }

  /* Chart */
  .chart-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .toggle { display: inline-flex; padding: 2px; border-radius: 10px; background: var(--sf-tile); }
  .toggle button {
    min-height: 32px; padding: 0 14px; border: 0; border-radius: 8px; background: transparent;
    font-size: 13px; font-weight: 500; color: var(--sf-secondary); cursor: pointer;
  }
  .toggle button[aria-pressed="true"] {
    background: var(--ha-card-background, var(--card-background-color, #fff));
    color: var(--primary-text-color);
  }
  .legend { display: flex; gap: 14px; font-size: 12px; color: var(--sf-secondary); }
  .legend span { display: flex; align-items: center; gap: 6px; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; }
  .chart { display: flex; gap: 8px; margin-top: 10px; }
  .axis {
    height: 160px; width: 40px; flex-shrink: 0; display: flex; flex-direction: column;
    justify-content: space-between; text-align: right; font-size: 11px; color: var(--sf-secondary);
  }
  .plot { flex: 1; position: relative; height: 160px; min-width: 0; }
  .grid { position: absolute; left: 0; right: 0; border-top: 1px dashed var(--sf-divider); }
  .bars {
    position: absolute; inset: 0; display: flex; align-items: flex-end; gap: 4px;
    border-bottom: 1px solid var(--sf-divider);
  }
  .bar {
    flex: 1; height: 100%; position: relative; padding: 0; border: 0; background: transparent;
    cursor: pointer; min-width: 0;
  }
  .bar .fc, .bar .ac { display: block; position: absolute; bottom: 0; }
  .bar .fc { left: 0; right: 0; border-radius: 3px 3px 0 0; background: var(--sf-forecast); }
  .bar .us {
    display: block; position: absolute; left: 0; right: 0; box-sizing: border-box;
    border: 1px dashed var(--sf-solar); border-bottom: 0; border-radius: 3px 3px 0 0;
    background: repeating-linear-gradient(135deg, color-mix(in srgb, var(--sf-solar) 22%, transparent) 0 2px, transparent 2px 5px);
  }
  .swatch.us { border: 1px dashed var(--sf-solar); background: repeating-linear-gradient(135deg, color-mix(in srgb, var(--sf-solar) 30%, transparent) 0 2px, transparent 2px 4px); box-sizing: border-box; }
  .bar .ac { left: 20%; right: 20%; border-radius: 2px 2px 0 0; background: var(--sf-solar); }
  .bar.selected .fc { outline: 1px solid var(--sf-solar); }
  .labels { display: flex; gap: 4px; padding-left: 48px; margin-top: 4px; }
  .labels span { flex: 1; min-width: 0; font-size: 11px; text-align: center; white-space: nowrap; overflow: visible; color: var(--sf-secondary); }
  .labels span.now { color: var(--primary-text-color); font-weight: 500; }
  .info {
    height: 18px; line-height: 18px; margin-top: 6px; font-size: 12px; color: var(--sf-secondary);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }

  /* Sources */
  .section-title { font-size: 14px; font-weight: 500; margin-bottom: 4px; }
  .source {
    display: grid; grid-template-columns: minmax(0, 104px) 76px minmax(0, 1fr) 72px minmax(36px, max-content);
    align-items: center; gap: 10px; min-height: 44px; border-top: 1px solid var(--sf-divider); cursor: pointer;
  }
  .source-name { font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .source-kwh { font-size: 14px; text-align: right; white-space: nowrap; }
  .source-weight { font-size: 12px; text-align: right; color: var(--sf-secondary); }
  .source.excluded .source-name, .source.excluded .source-kwh, .source.excluded .progress { opacity: 0.45; }
  .source.excluded .progress > * { background: var(--sf-secondary); }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 500;
    white-space: nowrap; max-width: 100%; overflow: hidden; text-overflow: ellipsis;
  }

  /* Deviation */
  .dev-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .dev { height: 64px; display: flex; gap: 6px; margin-top: 8px; }
  .day { flex: 1; display: flex; flex-direction: column; padding: 0; border: 0; background: transparent; cursor: pointer; min-width: 0; }
  .day .up, .day .down { width: 100%; }
  .day .up { height: 32px; display: flex; align-items: flex-end; border-bottom: 1px solid var(--sf-divider); }
  .day .down { height: 32px; display: flex; align-items: flex-start; }
  .day .up > span { display: block; width: 100%; border-radius: 2px 2px 0 0; background: var(--sf-solar); }
  .day .down > span { display: block; width: 100%; border-radius: 0 0 2px 2px; background: var(--sf-under); }
  .day.missing { cursor: default; }
  .day.selected { outline: 1px solid var(--sf-divider); outline-offset: 2px; border-radius: 2px; }
  .dev-dates { display: flex; justify-content: space-between; font-size: 11px; color: var(--sf-secondary); margin-top: 4px; }
  .empty { padding: 16px 0; text-align: center; font-size: 12px; color: var(--sf-secondary); }

  /* Narrow cards */
  @container (max-width: 400px) {
    .tile { padding: 10px 8px; }
    .tile-value { font-size: 18px; }
    .source { grid-template-columns: minmax(0, 1fr) 72px 64px; }
    .source .badge-cell, .source .source-weight { display: none; }
  }
  .wrap { container-type: inline-size; }
`;

class SolarFusionCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._prefix = "";
    this._locale = {};
    this._localeLang = null;
    this._day = "today";        // chart: "today" | "tomorrow"
    this._selectedHour = null;  // chart slot shown in the info line (tap on mobile)
    this._selectedDay = null;   // deviation day shown in the info line
    this._actualHourly = {};    // {"HH": kWh} today, from recorder statistics
    this._statsFetched = 0;
    this._statsDate = null;
    this._lastStates = null;    // state objects of the last render
  }

  // Load locale JSON from locales/<lang>.json next to the card file.
  // Falls back to English if the requested language is unavailable.
  // The version query busts the browser cache after an update.
  async _loadLocale(lang) {
    const base = new URL(".", import.meta.url).href;
    const query = new URL(import.meta.url).search;
    for (const l of [lang.split("-")[0], "en"]) {
      try {
        const res = await fetch(`${base}locales/${l}.json${query}`);
        if (res.ok) return await res.json();
      } catch (_) { /* try next */ }
    }
    return {};
  }

  // Returns a translated string with {name} placeholders filled
  _t(key, vars = {}) {
    const text = this._locale[key] ?? DEFAULT_LOCALE[key] ?? key;
    return text.replace(/\{(\w+)\}/g, (m, k) => vars[k] ?? m);
  }

  setConfig(config) {
    if (!config.entity) throw new Error("'entity' is required");
    this._config = config;
    // Derive prefix: "sensor.solar_fusion_dach_forecast_today" → "sensor.solar_fusion_dach"
    this._prefix = config.entity.replace(/_forecast_today$/, "");
    this._render();
  }

  set hass(hass) {
    const lang = hass.language || "en";
    this._hass = hass;
    this._maybeFetchStats();

    if (lang !== this._localeLang) {
      this._localeLang = lang;
      this._loadLocale(lang).then(locale => {
        this._locale = locale;
        this._render();
      });
      return;
    }
    // hass is set on every state change in Home Assistant; redraw only when
    // one of the card's own entities changed (keeps hover and selection).
    const states = [this._config.entity, `${this._prefix}_forecast_tomorrow`, this._actualId()]
      .map(id => hass.states[id]);
    if (this._lastStates && states.every((st, i) => st === this._lastStates[i])) return;
    this._render();
  }

  getCardSize() {
    return 9;
  }

  static getStubConfig() {
    return { entity: "sensor.solar_fusion_dach_forecast_today" };
  }

  // ── Data ────────────────────────────────────────────────────────────────

  _actualId() {
    return `${this._prefix}_diagnostics_pv_daily_production`;
  }

  _todayIso() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  // Hourly yield of today from the recorder's long-term statistics of the
  // PV daily meter (statistics are written after each hour ends).
  async _maybeFetchStats() {
    const today = this._todayIso();
    if (this._statsDate === today && Date.now() - this._statsFetched < STATS_REFRESH_MS) return;
    if (!this._hass?.states[this._actualId()]) return;
    if (this._statsDate !== today) this._actualHourly = {};
    this._statsFetched = Date.now();
    this._statsDate = today;
    const start = new Date();
    start.setHours(0, 0, 0, 0);
    try {
      const result = await this._hass.callWS({
        type: "recorder/statistics_during_period",
        start_time: start.toISOString(),
        statistic_ids: [this._actualId()],
        period: "hour",
        types: ["change"],
        units: { energy: "kWh" },
      });
      const hourly = {};
      for (const row of result?.[this._actualId()] || []) {
        const begin = new Date(row.start);
        if (row.change == null || begin < start) continue;
        hourly[String(begin.getHours()).padStart(2, "0")] = Math.max(0, row.change);
      }
      this._actualHourly = hourly;
      this._render();
    } catch (err) {
      console.debug("Solar Fusion Card: no hourly statistics", err);
    }
  }

  // [{hour, fc, ac}] in kWh for the chart; today includes the running hour
  _hourlySeries(attrs, tomorrowAttrs, actualKwh) {
    const isToday = this._day === "today";
    const source = (isToday ? attrs.hourly_forecast_wh : tomorrowAttrs?.hourly_forecast_wh) || {};
    const fc = {};
    for (const [slot, wh] of Object.entries(source)) {
      fc[slot.slice(11, 13)] = (Number(wh) || 0) / 1000;
    }
    // Hours lowered by shading: forecast without shading
    const unshaded = (isToday ? attrs.unshaded_hourly_wh : tomorrowAttrs?.unshaded_hourly_wh) || {};
    const us = {};
    for (const [slot, wh] of Object.entries(unshaded)) {
      us[slot.slice(11, 13)] = (Number(wh) || 0) / 1000;
    }
    const ac = {};
    if (isToday) {
      Object.assign(ac, this._actualHourly);
      // Running hour: total so far minus the completed hours
      const nowHour = String(new Date().getHours()).padStart(2, "0");
      if (actualKwh != null && !(nowHour in ac)) {
        const done = Object.values(ac).reduce((a, b) => a + b, 0);
        if (actualKwh - done > 0) ac[nowHour] = actualKwh - done;
      }
    }
    const hours = [...new Set([...Object.keys(fc), ...Object.keys(ac)])]
      .filter(h => (fc[h] || 0) > 0.001 || (ac[h] || 0) > 0.001)
      .map(Number);
    if (!hours.length) return [];
    const series = [];
    for (let h = Math.min(...hours); h <= Math.max(...hours); h++) {
      const key = String(h).padStart(2, "0");
      const lowered = (us[key] || 0) - (fc[key] || 0) > 0.005;
      series.push({
        hour: key, fc: fc[key] || 0, ac: isToday && key in ac ? ac[key] : null, us: lowered ? us[key] : null,
      });
    }
    return series;
  }

  // kWh removed by shading over the day (0 without shading)
  _shadingLoss(unshadedWh, forecastWh) {
    let loss = 0;
    for (const [slot, wh] of Object.entries(unshadedWh || {})) {
      loss += Math.max(0, (Number(wh) || 0) - (Number(forecastWh?.[slot]) || 0));
    }
    return loss / 1000;
  }

  // [{date, deviation}] for the last HISTORY_DAYS days up to yesterday –
  // mean morning forecast of the sources minus actual; null without a record
  _deviationSeries(history) {
    const byDate = {};
    for (const r of history) {
      if (!byDate[r.date]) byDate[r.date] = { forecasts: [], actual: r.actual_kwh };
      byDate[r.date].forecasts.push(r.forecast_kwh);
    }
    const points = [];
    for (let k = HISTORY_DAYS; k >= 1; k--) {
      const d = new Date();
      d.setDate(d.getDate() - k);
      const date = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      const v = byDate[date];
      points.push({
        date,
        deviation: v ? v.forecasts.reduce((a, b) => a + b, 0) / (v.forecasts.length || 1) - v.actual : null,
      });
    }
    return points;
  }

  // ── Formatting ──────────────────────────────────────────────────────────

  _num(v, decimals = 1) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    return Number(v).toLocaleString(this._hass?.language || "en", {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    });
  }

  _date(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(this._hass?.language || "en", { day: "2-digit", month: "2-digit" });
  }

  _escape(str) {
    return String(str).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  // Upper end of the chart axis: a round number ≥ max
  _axisMax(max) {
    if (max <= 0) return 1;
    for (const step of [0.5, 1, 2, 2.5, 5, 10, 20, 50]) {
      if (max <= step * 2) return step * 2;
    }
    return Math.ceil(max / 50) * 50;
  }

  // RMSE that drives the weight: calibrated when calibration is active, else raw
  _effectiveRmse(s) {
    return s.calibration_active !== false && s.rmse_calibrated_kwh != null
      ? s.rmse_calibrated_kwh : s.rmse_kwh;
  }

  _qualityTip(s) {
    const bias = s.bias_kwh != null ? (s.bias_kwh > 0 ? "+" : "") + this._num(s.bias_kwh, 2) : "—";
    const lines = [this._t("quality_tip", {
      rmse: this._num(s.rmse_kwh, 2), mae: this._num(s.mae_kwh, 2), bias, days: s.days_evaluated ?? 0,
    })];
    if (s.excluded) {
      const rmse = this._effectiveRmse(s);
      lines.push(this._t("excluded_reason", { rmse: rmse != null ? this._num(rmse, 1) : "—" }));
    }
    if (s.calibration_active === false) lines.push(this._t("cal_raw_hint"));
    return lines.join("\n");
  }

  _badge(s) {
    const key = (s.quality_label || "").toLowerCase();
    const color = QUALITY_COLOR[key];
    if (!color) return s.quality_label ? `<span class="badge muted">${this._escape(s.quality_label)}</span>` : "";
    // Text mixed towards the text colour: readable on light and dark themes
    return `<span class="badge" style="color:color-mix(in srgb, ${color} 70%, var(--primary-text-color));background:color-mix(in srgb, ${color} 18%, transparent)">${this._escape(this._t(`quality_${key}`))}</span>`;
  }

  // ── Interaction ─────────────────────────────────────────────────────────

  _moreInfo(entityId) {
    const event = new Event("hass-more-info", { bubbles: true, composed: true });
    event.detail = { entityId };
    this.dispatchEvent(event);
  }

  _attachListeners() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-entity]").forEach(el => {
      el.addEventListener("click", () => this._moreInfo(el.dataset.entity));
    });
    root.querySelectorAll("[data-day]").forEach(el => {
      el.addEventListener("click", () => {
        this._day = el.dataset.day;
        this._selectedHour = null;
        this._render();
      });
    });
    // Selecting a bar only changes classes and the info line: no redraw,
    // so the card keeps its size.
    const select = (attr, prop, info) => {
      root.querySelectorAll(`[data-${attr}]`).forEach(el => {
        el.addEventListener("click", () => {
          const value = this[prop] === el.dataset[attr] ? null : el.dataset[attr];
          this[prop] = value;
          root.querySelectorAll(`[data-${attr}]`).forEach(b => b.classList.toggle("selected", b.dataset[attr] === value));
          const line = root.querySelector(`.info[data-info="${info}"]`);
          if (line) line.textContent = value ? el.title : "";
        });
      });
    };
    select("hour", "_selectedHour", "chart");
    select("dev", "_selectedDay", "dev");
  }

  // ── Rendering ───────────────────────────────────────────────────────────

  _renderChart(series) {
    const toggle = `
      <div class="toggle" role="group">
        <button data-day="today" aria-pressed="${this._day === "today"}">${this._t("today")}</button>
        <button data-day="tomorrow" aria-pressed="${this._day === "tomorrow"}">${this._t("tomorrow")}</button>
      </div>`;
    const legend = `
      <div class="legend">
        <span><span class="swatch" style="background:var(--sf-forecast)"></span>${this._t("forecast")}</span>
        ${this._day === "today" ? `<span><span class="swatch" style="background:var(--sf-solar)"></span>${this._t("actual")}</span>` : ""}
        ${series.some(p => p.us != null) ? `<span><span class="swatch us"></span>${this._t("shading")}</span>` : ""}
      </div>`;
    if (!series.length) {
      return `<div><div class="chart-head">${toggle}${legend}</div><div class="empty">${this._t("no_hourly")}</div></div>`;
    }

    const top = this._axisMax(Math.max(...series.map(p => Math.max(p.fc, p.ac || 0, p.us || 0))));
    const height = 156;
    const nowHour = this._day === "today" ? String(new Date().getHours()).padStart(2, "0") : null;
    const nowIndex = series.findIndex(p => p.hour === nowHour);
    const tip = p => (p.ac != null
      ? this._t("hour_tip_actual", { hour: p.hour, fc: this._num(p.fc), ac: this._num(p.ac) })
      : this._t("hour_tip", { hour: p.hour, fc: this._num(p.fc) }))
      + (p.us != null ? ` · ${this._t("shading_tip", { us: this._num(p.us) })}` : "");

    const bars = series.map(p => {
      const fh = Math.max(1, Math.round(p.fc / top * height));
      const ah = p.ac != null ? Math.max(1, Math.round(p.ac / top * height)) : 0;
      const uh = p.us != null ? Math.max(2, Math.round((p.us - p.fc) / top * height)) : 0;
      const label = this._escape(tip(p));
      return `<button class="bar${this._selectedHour === p.hour ? " selected" : ""}" data-hour="${p.hour}" title="${label}" aria-label="${label}">
        <span class="fc" style="height:${p.fc > 0 ? fh : 0}px"></span>
        ${p.us != null ? `<span class="us" style="bottom:${p.fc > 0 ? fh : 0}px;height:${uh}px"></span>` : ""}
        ${p.ac != null ? `<span class="ac" style="height:${ah}px"></span>` : ""}
      </button>`;
    }).join("");

    const labels = series.map((p, i) => {
      if (i === nowIndex) return `<span class="now">${this._t("now")}</span>`;
      const crowded = nowIndex >= 0 && Math.abs(i - nowIndex) <= 1;
      return `<span>${Number(p.hour) % 3 === 0 && !crowded ? p.hour : ""}</span>`;
    }).join("");

    const selected = series.find(p => p.hour === this._selectedHour);
    return `
      <div>
        <div class="chart-head">${toggle}${legend}</div>
        <div class="chart">
          <div class="axis"><span>${this._num(top, top < 2 ? 1 : 0)} kWh</span><span>${this._num(top / 2, top < 4 ? 1 : 0)}</span><span>0</span></div>
          <div class="plot">
            <div class="grid" style="top:${160 - height}px"></div>
            <div class="grid" style="top:${160 - height / 2}px"></div>
            <div class="bars">${bars}</div>
          </div>
        </div>
        <div class="labels">${labels}</div>
        <div class="info" data-info="chart">${selected ? this._escape(tip(selected)) : ""}</div>
      </div>`;
  }

  _renderSources(sourceList, entityId) {
    if (!sourceList.length) return "";
    const maxKwh = Math.max(...sourceList.map(([, s]) => s.today_kwh || 0), 0.1);
    return `
      <div>
        <div class="section-title">${this._t("sources_today")}</div>
        ${sourceList.map(([, s]) => `
          <div class="source${s.excluded ? " excluded" : ""}" data-entity="${entityId}" title="${this._escape(this._qualityTip(s))}">
            <div class="source-name">${this._escape(SOURCE_SHORT[s.name] || s.name)}</div>
            <div class="badge-cell">${this._badge(s)}</div>
            <div class="progress"><div style="width:${((s.today_kwh || 0) / maxKwh * 100).toFixed(1)}%"></div></div>
            <div class="source-kwh">${this._num(s.today_kwh)} kWh</div>
            <div class="source-weight">${s.excluded ? this._t("excluded") : s.weight != null ? this._num(s.weight * 100, 0) + " %" : "—"}</div>
          </div>`).join("")}
      </div>`;
  }

  _renderDeviation(points) {
    const title = `<div class="section-title" style="margin:0">${this._t("history_title", { n: HISTORY_DAYS })}</div>`;
    const known = points.filter(p => p.deviation != null);
    if (!known.length) {
      return `<div><div class="dev-head">${title}</div><div class="empty">${this._t("no_history")}</div></div>`;
    }
    const avg = known.reduce((a, p) => a + p.deviation, 0) / known.length;
    const avgText = this._t(avg >= 0 ? "avg_over" : "avg_under", { v: this._num(Math.abs(avg)) });
    const maxDev = Math.max(...known.map(p => Math.abs(p.deviation)), 0.5);
    const tip = p => p.deviation == null
      ? this._t("day_none", { date: this._date(p.date) })
      : this._t(p.deviation >= 0 ? "day_over" : "day_under", {
        date: this._date(p.date), v: this._num(Math.abs(p.deviation)),
      });
    const days = points.map(p => {
      const h = Math.max(2, Math.round(Math.abs(p.deviation ?? 0) / maxDev * 30));
      const label = this._escape(tip(p));
      return `<button class="day${p.deviation == null ? " missing" : ""}${this._selectedDay === p.date ? " selected" : ""}" data-dev="${p.date}" title="${label}" aria-label="${label}">
        <span class="up">${p.deviation > 0 ? `<span style="height:${h}px"></span>` : ""}</span>
        <span class="down">${p.deviation < 0 ? `<span style="height:${h}px"></span>` : ""}</span>
      </button>`;
    }).join("");
    const selected = points.find(p => p.date === this._selectedDay);
    return `
      <div>
        <div class="dev-head">${title}<span class="small muted">${avgText}</span></div>
        <div class="dev">${days}</div>
        <div class="dev-dates"><span>${this._date(points[0].date)}</span><span>${this._date(points[points.length - 1].date)}</span></div>
        <div class="info" data-info="dev">${selected ? this._escape(tip(selected)) : ""}</div>
      </div>`;
  }

  _render() {
    if (!this._config || !this._hass) return;
    this._lastStates = [this._config.entity, `${this._prefix}_forecast_tomorrow`, this._actualId()]
      .map(id => this._hass.states[id]);

    const entityId   = this._config.entity;  // forecast_today – provides all attributes
    const tomorrowId = `${this._prefix}_forecast_tomorrow`;
    const actualId   = `${this._prefix}_diagnostics_pv_daily_production`;

    const mainState = this._hass.states[entityId];
    if (!mainState) {
      this.shadowRoot.innerHTML = `<style>${STYLES}</style>
        <ha-card><div class="empty">${this._t("entity_not_found")} ${this._escape(entityId)}</div></ha-card>`;
      return;
    }

    const attrs         = mainState.attributes;
    const tomorrowAttrs = this._hass.states[tomorrowId]?.attributes || {};
    const todayKwh      = parseFloat(mainState.state);
    const tomorrowKwh   = attrs.fused_tomorrow_kwh;
    const actualRaw     = parseFloat(this._hass.states[actualId]?.state);
    const actualKwh     = Number.isNaN(actualRaw) ? null : actualRaw;
    const uncertainty   = attrs.uncertainty_pct;
    const sources       = attrs.sources || {};
    const history       = attrs.history || [];
    const nSources      = (attrs.active_sources || []).length;
    const title         = this._config.title || this._t("default_title");
    const lang          = this._hass.language || "en";

    let updated = "";
    try {
      if (attrs.last_updated) {
        updated = this._t("updated", {
          time: new Date(attrs.last_updated).toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" }),
        });
      }
    } catch (_) {}

    const lossToday = this._shadingLoss(attrs.unshaded_hourly_wh, attrs.hourly_forecast_wh);
    const lossTomorrow = this._shadingLoss(tomorrowAttrs.unshaded_hourly_wh, tomorrowAttrs.hourly_forecast_wh);
    const pct = actualKwh != null && todayKwh > 0 ? Math.round(actualKwh / todayKwh * 100) : null;
    const series = this._hourlySeries(attrs, tomorrowAttrs, actualKwh);

    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <ha-card>
      <div class="wrap" style="display:flex;flex-direction:column;gap:20px">

        <div class="header">
          <div class="header-icon"><ha-icon icon="mdi:solar-power-variant"></ha-icon></div>
          <div class="header-text">
            <div class="title">${this._escape(title)}</div>
            <div class="small muted">${this._escape(updated)}</div>
          </div>
          <button class="icon-button" data-entity="${entityId}" aria-label="${this._t("details")}">
            <ha-icon icon="mdi:dots-vertical"></ha-icon>
          </button>
        </div>

        <div class="tiles">
          <button class="tile" data-entity="${actualId}">
            <span class="small muted">${this._t("actual_today")}</span>
            <span class="tile-value">${this._num(actualKwh)}<span class="tile-unit"> kWh</span></span>
            ${pct != null ? `<span class="progress"><span style="width:${Math.min(pct, 100)}%"></span></span>
            <span class="small muted">${this._t("of_forecast", { pct })}</span>` : ""}
          </button>
          <button class="tile" data-entity="${entityId}">
            <span class="small muted">${this._t("forecast_today")}</span>
            <span class="tile-value">${this._num(todayKwh)}<span class="tile-unit"> kWh</span></span>
            ${uncertainty != null ? `<span class="small muted">${this._t("uncertainty", { pct: this._num(uncertainty) })}</span>` : ""}
            ${lossToday >= 0.05 ? `<span class="small muted">${this._t("shading_loss", { v: this._num(lossToday) })}</span>` : ""}
          </button>
          <button class="tile" data-entity="${tomorrowId}">
            <span class="small muted">${this._t("forecast_tomorrow")}</span>
            <span class="tile-value">${this._num(tomorrowKwh)}<span class="tile-unit"> kWh</span></span>
            ${nSources ? `<span class="small muted">${this._t("from_sources", { n: nSources })}</span>` : ""}
            ${lossTomorrow >= 0.05 ? `<span class="small muted">${this._t("shading_loss", { v: this._num(lossTomorrow) })}</span>` : ""}
          </button>
        </div>

        ${this._renderChart(series)}
        ${this._renderSources(Object.entries(sources), entityId)}
        ${this._renderDeviation(this._deviationSeries(history))}

      </div>
      </ha-card>`;

    this._attachListeners();
  }
}

// The integration loads the card before any dashboard resource. A card from
// the former HACS plugin, still registered as a resource, then fails to
// define the element; if it was loaded first, this one steps aside.
if (!customElements.get("solar-fusion-card")) {
  customElements.define("solar-fusion-card", SolarFusionCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "solar-fusion-card",
    name: "Solar Fusion Card",
    description: "Fused PV forecast with hourly chart, source comparison, quality and history.",
  });
  console.info(
    `%c SOLAR-FUSION-CARD %c ${CARD_VERSION} `,
    "color:#fff;background:#ff9800;font-weight:700",
    "color:#ff9800;background:transparent",
  );
} else {
  console.warn(
    "Solar Fusion Card: another solar-fusion-card is already defined. " +
    "Remove the dashboard resource of the former HACS card (hass-solar-fusion-card).",
  );
}
