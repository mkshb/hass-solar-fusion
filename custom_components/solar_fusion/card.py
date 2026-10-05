"""Solar Fusion Card: mit der Integration ausliefern.

Die Karte (``frontend/``) wird einmal je HA-Start unter ``/solar_fusion/``
bereitgestellt und als Frontend-Modul eingetragen; eine Dashboard-Ressource
ist nicht nötig.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_DIR = Path(__file__).parent / "frontend"
CARD_FILE = "solar-fusion-card.js"
URL_BASE = f"/{DOMAIN}"


async def async_setup_card(hass: HomeAssistant) -> None:
    """Karte bereitstellen und als Frontend-Modul eintragen."""
    version = (await async_get_integration(hass, DOMAIN)).version
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(CARD_DIR), cache_headers=True)]
    )
    url = f"{URL_BASE}/{CARD_FILE}?v={version}"
    if "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, url)
    else:
        _LOGGER.debug("Frontend not loaded, card not registered")

