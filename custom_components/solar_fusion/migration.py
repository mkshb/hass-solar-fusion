"""Migration der gespeicherten Daten (ohne Home-Assistant-Importe, testbar)."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Tuple


def migrate_storage(old_major: int, data: Dict[str, Any]) -> Dict[str, Any]:
    """Gespeicherte Daten auf STORAGE_VERSION bringen.

    v1 → v2: Morgen-Snapshot {date: {source: kWh}} wird zu
    {date: {"daily": {source: kWh}}}. Stundenwerte gab es in v1 nicht; diese
    Tage überspringt das Verschattungslernen.
    """
    if old_major < 2:
        data = dict(data)
        data["morning_snapshots"] = {
            d: {"daily": dict(v)}
            for d, v in (data.get("morning_snapshots") or {}).items()
        }
    return data


def split_entry_options(
    data: Mapping[str, Any], options: Mapping[str, Any], keys: Iterable[str]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Config-Entry 1.1 → 1.2: Tuning-Parameter ``keys`` von data nach options.

    Bis 0.4 schrieb der Options-Flow alle Einstellungen in data; options blieb
    leer. Schon vorhandene options haben Vorrang. Nie gesetzte Schlüssel
    bleiben ungesetzt, der Code nimmt dafür wie bisher den Standardwert.
    """
    new_data = dict(data)
    moved = {key: new_data.pop(key) for key in keys if key in new_data}
    return new_data, {**moved, **options}
