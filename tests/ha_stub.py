"""Minimal stand-ins for the Home Assistant modules fusion.py imports.

Lets tests load ``fusion.py`` (and its relative imports) without a Home
Assistant install. Only what fusion/source_reader touch at import time or in
``FusionEngine`` is provided; the clock is fixed via ``set_now``.
"""
import importlib
import os
import sys
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
_now = [datetime(2026, 10, 4, 12, 0, tzinfo=TZ)]


def set_now(when: datetime) -> None:
    _now[0] = when


def _install() -> None:
    if "homeassistant.util.dt" in sys.modules and getattr(
        sys.modules["homeassistant.util.dt"], "_solar_fusion_stub", False
    ):
        return
    ha = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    core = types.ModuleType("homeassistant.core")

    dt._solar_fusion_stub = True
    dt.now = lambda: _now[0]
    dt.get_default_time_zone = lambda: TZ
    dt.as_utc = lambda d: d.astimezone(timezone.utc)
    dt.as_local = lambda d: d.astimezone(TZ)
    dt.parse_datetime = lambda s: datetime.fromisoformat(s)

    class HomeAssistant:  # noqa: D401 – placeholder type only
        pass

    core.HomeAssistant = HomeAssistant
    core.callback = lambda f: f

    ha.util = util
    util.dt = dt
    ha.core = core
    sys.modules.update({
        "homeassistant": ha,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
        "homeassistant.core": core,
    })


def load_module(pkg_dir: str, module: str, name: str = "sf_pkg"):
    """Import ``module`` from ``pkg_dir`` as part of a synthetic package ``name``.

    The package ``__init__`` (which needs the full Home Assistant) is not run.
    """
    _install()
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [os.path.abspath(pkg_dir)]
        sys.modules[name] = pkg
    return importlib.import_module(f"{name}.{module}")


def load_fusion(pkg_dir: str, name: str = "sf_pkg"):
    """Import ``fusion`` from ``pkg_dir`` (see load_module)."""
    return load_module(pkg_dir, "fusion", name)
