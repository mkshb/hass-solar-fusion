/**
 * Solar Fusion Card v2
 * Lovelace Custom Card for the Solar Fusion integration.
 *
 * Installation:
 *   1. Copy solar-fusion-card.js and the locales/ folder to /config/www/
 *   2. Settings → Dashboards → Resources:
 *      URL: /local/solar-fusion-card.js   Type: JavaScript module
 *
 * Card YAML – only one entity required:
 *   type: custom:solar-fusion-card
 *   entity: sensor.solar_fusion_dach_forecast_today
 *   title: Solar Fusion Roof   # optional
 */

// Keys match the integration's language-neutral quality_label (calc.quality_label)
const QUALITY = {
  "accurate": { color: "#4ade80" },
  "skewed":   { color: "#38bdf8" },
  "noisy":    { color: "#facc15" },
  "poor":     { color: "#f87171" },
};
// Labels from integration versions before 0.2.2
const QUALITY_ALIAS = {
  "excellent": "accurate", "top": "accurate", "good": "accurate", "genau": "accurate",
  "verzerrt (korrigiert)": "skewed",
  "fair": "noisy", "okay": "noisy", "unruhig": "noisy",
  "bad": "poor", "schlecht": "poor",
};

// Converts a #rrggbb hex color to rgba(r,g,b,alpha)
function hexRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

// Built-in English fallback – used when locale JSON files are unavailable
const DEFAULT_LOCALE = {
  "entity_not_found": "Entity not found:",
  "default_title":    "Solar Fusion",
  "updated":          "Updated",
  "time_suffix":      "",
  "today":            "Today",
  "tomorrow":         "Tomorrow",
  "uncertainty":      "% uncertainty",
  "sources_today":    "Sources – Today",
  "weight":           "Wgt.",
  "quality_accuracy": "Quality & Accuracy",
  "col_source":       "Source",
  "col_label":        "Label",
  "col_rmse":         "RMSE",
  "col_mae":          "MAE",
  "col_bias":         "Bias",
  "col_days":         "Days",
  "history_title":    "Forecast Deviation (14 days)",
  "no_history":       "No history data yet",
  "hero_actual":      "Today's Yield",
  "forecast":         "Forecast",
  "over_forecast":    "Over-forecast",
  "under_forecast":   "Under-forecast",
  "days_short":       "d",
  "quality_accurate": "Accurate",
  "quality_skewed":   "Skewed",
  "quality_noisy":    "Noisy",
  "quality_poor":     "Poor",
  "excluded":         "excl.",
  "excluded_reason":  "Excluded: RMSE {rmse} kWh too high compared to the best source",
  "cal_raw":          "raw",
  "cal_raw_hint":     "Calibration would increase the error – source is fused uncalibrated",
  "rmse_calibrated":  "RMSE calibrated",
};

const SOURCE_SHORT = {
  "Forecast.Solar":            "Forecast.Solar",
  "Open-Meteo Solar Forecast": "Open-Meteo",
  "Solcast PV Forecast":       "Solcast",
};


