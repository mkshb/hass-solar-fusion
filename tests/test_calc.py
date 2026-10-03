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


# ── inverse_spread_weights (B: fallback) ────────────────────────────────────

def test_weights_lower_spread_gets_more():
    w = calc.inverse_spread_weights({"a": 5.0, "b": 20.0})
    assert w["a"] > w["b"]
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_missing_source_does_not_collapse_to_equal():
    # Old behaviour: one None → all equal. New: data-less source filled with the
    # mean spread, established sources keep their relative weighting.
    w = calc.inverse_spread_weights({"a": 5.0, "b": 20.0, "c": None})
    assert w["a"] > w["c"] > w["b"]          # not all equal
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert abs(w["a"] - 1.0 / 3) > 1e-3      # genuinely not equal


def test_weights_all_missing_equal():
    w = calc.inverse_spread_weights({"a": None, "b": None})
    assert w == {"a": 0.5, "b": 0.5}


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
