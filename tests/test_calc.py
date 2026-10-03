"""Unit tests for the dependency-free numeric core (custom_components/solar_fusion/calc.py).

Runs both under pytest and as a plain script (``python3 tests/test_calc.py``),
so it can be executed without a Home Assistant install.
"""
import math
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "solar_fusion")
)
import calc  # noqa: E402


# ── daily_total_from_increasing (F1 carryover fix) ──────────────────────────

def test_daily_total_ignores_carryover_at_window_start():
    # Previous day's total (60.029) carried into the window, then reset to 0,
    # then today's real production climbs to 50.606.
    seq = [60.029, 0.0, 10.0, 25.0, 40.0, 50.606]
    assert calc.daily_total_from_increasing(seq) == 50.606


def test_daily_total_lifetime_counter_no_reset():
    # A cumulative meter that never resets → last - first.
    assert calc.daily_total_from_increasing([10.0, 12.0, 15.0, 20.0]) == 10.0


def test_daily_total_empty_and_single():
    assert calc.daily_total_from_increasing([]) == 0.0
    assert calc.daily_total_from_increasing([42.0]) == 0.0


def test_daily_total_sums_positive_deltas_across_resets():
    assert calc.daily_total_from_increasing([0.0, 20.0, 40.0, 0.0, 15.0, 30.0]) == 70.0


def test_daily_total_max_minus_min_would_be_wrong():
    # Regression guard: the old max-min approach returns the carryover (60.029);
    # the delta sum returns today's real total.
    seq = [60.029, 0.0, 30.0, 50.606]
    assert max(seq) - min(seq) == 60.029
    assert calc.daily_total_from_increasing(seq) == 50.606


# ── isotonic regression ─────────────────────────────────────────────────────

def test_isotonic_pools_violation_and_is_monotone():
    kx, ky = calc.isotonic_fit([1.0, 2.0, 3.0], [1.0, 3.0, 2.0])
    assert kx == [1.0, 2.5]
    assert ky == [1.0, 2.5]
    assert all(ky[i] <= ky[i + 1] for i in range(len(ky) - 1))


def test_isotonic_predict_interpolates_and_extrapolates_flat():
    kx, ky = calc.isotonic_fit([1.0, 2.0, 3.0], [1.0, 3.0, 2.0])
    assert abs(calc.isotonic_predict(kx, ky, 2.0) - 2.0) < 1e-9
    assert calc.isotonic_predict(kx, ky, 0.0) == 1.0      # flat below
    assert calc.isotonic_predict(kx, ky, 99.0) == 2.5     # flat above


def test_isotonic_empty():
    assert calc.isotonic_fit([], []) == ([], [])
    assert calc.isotonic_predict([], [], 5.0) == 5.0


# ── quality_label (A: bias vs scatter) ──────────────────────────────────────

def test_label_biased_but_consistent_is_corrected_not_good():
    # Forecast.Solar live case: low scatter, large bias → correctable.
    assert calc.quality_label(14.2, 41.6) == "Verzerrt (korrigiert)"


def test_label_accurate():
    assert calc.quality_label(13.2, 1.1) == "Genau"


def test_label_noisy_and_bad():
    assert calc.quality_label(17.9, 4.1) == "Unruhig"
    assert calc.quality_label(35.0, 2.0) == "Schlecht"


def test_label_high_scatter_overrides_bias():
    # A noisy source is not "corrected" even if it also has bias.
    assert calc.quality_label(40.0, 50.0) == "Schlecht"


def test_label_none_scatter():
    assert calc.quality_label(None, 10.0) is None


# ── inverse_variance_weights (1/RMSE², Ausschluss, Absicherungen) ──────────

LIVE_RMSE = {"forecast_solar": 11.92, "open_meteo_solar_forecast": 4.00, "solcast": 4.27}
LIVE_DAYS = {s: 14 for s in LIVE_RMSE}


def _w(result):
    return {s: info["weight"] for s, info in result.items()}


def test_ivar_live_values_without_exclusion():
    # Werte vom 03.10.2026 (14 Tage). Ohne Ausschluss (k groß) ergibt 1/RMSE²
    # etwa 0,05 / 0,50 / 0,45.
    w = _w(calc.inverse_variance_weights(LIVE_RMSE, LIVE_DAYS, exclusion_factor=10.0))
    assert abs(w["forecast_solar"] - 0.057) < 0.005
    assert abs(w["open_meteo_solar_forecast"] - 0.502) < 0.005
    assert abs(w["solcast"] - 0.441) < 0.005
    assert abs(sum(w.values()) - 1.0) < 1e-3


