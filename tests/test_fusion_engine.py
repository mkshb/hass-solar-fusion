"""FusionEngine: Qualitätsmetriken, Isotonic-Cache, Verschattungsverhältnisse, Kartenwechsel.

Läuft unter pytest und als Skript, ohne Home Assistant (siehe ha_stub.py).
"""
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
from test_fusion_regression import TODAY, TOMORROW  # noqa: E402
from test_fusion_shading import CELLS, _engine, _reading, fusion  # noqa: E402

OM = "open_meteo_solar_forecast"


def _records(sid, days, error=0.0, actual=20.0, first=1):
    """Ein Datensatz je Tag, ``first`` bis ``first + days - 1`` Tage vor heute."""
    return [
        {"date": (TODAY - timedelta(days=i)).isoformat(), "source": sid,
         "forecast_kwh": round(actual + error + (i % 5), 3), "actual_kwh": actual + (i % 5)}
        for i in range(first, first + days)
    ]


# ── source_quality ──────────────────────────────────────────────────────────

def test_quality_metrics():
    errors = [2.0, 2.0, -1.0, 1.0]
    history = [
        {"date": (TODAY - timedelta(days=i + 1)).isoformat(), "source": "solcast",
         "forecast_kwh": 20.0 + e, "actual_kwh": 20.0}
        for i, e in enumerate(errors)
    ]
    # Außerhalb des 14-Tage-Fensters: zählt nicht für die Metriken
    history.append({"date": (TODAY - timedelta(days=30)).isoformat(), "source": "solcast",
                    "forecast_kwh": 99.0, "actual_kwh": 1.0})
    q = _engine(history).source_quality()["solcast"]
    assert q == {
        "rmse": 1.581,          # √((4 + 4 + 1 + 1) / 4)
        "rmse_pct": 7.9,
        "mae": 1.5,
        "bias": 1.0,
        "std": 1.225,           # √((1 + 1 + 4 + 0) / 4)
        "std_pct": 6.1,
        "bias_pct": 5.0,
        "days_evaluated": 4,
        "calibration_mode": "linear_bias (4 recent pts)",
    }


def test_quality_without_history_and_calibration_modes():
    history = _records("solcast", 2) + _records(OM, 22)
    q = _engine(history).source_quality()
    assert q["forecast_solar"]["days_evaluated"] == 0
    assert q["forecast_solar"]["calibration_mode"] == "none"
    assert q["forecast_solar"]["rmse"] is None
    assert q["solcast"]["calibration_mode"] == "none (insufficient data)"
    # 22 Tage im Saisonfenster (Sep–Nov), davon 14 im Fenster der Metriken
    assert q[OM]["calibration_mode"] == "isotonic (22 seasonal pts)"
    assert q[OM]["days_evaluated"] == 14


# ── Isotonic-Cache ──────────────────────────────────────────────────────────

def test_record_actual_invalidates_isotonic_cache_of_affected_sources():
    engine = _engine(_records("solcast", 22, error=3.0) + _records(OM, 22, error=-2.0),
                     shading_apply=False)
    before = engine._calibrate("solcast", 30.0, TODAY.month)
    engine._calibrate(OM, 30.0, TODAY.month)
    assert set(engine._iso_cache) == {"solcast", OM}

    # Neuer Tag im Saisonfenster nur für Solcast: nur dessen Kurve wird neu angepasst
    engine.record_actual(TODAY, 10.0, [_reading("solcast", 30.0)])
    assert set(engine._iso_cache) == {OM}
    assert engine._calibrate("solcast", 30.0, TODAY.month) < before

    # Ein Tag außerhalb von ±1 Monat lässt den Cache stehen
    engine.record_actual(date(2026, 7, 1), 10.0, [_reading("solcast", 30.0)])
    assert set(engine._iso_cache) == {"solcast", OM}


def test_record_actual_replaces_the_day_and_stores_corrected():
    engine = _engine(_records("solcast", 3))
    readings = [_reading("solcast", 30.0), _reading(OM, 28.0)]
    engine.record_actual(TODAY, 25.0, readings)
    engine.record_actual(TODAY, 26.0, readings, corrected={"solcast": 24.0})
    today = [r for r in engine._history if r["date"] == TODAY.isoformat()]
    assert sorted(today, key=lambda r: r["source"]) == [
        {"date": TODAY.isoformat(), "source": OM, "forecast_kwh": 28.0, "actual_kwh": 26.0},
        {"date": TODAY.isoformat(), "source": "solcast", "forecast_kwh": 30.0,
         "actual_kwh": 26.0, "forecast_corrected_kwh": 24.0},
    ]


# ── shading_ratios ──────────────────────────────────────────────────────────

def test_shading_ratios_per_source():
    readings = [
        _reading(OM, 38.0),
        _reading("solcast", 41.0, hourly=False),
        _reading("forecast_solar", 35.0),
    ]
    engine = _engine(horizon_sources={"forecast_solar"})
    ratios = engine.shading_ratios(readings, TODAY)
    assert 0.0 < ratios[OM] < 1.0
    # Ohne Stundenwerte: Mittel der übrigen korrigierten Quellen; Horizont-Quelle: 1,0
    assert ratios["solcast"] == ratios[OM]
    assert ratios["forecast_solar"] == 1.0
    # Gleiche Verhältnisse wie in der Fusion (Grundlage des Morgen-Snapshots)
    engine.fuse(readings, TODAY)
    assert engine.last_shading_ratios[TODAY.isoformat()] == ratios


def test_shading_ratios_tomorrow_and_without_apply():
    readings = [_reading(OM, 38.0)]
    assert _engine().shading_ratios(readings, TOMORROW)[OM] < 1.0
    assert _engine(shading_apply=False).shading_ratios(readings, TODAY) == {OM: 1.0}
    assert _engine(location=None).shading_ratios(readings, TODAY) == {OM: 1.0}


# ── set_shading_cells ───────────────────────────────────────────────────────

def test_set_shading_cells_switches_map_and_clears_caches():
    history = _records("solcast", 22) + _records(OM, 22)
    readings = [_reading(OM, 38.0), _reading("solcast", 41.0)]
    engine = _engine(history)
    shaded = engine.fuse(readings, TODAY)
    engine._calibrate("solcast", 30.0, TODAY.month)
    assert engine.shading_active and engine._iso_cache and engine._weight_cache

    engine.set_shading_cells({})
    assert not engine.shading_active
    assert engine._iso_cache == {} and engine._weight_cache is None
    assert engine.fuse(readings, TODAY) == _engine(history, shading_cells={}).fuse(readings, TODAY)

    engine.set_shading_cells(CELLS)
    assert engine.fuse(readings, TODAY) == shaded


def test_unlearned_cells_do_not_activate_shading():
    cells = {k: {**v, "learned": False} for k, v in CELLS.items()}
    engine = _engine(shading_cells=cells)
    assert not engine.shading_active
    assert engine.slot_factors({f"{TODAY.isoformat()}T17:00"}) == {}


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