const STYLES = `
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@400;600;700;800&display=swap');

  :host {
    --sf-bg:      #0f1117;
    --sf-surface: #181c27;
    --sf-border:  #252a38;
    --sf-accent:  #f59e0b;
    --sf-accent2: #38bdf8;
    --sf-text:    #e2e8f0;
    --sf-muted:   #64748b;
    --sf-radius:  12px;
    --sf-font:    'Syne', sans-serif;
    --sf-mono:    'DM Mono', monospace;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  .card {
    background: var(--sf-bg);
    border: 1px solid var(--sf-border);
    border-radius: var(--sf-radius);
    padding: 20px;
    font-family: var(--sf-font);
    color: var(--sf-text);
    overflow: hidden;
    position: relative;
    container-type: inline-size;
  }
  .card::before {
    content: '';
    position: absolute;
    top: -60px; right: -60px;
    width: 200px; height: 200px;
    background: radial-gradient(circle, rgba(245,158,11,0.07) 0%, transparent 70%);
    pointer-events: none;
  }

  /* Clickable base style */
  [data-entity] {
    cursor: pointer;
    transition: opacity 0.15s ease;
  }
  [data-entity]:hover { opacity: 0.75; }
  [data-entity]:active { opacity: 0.5; }

  .header {
    display: flex; justify-content: space-between; align-items: flex-start;
    margin-bottom: 20px;
  }
  .title { font-size: 11px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--sf-muted); }
  .updated { font-family: var(--sf-mono); font-size: 10px; color: var(--sf-muted); text-align: right; }

  /* Hero */
  .hero { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 20px; }
  .hero-card {
    background: var(--sf-surface); border: 1px solid var(--sf-border);
    border-radius: 10px; padding: 14px 16px; position: relative; overflow: hidden;
  }
  .hero-card:hover { border-color: #3a4255; }
  .hero-bar { position: absolute; bottom: 0; left: 0; right: 0; height: 2px; }
  .hero-label { font-size: 10px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: var(--sf-muted); margin-bottom: 2px; }
  .hero-sublabel { font-size: 9px; font-weight: 400; letter-spacing: 0.05em; text-transform: uppercase; color: var(--sf-muted); opacity: 0.5; margin-bottom: 6px; }
  .hero-value { font-size: 28px; font-weight: 800; line-height: 1; }
  .hero-unit { font-size: 13px; font-weight: 400; color: var(--sf-muted); margin-left: 3px; }
  .hero-unc { font-family: var(--sf-mono); font-size: 10px; color: var(--sf-muted); margin-top: 6px; }

  /* Section */
  .section-title {
    font-size: 10px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--sf-muted); margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid var(--sf-border);
  }

  /* Sources */
  .sources { margin-bottom: 20px; }
  .source-row {
    display: grid; grid-template-columns: 110px 1fr 70px 68px;
    align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid #1e2330;
    border-radius: 4px;
  }
  .source-row:last-child { border-bottom: none; }
  .source-name { font-size: 12px; font-weight: 600; white-space: nowrap; }
  .bar-wrap { height: 6px; background: var(--sf-border); border-radius: 3px; overflow: hidden; }
  .bar { height: 100%; border-radius: 3px; background: var(--sf-accent); transition: width 0.7s cubic-bezier(.4,0,.2,1); }
  .source-kwh { font-family: var(--sf-mono); font-size: 11px; text-align: right; }
  .source-weight { font-family: var(--sf-mono); font-size: 10px; color: var(--sf-muted); text-align: right; }
  .source-kwh-tmr { font-family: var(--sf-mono); font-size: 9px; color: var(--sf-muted); margin-top: 1px; }
  .source-row.excluded .source-name,
  .source-row.excluded .source-kwh,
  .source-row.excluded .bar-wrap { opacity: 0.4; }
  .source-row.excluded .bar { background: var(--sf-muted); }
  .source-excl { color: #f87171; text-transform: uppercase; letter-spacing: 0.06em; }

  /* Quality table */
  .q-section { margin-bottom: 20px; }
  .q-header, .q-row {
    display: grid; grid-template-columns: 1fr 60px 46px 46px 46px 36px;
    align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid #1e2330;
    border-radius: 4px;
  }
  .q-row:last-child { border-bottom: none; }
  .q-header { font-size: 9px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--sf-muted); padding-bottom: 4px; }
  .q-col-label { text-align: left; }
  .q-name { font-size: 12px; font-weight: 600; white-space: nowrap; }
  .q-val { font-family: var(--sf-mono); font-size: 11px; }
  .q-days { font-family: var(--sf-mono); font-size: 10px; color: var(--sf-muted); }
  .q-cal { font-family: var(--sf-mono); font-size: 8px; color: var(--sf-muted); margin-top: 2px; opacity: 0.7; }
  .badge { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 9px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; font-family: var(--sf-font); }

  /* Sparkline */
  .spark-wrap {
    background: var(--sf-surface); border: 1px solid var(--sf-border);
    border-radius: 10px; padding: 14px;
  }
  .spark-wrap > svg { width: 100%; height: 80px; display: block; }
  .legend { display: flex; gap: 20px; margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--sf-border); align-items: center; }
  .legend-item { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #94a3b8; font-family: var(--sf-mono); white-space: nowrap; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }

  /* ── Responsive breakpoints via container queries ─────────────────── */

  /* Narrow (~320 px): drop weight + days columns */
  @container (max-width: 320px) {
    .source-name   { overflow: hidden; text-overflow: ellipsis; }
    .source-weight { display: none; }
    .source-row    { grid-template-columns: minmax(0, 1fr) 1fr 68px; }
    .q-name        { overflow: hidden; text-overflow: ellipsis; }
    .q-col-days, .q-days { display: none; }
    .q-header, .q-row { grid-template-columns: 1fr 58px 44px 44px 44px; }
  }

  /* Narrow hero: 3 → 2 columns */
  @container (max-width: 360px) {
    .hero { grid-template-columns: 1fr 1fr; }
  }

  /* Very narrow (~260 px): stack hero, drop kWh + mae + bias columns */
  @container (max-width: 260px) {
    .hero          { grid-template-columns: 1fr 1fr; }
    .hero-value    { font-size: 22px; }
    .source-kwh    { display: none; }
    .source-row    { grid-template-columns: minmax(0, 1fr) 1fr; }
    .q-col-mae, .q-val-mae { display: none; }
    .q-col-bias, .q-val-bias { display: none; }
    .q-header, .q-row { grid-template-columns: 1fr 56px 44px; }
  }
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
  }

  // Load locale JSON from locales/<lang>.json next to the card file.
  // Falls back to English if the requested language is unavailable.
  async _loadLocale(lang) {
    const base = new URL(".", import.meta.url).href;
    for (const l of [lang.split("-")[0], "en"]) {
      try {
        const res = await fetch(`${base}locales/${l}.json`);
        if (res.ok) return await res.json();
      } catch (_) { /* try next */ }
    }
    return {};
  }

  // Returns a translated string, falling back to the key itself.
  _t(key) {
    return this._locale[key] ?? DEFAULT_LOCALE[key] ?? key;
  }

  setConfig(config) {
    if (!config.entity) throw new Error("'entity' is required");
    this._config = config;
    // Derive prefix: "sensor.solar_fusion_dach_fused_today" → "sensor.solar_fusion_dach"
    this._prefix = config.entity.replace(/_forecast_today$/, "");
    this._render();
  }

  set hass(hass) {
    const lang = hass.language || "en";
    this._hass = hass;

    if (lang !== this._localeLang) {
      this._localeLang = lang;
      this._loadLocale(lang).then(locale => {
        this._locale = locale;
        this._render();
      });
    } else {
      this._render();
    }
  }

  // Fire HA more-info dialog for the given entity_id
  _moreInfo(entityId) {
    const event = new Event("hass-more-info", { bubbles: true, composed: true });
    event.detail = { entityId };
    this.dispatchEvent(event);
  }

  // Attach click listeners to all [data-entity] elements after render
  _attachListeners() {
    this.shadowRoot.querySelectorAll("[data-entity]").forEach(el => {
      el.addEventListener("click", () => this._moreInfo(el.dataset.entity));
    });
  }

  _fmt(v, decimals = 2) {
    if (v === null || v === undefined) return "—";
    return Number(v).toFixed(decimals);
  }

  // Fills {name} placeholders in a translated string
  _tf(key, vars) {
    return this._t(key).replace(/\{(\w+)\}/g, (m, k) => vars[k] ?? m);
  }

  _escape(str) {
    return String(str).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  // RMSE that drives the weight: calibrated when calibration is active, else raw
  _effectiveRmse(s) {
    return s.calibration_active !== false && s.rmse_calibrated_kwh != null
      ? s.rmse_calibrated_kwh : s.rmse_kwh;
  }

  // Short calibration hint for the quality table; null when nothing to show
  _calShort(s) {
    if (s.calibration_active === false) return this._t("cal_raw");
    const mode = s.calibration_mode || "";
    if (mode.startsWith("isotonic")) return "iso";
    if (mode.startsWith("linear")) return "linear";
    return null;
  }

  _sparkline(history) {
    const byDate = {};
    for (const r of history) {
      if (!byDate[r.date]) byDate[r.date] = { forecasts: [], actual: r.actual_kwh };
      byDate[r.date].forecasts.push(r.forecast_kwh);
    }
    const points = Object.entries(byDate)
      .sort(([a], [b]) => a.localeCompare(b))
      .slice(-14)
      .map(([date, v]) => ({
        date,
        deviation: v.forecasts.reduce((a, b) => a + b, 0) / (v.forecasts.length || 1) - v.actual,
      }));

    if (points.length < 1) {
      return `<text x="50%" y="50%" text-anchor="middle" fill="#64748b"
        font-size="11" font-family="DM Mono,monospace">${this._t("no_history")}</text>`;
    }

    const W = 400, H = 80;
    const CT = 6, CB = 62;           // chart area top / bottom y
    const midY = (CT + CB) / 2;      // zero line y = 34
    const maxDev = Math.max(...points.map(p => Math.abs(p.deviation)), 0.5);
    const scale = (midY - CT) / maxDev;

    const n = points.length;
    const slotW = (W - 24) / n;
    const barW = Math.max(3, Math.min(slotW * 0.75, 24));

    const bars = points.map((p, i) => {
      const cx  = 12 + i * slotW + slotW / 2;
      const h   = Math.abs(p.deviation) * scale;
      const y   = p.deviation >= 0 ? midY - h : midY;
      const col = p.deviation >= 0 ? "#f59e0b" : "#60a5fa";
      return `<rect x="${(cx - barW / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(h, 1).toFixed(1)}" fill="${col}" rx="1.5"/>`;
    }).join("");

    const zero = `<line x1="8" y1="${midY}" x2="${W - 8}" y2="${midY}" stroke="#3a4255" stroke-width="1"/>`;

    const scaleLabels = `
      <text x="${W - 8}" y="${CT + 8}" text-anchor="end" fill="#4b5563" font-size="8" font-family="DM Mono,monospace">+${maxDev.toFixed(1)}</text>
      <text x="${W - 8}" y="${CB - 2}" text-anchor="end" fill="#4b5563" font-size="8" font-family="DM Mono,monospace">−${maxDev.toFixed(1)}</text>`;

    const fmt = d => d.slice(5).replace("-", "/");
    const dateLabels = `
      <text x="12" y="${H - 4}" fill="#4b5563" font-size="8" font-family="DM Mono,monospace">${fmt(points[0].date)}</text>
      ${points.length > 1 ? `<text x="${W - 12}" y="${H - 4}" text-anchor="end" fill="#4b5563" font-size="8" font-family="DM Mono,monospace">${fmt(points[points.length - 1].date)}</text>` : ""}`;

    return zero + bars + scaleLabels + dateLabels;
  }

  _render() {
    if (!this._config || !this._hass) return;

    const entityId   = this._config.entity;  // forecast_today – provides all attributes
    const tomorrowId = `${this._prefix}_forecast_tomorrow`;
    const actualId   = `${this._prefix}_diagnostics_pv_daily_production`;

    const mainState = this._hass.states[entityId];
    if (!mainState) {
      this.shadowRoot.innerHTML = `<style>${STYLES}</style>
        <div class="card"><div style="color:#64748b;padding:20px;text-align:center">
          ${this._t("entity_not_found")} ${entityId}</div></div>`;
      return;
    }

    const attrs       = mainState.attributes;
    const todayKwh    = parseFloat(mainState.state) || 0;
    const tomorrowKwh = attrs.fused_tomorrow_kwh;
    const uncertainty = attrs.uncertainty_pct;
    const sources     = attrs.sources || {};
    const history     = attrs.history || [];
    const title       = this._config.title || this._t("default_title");
    const lang        = this._hass.language || "en";

    let updatedStr = "—";
    try {
      if (attrs.last_updated)
        updatedStr = new Date(attrs.last_updated)
          .toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" });
    } catch (_) {}

    const sourceList = Object.entries(sources); // [[id, data], ...]
    const maxKwh = Math.max(...sourceList.map(([, s]) => s.today_kwh || 0), 0.1);

    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="card">

        <div class="header">
          <div class="title">${title}</div>
          <div class="updated">${this._t("updated")}<br>${updatedStr}${this._t("time_suffix")}</div>
        </div>

        <!-- Hero: actual + today + tomorrow -->
        <div class="hero">
          <div class="hero-card" data-entity="${actualId}">
            <div class="hero-bar" style="background:#4ade80"></div>
            <div class="hero-label">${this._t("hero_actual")}</div>
            <div class="hero-sublabel" style="visibility:hidden">.</div>
            <div class="hero-value">${this._fmt(parseFloat(this._hass.states[actualId]?.state) || null, 1)}<span class="hero-unit">kWh</span></div>
          </div>
          <div class="hero-card" data-entity="${entityId}">
            <div class="hero-bar" style="background:var(--sf-accent)"></div>
            <div class="hero-label">${this._t("today")}</div>
            <div class="hero-sublabel">${this._t("forecast")}</div>
            <div class="hero-value">${this._fmt(todayKwh, 1)}<span class="hero-unit">kWh</span></div>
            ${uncertainty != null
              ? `<div class="hero-unc">±${this._fmt(uncertainty, 1)} ${this._t("uncertainty")}</div>`
              : ""}
          </div>
          <div class="hero-card" data-entity="${tomorrowId}">
            <div class="hero-bar" style="background:var(--sf-accent2)"></div>
            <div class="hero-label">${this._t("tomorrow")}</div>
            <div class="hero-sublabel">${this._t("forecast")}</div>
            <div class="hero-value">${this._fmt(tomorrowKwh, 1)}<span class="hero-unit">kWh</span></div>
          </div>
        </div>

        <!-- Source bars -->
        ${sourceList.length ? `
        <div class="sources">
          <div class="section-title">${this._t("sources_today")}</div>
          ${sourceList.map(([, s]) => {
            const rmse = this._effectiveRmse(s);
            const exclTitle = s.excluded
              ? this._tf("excluded_reason", { rmse: rmse != null ? this._fmt(rmse, 1) : "—" })
              : "";
            return `
            <div class="source-row${s.excluded ? " excluded" : ""}" data-entity="${entityId}"${s.excluded ? ` title="${this._escape(exclTitle)}"` : ""}>
              <div class="source-name">${SOURCE_SHORT[s.name] || s.name}</div>
              <div class="bar-wrap">
                <div class="bar" style="width:${((s.today_kwh || 0) / maxKwh * 100).toFixed(1)}%"></div>
              </div>
              <div class="source-kwh">
                ${this._fmt(s.today_kwh, 2)} kWh
                ${s.tomorrow_kwh != null ? `<div class="source-kwh-tmr">${this._fmt(s.tomorrow_kwh, 2)} kWh</div>` : ""}
              </div>
              <div class="source-weight">${s.excluded
                ? `<span class="source-excl">${this._t("excluded")}</span>`
                : s.weight != null ? this._t("weight") + " " + this._fmt(s.weight * 100, 0) + " %" : "—"}</div>
            </div>`;
          }).join("")}
        </div>` : ""}

        <!-- Quality table -->
        ${sourceList.length ? `
        <div class="q-section">
          <div class="section-title">${this._t("quality_accuracy")}</div>
          <div class="q-header">
            <span>${this._t("col_source")}</span><span class="q-col-label">${this._t("col_label")}</span><span>${this._t("col_rmse")}</span><span class="q-col-mae">${this._t("col_mae")}</span><span class="q-col-bias">${this._t("col_bias")}</span><span class="q-col-days">${this._t("col_days")}</span>
          </div>
          ${sourceList.map(([, s]) => {
            const raw    = (s.quality_label || "").toLowerCase();
            const key    = QUALITY_ALIAS[raw] || raw;
            const color  = QUALITY[key]?.color || "#94a3b8";
            const qlabel = QUALITY[key] ? this._t(`quality_${key}`) : s.quality_label;
            const bias   = s.bias_kwh != null
              ? (s.bias_kwh > 0 ? "+" : "") + this._fmt(s.bias_kwh, 2) : "—";
            const calShort = this._calShort(s);
            const tooltip  = [
              s.rmse_calibrated_kwh != null ? `${this._t("rmse_calibrated")}: ${this._fmt(s.rmse_calibrated_kwh, 2)} kWh` : null,
              s.calibration_active === false ? this._t("cal_raw_hint") : null,
            ].filter(Boolean).join("\n");
            return `
            <div class="q-row" data-entity="${entityId}"${tooltip ? ` title="${this._escape(tooltip)}"` : ""}>
              <span class="q-name">${SOURCE_SHORT[s.name] || s.name}</span>
              <span class="q-col-label">${s.quality_label
                ? `<span class="badge" style="background:${hexRgba(color,0.25)};border:1px solid ${hexRgba(color,0.75)};color:${color}">${this._escape(qlabel)}</span>${calShort ? `<div class="q-cal">${calShort}</div>` : ""}`
                : "—"}</span>
              <span class="q-val">${this._fmt(s.rmse_kwh, 2)}</span>
              <span class="q-val q-val-mae">${this._fmt(s.mae_kwh, 2)}</span>
              <span class="q-val q-val-bias">${bias}</span>
              <span class="q-days">${s.days_evaluated ?? "—"} ${this._t("days_short")}</span>
            </div>`;
          }).join("")}
        </div>` : ""}

        <!-- Sparkline / history -->
        <div>
          <div class="section-title">${this._t("history_title")}</div>
          <div class="spark-wrap">
            <svg viewBox="0 0 400 80" preserveAspectRatio="none">
              ${this._sparkline(history)}
            </svg>
            <div class="legend">
              <div class="legend-item">
                <div class="legend-dot" style="background:#f59e0b;border-radius:2px"></div>${this._t("over_forecast")}
              </div>
              <div class="legend-item">
                <div class="legend-dot" style="background:#60a5fa;border-radius:2px"></div>${this._t("under_forecast")}
              </div>
            </div>
          </div>
        </div>

      </div>`;

    this._attachListeners();
  }

  static getStubConfig() {
    return { entity: "sensor.solar_fusion_dach_forecast_today" };
  }
}

customElements.define("solar-fusion-card", SolarFusionCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "solar-fusion-card",
  name: "Solar Fusion Card",
  description: "Fused PV forecast with source comparison, quality metrics, and history.",
});
