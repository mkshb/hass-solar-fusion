"""Übersetzungen: vollständig und deckungsgleich mit services.yaml.

Läuft unter pytest und als Skript, ohne Home Assistant und ohne PyYAML.
"""
import json
import os
import re
import sys

PKG = os.path.join(os.path.dirname(__file__), "..", "custom_components", "solar_fusion")
FILES = ["strings.json", "translations/en.json", "translations/de.json"]


def _load(name):
    with open(os.path.join(PKG, name), encoding="utf-8") as fh:
        return json.load(fh)


def _services_yaml():
    """{service: [field, …]} aus services.yaml (Einrückung: 0 Service, 4 Feld)."""
    services, current, in_fields = {}, None, False
    with open(os.path.join(PKG, "services.yaml"), encoding="utf-8") as fh:
        for line in fh:
            if m := re.match(r"^(\w+):\s*$", line):
                current, in_fields = m.group(1), False
                services[current] = []
            elif re.match(r"^  fields:\s*$", line):
                in_fields = True
            elif re.match(r"^  \w+:", line):
                in_fields = False
            elif in_fields and (m := re.match(r"^    (\w+):\s*$", line)):
                services[current].append(m.group(1))
    return services


def _keys(obj, prefix=""):
    if isinstance(obj, dict):
        out = set()
        for k, v in obj.items():
            out |= {f"{prefix}{k}"} | _keys(v, f"{prefix}{k}.")
        return out
    return set()


def test_every_service_and_field_is_translated():
    services = _services_yaml()
    assert set(services) >= {"take_snapshot", "repair_history", "learn_shading"}
    for name in FILES:
        translated = _load(name).get("services", {})
        for service, fields in services.items():
            assert service in translated, (name, service)
            assert translated[service].get("name"), (name, service)
            assert translated[service].get("description"), (name, service)
            assert set(translated[service].get("fields", {})) == set(fields), (name, service)


def test_translation_files_have_the_same_keys():
    keys = {name: _keys(_load(name)) for name in FILES}
    assert keys["strings.json"] == keys["translations/en.json"]
    assert keys["strings.json"] == keys["translations/de.json"], (
        keys["strings.json"] ^ keys["translations/de.json"])


def test_english_translation_matches_strings():
    assert _load("strings.json") == _load("translations/en.json")


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