def test_ivar_live_values_default_excludes_forecast_solar():
    # 11,92 > 2,0 × 4,00 → Forecast.Solar fällt raus, der Rest wird neu normiert.
    r = calc.inverse_variance_weights(LIVE_RMSE, LIVE_DAYS)
    fs = r["forecast_solar"]
    assert fs["weight"] == 0.0
    assert fs["excluded"] is True
    assert "11.9" in fs["exclusion_reason"] and "4.0" in fs["exclusion_reason"]
    assert r["open_meteo_solar_forecast"]["excluded"] is False
    assert r["solcast"]["exclusion_reason"] is None
    w = _w(r)
    assert abs(w["open_meteo_solar_forecast"] - 0.532) < 0.005
    assert abs(w["solcast"] - 0.468) < 0.005
    assert abs(sum(w.values()) - 1.0) < 1e-3


def test_ivar_exclusion_boundary():
    # Genau k × beste Quelle bleibt drin, knapp darüber fliegt raus.
    days = {"a": 10, "b": 10}
    assert calc.inverse_variance_weights({"a": 2.0, "b": 4.0}, days)["b"]["excluded"] is False
    assert calc.inverse_variance_weights({"a": 2.0, "b": 4.01}, days)["b"]["excluded"] is True


def test_ivar_too_few_days_equal_weights():
    r = calc.inverse_variance_weights(LIVE_RMSE, {s: 6 for s in LIVE_RMSE})
    assert all(abs(i["weight"] - 1 / 3) < 1e-3 for i in r.values())
    assert not any(i["excluded"] for i in r.values())


def test_ivar_new_source_in_warmup_does_not_reset_others():
    # Solcast neu (3 Tage): Forecast.Solar bleibt ausgeschlossen, Solcast bekommt
    # die mittlere inverse Varianz der verbleibenden Quellen (hier nur Open-Meteo).
    days = {"forecast_solar": 14, "open_meteo_solar_forecast": 14, "solcast": 3}
    r = calc.inverse_variance_weights(LIVE_RMSE, days)
    assert r["forecast_solar"]["excluded"] is True
    assert r["solcast"]["excluded"] is False
    assert abs(r["solcast"]["weight"] - 0.5) < 1e-3
    assert abs(r["open_meteo_solar_forecast"]["weight"] - 0.5) < 1e-3


def test_ivar_missing_source_renormalises():
    # Solcast liefert gerade nicht → nur noch zwei aktive Quellen.
    rmse = {"forecast_solar": 11.92, "open_meteo_solar_forecast": 4.00}
    r = calc.inverse_variance_weights(rmse, LIVE_DAYS)
    assert set(r) == set(rmse)
    assert r["forecast_solar"]["excluded"] is True
    assert r["open_meteo_solar_forecast"]["weight"] == 1.0


def test_ivar_best_source_missing_threshold_follows_remaining_best():
    # Fällt die beste Quelle aus, ist die Schwelle relativ zur besten verbleibenden.
    rmse = {"forecast_solar": 11.92, "solcast": 4.27}
    r = calc.inverse_variance_weights(rmse, LIVE_DAYS)
    assert r["forecast_solar"]["excluded"] is True     # 11,92 > 8,54
    assert r["solcast"]["weight"] == 1.0


def test_ivar_all_bad_but_similar_none_excluded():
    # Alle gleich schlecht: der Ausschluss ist relativ, also bleibt jede drin.
    r = calc.inverse_variance_weights({"a": 10.0, "b": 12.0, "c": 15.0}, {"a": 9, "b": 9, "c": 9})
    assert not any(i["excluded"] for i in r.values())
    w = _w(r)
    assert w["a"] > w["b"] > w["c"]


def test_ivar_all_excluded_falls_back_to_best():
    # Nur mit k < 1 möglich (der Options-Flow erlaubt das nicht) – dann zählt die beste Quelle.
    r = calc.inverse_variance_weights({"a": 3.0, "b": 5.0}, {"a": 9, "b": 9}, exclusion_factor=0.5)
    assert r["a"] == {"weight": 1.0, "excluded": False, "exclusion_reason": None}
    assert r["b"]["weight"] == 0.0 and r["b"]["excluded"] is True


def test_ivar_rmse_floor_prevents_near_zero_division():
    # 0,01 kWh würde ohne Untergrenze 99,9 % des Gewichts bekommen.
    w = _w(calc.inverse_variance_weights({"a": 0.01, "b": 0.6}, {"a": 9, "b": 9}))
    assert abs(w["a"] - (1 / 0.25) / (1 / 0.25 + 1 / 0.36)) < 1e-3
    assert w["b"] > 0.4


def test_ivar_single_and_empty():
    assert calc.inverse_variance_weights({}, {}) == {}
    r = calc.inverse_variance_weights({"a": 7.0}, {"a": 14})
    assert r["a"]["weight"] == 1.0 and r["a"]["excluded"] is False


