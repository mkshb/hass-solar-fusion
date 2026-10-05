"""Tests für die Migration der gespeicherten Daten (migration.py).

Läuft unter pytest und als Skript (``python3 tests/test_migration.py``), ohne
Home Assistant.
"""
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "solar_fusion")
)
import migration  # noqa: E402


def test_migration_v1_snapshots():
    old = {
        "history": [{"date": "2026-10-01", "source": "solcast",
                     "forecast_kwh": 10.0, "actual_kwh": 9.0}],
        "morning_snapshots": {"2026-10-01": {"solcast": 10.0, "open_meteo_solar_forecast": 11.0}},
        "calibration_state": {"solcast": False},
    }
    new = migration.migrate_storage(1, old)
    assert new["morning_snapshots"] == {
        "2026-10-01": {"daily": {"solcast": 10.0, "open_meteo_solar_forecast": 11.0}}
    }
    assert new["history"] == old["history"]
    assert new["calibration_state"] == old["calibration_state"]
    assert old["morning_snapshots"]["2026-10-01"] == {
        "solcast": 10.0, "open_meteo_solar_forecast": 11.0}   # Eingabe unverändert


def test_migration_v1_without_snapshots():
    assert migration.migrate_storage(1, {"history": []})["morning_snapshots"] == {}



def test_split_entry_options_moves_tuning_keys():
    keys = ("update_interval", "shading_learn", "min_eval_days")
    data = {"sources": ["solcast"], "update_interval": 30, "shading_learn": True}
    new_data, options = migration.split_entry_options(data, {}, keys)
    assert new_data == {"sources": ["solcast"]}
    assert options == {"update_interval": 30, "shading_learn": True}
    assert data["update_interval"] == 30  # Eingabe unverändert


def test_split_entry_options_keeps_existing_options():
    new_data, options = migration.split_entry_options(
        {"update_interval": 30}, {"update_interval": 45, "other": 1}, ("update_interval",))
    assert new_data == {}
    assert options == {"update_interval": 45, "other": 1}


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
