"""Migration der gespeicherten Daten (ohne Home-Assistant-Importe, testbar)."""
from __future__ import annotations

from typing import Any, Dict


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
