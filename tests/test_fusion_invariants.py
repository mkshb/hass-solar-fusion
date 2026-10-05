"""Invarianten von FusionEngine.fuse über viele zufällige (reproduzierbare) Eingaben.

Läuft unter pytest und als Skript, ohne Home Assistant (siehe ha_stub.py).
"""
import math
import os
import random
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import ha_stub  # noqa: E402

PKG = os.path.join(HERE, "..", "custom_components", "solar_fusion")
fusion = ha_stub.load_fusion(PKG, "sf_inv")
calc = sys.modules["sf_inv.calc"]

TODAY = datetime(2026, 10, 4).date()
TOMORROW = TODAY + timedelta(days=1)
SOURCES = ["forecast_solar", "open_meteo_solar_forecast", "solcast"]
LOCATION = (53.55, 9.99)
N_CASES = 150


def _bell(rng, day, kwh):
    peak, sigma = rng.uniform(12.0, 15.0), rng.uniform(2.0, 3.5)
    raw = {f"{day.isoformat()}T{h:02d}:00": math.exp(-0.5 * ((h + 0.5 - peak) / sigma) ** 2)
           for h in range(5, 21)}
    total = sum(raw.values())
    return {k: v / total * kwh * 1000.0 for k, v in raw.items()}


def _case(seed):
    rng = random.Random(seed)
    sids = rng.sample(SOURCES, rng.randint(1, 3))
    readings = []
    for sid in sids:
        today, tomorrow = rng.uniform(0.0, 60.0), rng.uniform(0.0, 60.0)
        hourly = rng.random() < 0.8
        readings.append(fusion.SourceReading(
            sid, today, tomorrow,
            _bell(rng, TODAY, today) if hourly else {},
            _bell(rng, TOMORROW, tomorrow) if hourly and rng.random() < 0.8 else {},
        ))
    history = []
    corrected = rng.random() < 0.5
    for i in range(rng.choice([0, 5, 14, 40]), 0, -1):
        actual = rng.uniform(5.0, 50.0)
        for sid in sids:
            rec = {"date": (TODAY - timedelta(days=i)).isoformat(), "source": sid,
                   "forecast_kwh": round(actual * rng.uniform(0.6, 1.5), 3),
                   "actual_kwh": round(actual, 3)}
            if corrected:
                rec["forecast_corrected_kwh"] = round(rec["forecast_kwh"] * rng.uniform(0.85, 1.0), 3)
            history.append(rec)
    cells = {}
    for _ in range(rng.randint(0, 40)):
        az, el = rng.uniform(60, 300), rng.uniform(0, 40)
        key = calc.shading_cell(az, el)
        a, e = (int(x) for x in key.split(":"))
        cells[key] = {"az": a + 2.5, "el": e + 1.0,
                      "factor": round(rng.uniform(calc.SHADING_FACTOR_MIN, calc.SHADING_FACTOR_MAX), 3),
                      "n": 5, "n_pooled": 5, "learned": rng.random() < 0.9}
    horizon = [s for s in sids if rng.random() < 0.3]
    return readings, history, cells, horizon


def _engine(history, cells, horizon, apply=True):
    return fusion.FusionEngine(list(history), calibration_state={}, shading_cells=cells,
                               shading_apply=apply, horizon_sources=horizon, location=LOCATION)


def _cases():
    ha_stub.set_now(datetime(2026, 10, 4, 12, 0, tzinfo=ha_stub.TZ))
    for seed in range(N_CASES):
        yield (seed, *_case(seed))


def test_outputs_are_well_formed():
    for seed, readings, history, cells, horizon in _cases():
        engine = _engine(history, cells, horizon)
        for day in (TODAY, TOMORROW):
            fused, unc, weights = engine.fuse(readings, day)
            assert all(v >= 0.0 for v in fused.values()), seed
            assert all(k.startswith(day.isoformat()) for k in fused), seed
            assert abs(sum(weights.values()) - 1.0) < 1e-3, (seed, weights)
            assert all(0.0 <= w <= 1.0 for w in weights.values()), seed
            if len(readings) == 1:
                assert unc is None, seed
            elif unc is not None:
                assert 0.0 <= unc <= 100.0, seed


def test_shading_ratios_are_bounded_and_horizon_untouched():
    for seed, readings, history, cells, horizon in _cases():
        engine = _engine(history, cells, horizon)
        for day in (TODAY, TOMORROW):
            engine.fuse(readings, day)
            ratios = engine.last_shading_ratios[day.isoformat()]
            for sid, r in ratios.items():
                assert calc.SHADING_FACTOR_MIN - 1e-12 <= r <= calc.SHADING_FACTOR_MAX + 1e-12, (seed, sid, r)
                if sid in horizon:
                    assert r == 1.0, (seed, sid)


def test_shading_never_raises_the_daily_total():
    # Vergleich mit leerer Karte bei sonst gleicher Konfiguration (gleicher
    # Kalibrierungsmodus): Zellen mit Faktor ≤ 1 senken oder halten die Tagessumme.
    for seed, readings, history, cells, horizon in _cases():
        cells = {k: {**c, "factor": min(1.0, c["factor"])} for k, c in cells.items()}
        shaded = _engine(history, cells, horizon)
        empty = _engine(history, {}, horizon)
        for day in (TODAY, TOMORROW):
            a, _, wa = shaded.fuse(readings, day)
            b, _, wb = empty.fuse(readings, day)
            assert wa == wb, seed
            assert sum(a.values()) <= sum(b.values()) + 0.05 * len(b) + 1e-6, (seed, day)


def test_empty_map_without_corrected_history_equals_apply_off():
    for seed, readings, history, cells, horizon in _cases():
        raw_history = [{k: v for k, v in r.items() if k != "forecast_corrected_kwh"}
                       for r in history]
        on = _engine(raw_history, {}, horizon, apply=True)
        off = _engine(raw_history, cells, horizon, apply=False)
        for day in (TODAY, TOMORROW):
            assert on.fuse(readings, day) == off.fuse(readings, day), seed


def test_fused_total_matches_hourly_sum_of_sources_scale():
    # Ohne Verschattung und Kalibrierung (keine Historie) ist die Tagessumme das
    # gewichtete Mittel der Tageswerte der Quellen mit Stundenwerten oder Profil.
    for seed, readings, _history, _cells, horizon in _cases():
        engine = _engine([], {}, horizon)
        fused, _, weights = engine.fuse(readings, TODAY)
        expected = sum(r.today_kwh * weights[r.source_id] for r in readings) * 1000.0
        if expected > 0:
            assert abs(sum(fused.values()) - expected) <= 0.05 * len(fused) + 1e-6, seed


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
