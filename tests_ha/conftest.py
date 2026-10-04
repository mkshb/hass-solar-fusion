"""Fixtures für die Integrationstests mit Home Assistant.

Ausführen: ``pip install -r requirements_test.txt`` und ``pytest tests_ha``.
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Recorder (Abhängigkeit laut manifest.json) und eigene Integration aktivieren.

    Das Plugin bringt ein eigenes Paket ``custom_components`` mit; unser
    Verzeichnis muss in dessen Suchpfad, sonst wird die Integration nicht gefunden.
    """
    import custom_components

    here = os.path.join(ROOT, "custom_components")
    if here not in custom_components.__path__:
        custom_components.__path__.insert(0, here)
    yield


@pytest.fixture
async def berlin(hass, freezer):
    """Zeitzone, Standort und feste Uhrzeit (04.10.2026 12:00) wie in den Daten."""
    from common import LAT, LON, at

    await hass.config.async_set_time_zone("Europe/Berlin")
    hass.config.latitude, hass.config.longitude = LAT, LON
    freezer.move_to(at("2026-10-04", 12))
    return hass
