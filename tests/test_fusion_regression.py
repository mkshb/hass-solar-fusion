"""Regression: FusionEngine.fuse ohne Verschattung liefert bitgenau dasselbe wie v0.2.3.

``fuse_golden_v023.json`` wurde mit dem ``fusion.py`` aus Tag v0.2.3 erzeugt:

    git worktree add /tmp/sf023 v0.2.3
    python3 tests/test_fusion_regression.py --write-golden /tmp/sf023/custom_components/solar_fusion

Läuft unter pytest und als Skript, ohne Home Assistant (siehe ha_stub.py).
"""
import json
import math
import os
import random
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import ha_stub  # noqa: E402

PKG = os.path.join(HERE, "..", "custom_components", "solar_fusion")
GOLDEN = os.path.join(HERE, "fuse_golden_v023.json")

TODAY = date(2026, 10, 4)
TOMORROW = TODAY + timedelta(days=1)
SOURCES = ["forecast_solar", "open_meteo_solar_forecast", "solcast"]


def _bell(day: date, total_kwh: float, peak: float = 13.5, sigma: float = 2.6):
    raw = {
        f"{day.isoformat()}T{h:02d}:00": math.exp(-0.5 * ((h + 0.5 - peak) / sigma) ** 2)
        for h in range(5, 21)
    }
    s = sum(raw.values())
    return {k: round(v / s * total_kwh * 1000.0, 1) for k, v in raw.items()}


def _history(days: int, seed: int, sources=SOURCES):
    rng = random.Random(seed)
    bias = {"forecast_solar": 0.62, "open_meteo_solar_forecast": 1.04, "solcast": 0.97}
    noise = {"forecast_solar": 0.25, "open_meteo_solar_forecast": 0.09, "solcast": 0.10}
    out = []
    for i in range(days, 0, -1):
        d = (TODAY - timedelta(days=i)).isoformat()
        actual = round(rng.uniform(8.0, 48.0), 3)
        for s in sources:
            fc = actual / bias[s] * (1 + rng.gauss(0, noise[s]))
            out.append({"date": d, "source": s, "forecast_kwh": round(max(fc, 0.0), 3),
                        "actual_kwh": actual})
    return out


def _readings(mod, spec):
    out = []
    for sid, today_kwh, tomorrow_kwh, hourly in spec:
        out.append(mod.SourceReading(
            source_id=sid,
            today_kwh=today_kwh,
            tomorrow_kwh=tomorrow_kwh,
            hourly_today=_bell(TODAY, today_kwh, peak=13.0 + 0.5 * SOURCES.index(sid))
            if hourly in ("both", "today") else {},
            hourly_tomorrow=_bell(TOMORROW, tomorrow_kwh, peak=13.2)
            if hourly in ("both",) else {},
        ))
    return out


SCENARIOS = {
    # name: (history days, seed, readings spec [(sid, today, tomorrow, hourly)])
    "no_history": (0, 1, [
        ("forecast_solar", 23.8, 25.1, "none"),
        ("open_meteo_solar_forecast", 38.6, 44.0, "both"),
        ("solcast", 41.3, 41.5, "both"),
    ]),
    "linear_warmup": (5, 2, [
        ("forecast_solar", 23.8, 25.1, "none"),
        ("open_meteo_solar_forecast", 38.6, 44.0, "both"),
        ("solcast", 41.3, 41.5, "both"),
    ]),
    "weighted_14d": (14, 3, [
        ("forecast_solar", 23.8, 25.1, "none"),
        ("open_meteo_solar_forecast", 38.6, 44.0, "both"),
        ("solcast", 41.3, 41.5, "both"),
    ]),
    "isotonic_60d": (60, 4, [
        ("forecast_solar", 23.8, 25.1, "both"),
        ("open_meteo_solar_forecast", 38.6, 44.0, "both"),
        ("solcast", 41.3, 41.5, "both"),
    ]),
    "fallback_profile_from_today": (20, 5, [
        ("open_meteo_solar_forecast", 38.6, 44.0, "today"),
        ("solcast", 41.3, 41.5, "today"),
    ]),
    "fallback_gaussian": (20, 6, [
        ("open_meteo_solar_forecast", 38.6, 44.0, "none"),
        ("solcast", 41.3, 41.5, "none"),
    ]),
    "single_source": (30, 7, [
        ("solcast", 41.3, 41.5, "both"),
    ]),
}


NEW_DETAIL_KEYS = {"shading_corrected_history"}


def run_scenarios(mod, engine_kwargs=None):
    ha_stub.set_now(datetime(2026, 10, 4, 12, 0, tzinfo=ha_stub.TZ))
    result = {}
    for name, (days, seed, spec) in SCENARIOS.items():
        sids = [s[0] for s in spec]
        engine = mod.FusionEngine(_history(days, seed, sids), calibration_state={},
                                  **(engine_kwargs or {}))
        readings = _readings(mod, spec)
        out = {}
        for label, d in (("today", TODAY), ("tomorrow", TOMORROW)):
            fused, unc, weights = engine.fuse(readings, d)
            out[label] = {"fused": fused, "uncertainty": unc, "weights": weights}
        # Neue Diagnosefelder (gab es in v0.2.3 nicht) gehören nicht zum Vergleich
        out["weight_details"] = {
            sid: {k: v for k, v in info.items() if k not in NEW_DETAIL_KEYS}
            for sid, info in engine.compute_weights(sids).items()
        }
        result[name] = out
    return result


def _golden():
    with open(GOLDEN, encoding="utf-8") as fh:
        return json.load(fh)


def _current():
    return ha_stub.load_fusion(PKG, "sf_current")


def _roundtrip(obj):
    return json.loads(json.dumps(obj))


def test_fuse_matches_v023_without_shading():
    assert _roundtrip(run_scenarios(_current())) == _golden()


def test_fuse_matches_v023_with_empty_shading_map():
    mod = _current()
    got = run_scenarios(mod, {"shading_cells": {}, "shading_apply": True,
                              "location": (53.55, 9.99)})
    assert _roundtrip(got) == _golden()


def test_fuse_matches_v023_with_map_but_apply_off():
    mod = _current()
    cells = {"245:12": {"az": 247.5, "el": 13.0, "factor": 0.2, "n": 9, "learned": True}}
    got = run_scenarios(mod, {"shading_cells": cells, "shading_apply": False,
                              "location": (53.55, 9.99)})
    assert _roundtrip(got) == _golden()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write-golden":
        data = run_scenarios(ha_stub.load_fusion(sys.argv[2], "sf_golden"))
        with open(GOLDEN, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        print("wrote", GOLDEN)
        sys.exit(0)
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