# ── use_calibration (Gating) ───────────────────────────────────────────────

def test_gating_keeps_calibration_when_it_helps():
    # Forecast.Solar 03.10.: roh 11,92, kalibriert 9,75 kWh → kalibrieren.
    assert calc.use_calibration(11.92, 9.75, 14) is True


def test_gating_drops_calibration_when_it_hurts():
    # Open-Meteo / Solcast 03.10.: Kalibrierung verschlechtert → roh fusionieren.
    assert calc.use_calibration(4.00, 5.63, 14) is False
    assert calc.use_calibration(4.27, 5.90, 14) is False


def test_gating_tie_uses_raw():
    assert calc.use_calibration(5.0, 5.0, 14) is False


def test_gating_defaults_to_calibration_without_enough_days():
    assert calc.use_calibration(4.0, 6.0, 6) is True
    assert calc.use_calibration(4.0, 6.0, 3, min_days=3) is False
    assert calc.use_calibration(None, None, 0) is True


def test_gating_hysteresis_keeps_previous_decision_in_band():
    # Kalibriert 5 % schlechter als roh: ohne Vorgeschichte → roh, war bisher
    # kalibriert → bleibt kalibriert (innerhalb von 10 %).
    assert calc.use_calibration(4.0, 4.2, 14) is False
    assert calc.use_calibration(4.0, 4.2, 14, previous=True) is True
    # Kalibriert 5 % besser: war bisher roh → bleibt roh.
    assert calc.use_calibration(4.0, 3.8, 14) is True
    assert calc.use_calibration(4.0, 3.8, 14, previous=False) is False


def test_gating_hysteresis_switches_outside_band():
    assert calc.use_calibration(4.0, 4.5, 14, previous=True) is False   # +12,5 %
    assert calc.use_calibration(4.0, 3.5, 14, previous=False) is True   # −12,5 %
    # Live-Fall: Open-Meteo 4,00 roh vs. 5,63 kalibriert → klar roh, auch mit Vorgeschichte.
    assert calc.use_calibration(4.00, 5.63, 14, previous=True) is False


def test_gating_hysteresis_band_edges():
    assert calc.use_calibration(10.0, 11.0, 14, previous=True) is True   # genau +10 %
    assert calc.use_calibration(10.0, 9.0, 14, previous=False) is False  # genau −10 %


def test_gating_warmup_ignores_previous():
    assert calc.use_calibration(4.0, 6.0, 5, previous=False) is True


def test_gating_and_weights_live_case_excludes_forecast_solar():
    # Gewicht aus dem Fehler des tatsächlich fusionierten Wertes:
    # FS kalibriert 9,75, OM roh 4,00, SC roh 4,27 → 9,75 > 2 × 4,00 → FS raus.
    raw = {"forecast_solar": 11.92, "open_meteo_solar_forecast": 4.00, "solcast": 4.27}
    cal = {"forecast_solar": 9.75, "open_meteo_solar_forecast": 5.63, "solcast": 5.90}
    used = {s: cal[s] if calc.use_calibration(raw[s], cal[s], 14) else raw[s] for s in raw}
    r = calc.inverse_variance_weights(used, LIVE_DAYS)
    assert r["forecast_solar"]["excluded"] is True
    assert abs(r["open_meteo_solar_forecast"]["weight"] - 0.532) < 0.005


def test_rmse_helper():
    assert calc.rmse([]) is None
    assert abs(calc.rmse([3.0, -4.0]) - math.sqrt(12.5)) < 1e-9


# ── weighted_spread_pct (F4 + 1-source) ─────────────────────────────────────

def test_spread_none_for_single_source():
    assert calc.weighted_spread_pct({"a": 50.0}, {"a": 1.0}, 50.0) is None


def test_spread_reflects_raw_disagreement():
    vals = {"a": 38.0, "b": 67.0, "c": 59.0}
    weights = {"a": 0.2, "b": 0.39, "c": 0.41}
    pct = calc.weighted_spread_pct(vals, weights, ref=61.0)
    assert 10.0 < pct < 25.0   # meaningful, not the old ~0.6 %


# ── linear_bias_factor (C: widened clamp) ───────────────────────────────────

def test_bias_factor_clamped_to_new_bounds():
    assert calc.linear_bias_factor(30.0, 90.0) == 2.0    # 3.0 clamped to 2.0
    assert calc.linear_bias_factor(100.0, 10.0) == 0.5   # 0.1 clamped to 0.5
    assert abs(calc.linear_bias_factor(50.0, 55.0) - 1.1) < 1e-9
    assert calc.linear_bias_factor(0.0, 50.0) == 1.0     # guard


# ── plain-script runner (no pytest needed) ──────────────────────────────────

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "->", e or "assertion")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, "->", repr(e))
    print("\n%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
