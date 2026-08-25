"""Typed session-state accessors for the Streamlit UI (`CLAUDE.md` §5.1).

The only module in the repository permitted to reference `st.session_state`; every other
module reads or mutates app state exclusively through the functions defined here. `AppState`
defines the complete state shape once, and `init_state()` is the single place every key is
given its default.
"""

from __future__ import annotations

from typing import TypedDict, cast

import streamlit as st

from src.domain.models import Level


class AppState(TypedDict):
    """The complete shape of `st.session_state` for this app."""

    level: Level
    selected_farm_id: str | None
    selected_turbine_id: str | None
    layers: dict[str, bool]
    nwp_cache: dict[str, object]
    history_window: str
    history_x_metric: str
    is_mobile: bool


_DEFAULT_LAYERS: dict[str, bool] = {"wind": False, "temperature": False, "forecast": False}
_DEFAULT_HISTORY_WINDOW = "24h"
_DEFAULT_HISTORY_X_METRIC = "wind_speed_ms"


def init_state() -> None:
    """Populate every `AppState` key absent from `st.session_state`.

    Never overwrites a key that already has a value, so calling this on every rerun (as
    `app.py` does) is safe and idempotent. Must run before any other function in this module
    is called.
    """
    defaults: AppState = {
        "level": Level.FLEET,
        "selected_farm_id": None,
        "selected_turbine_id": None,
        "layers": dict(_DEFAULT_LAYERS),
        "nwp_cache": {},
        "history_window": _DEFAULT_HISTORY_WINDOW,
        "history_x_metric": _DEFAULT_HISTORY_X_METRIC,
        # SPEC-GAP: PROJECT_SPEC.md §10.1 rules out JS-based viewport measurement, and no new
        # dependency is available to bridge JS -> Python. The desktop/mobile panel placement
        # itself is decided entirely by CSS (see layout.inject_css); this flag has no live
        # detection wired up in this phase and stays False. get_is_mobile/set_is_mobile exist
        # so a later phase can wire real detection without touching this module's contract.
        "is_mobile": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def get_level() -> Level:
    """Return the current drill-down level."""
    return cast(Level, st.session_state["level"])


def get_selected_farm_id() -> str | None:
    """Return the currently selected farm's ID, or `None` at the fleet level."""
    return cast("str | None", st.session_state["selected_farm_id"])


def get_selected_turbine_id() -> str | None:
    """Return the currently selected turbine's ID, or `None` above the turbine level."""
    return cast("str | None", st.session_state["selected_turbine_id"])


def select_farm(farm_id: str) -> None:
    """Drill down to a farm: set the level to farm and clear any turbine selection.

    Args:
        farm_id: The farm to select.
    """
    st.session_state["level"] = Level.FARM
    st.session_state["selected_farm_id"] = farm_id
    st.session_state["selected_turbine_id"] = None


def select_turbine(turbine_id: str) -> None:
    """Drill down to a turbine, leaving the current farm selection untouched.

    Args:
        turbine_id: The turbine to select.
    """
    st.session_state["level"] = Level.TURBINE
    st.session_state["selected_turbine_id"] = turbine_id


def reset_view() -> None:
    """Return to the fleet level, clearing both selections.

    Per `PROJECT_SPEC.md` §7.2, this must NOT touch `layers` or `nwp_cache` — layer checkbox
    states and cached forecasts survive a reset.
    """
    st.session_state["level"] = Level.FLEET
    st.session_state["selected_farm_id"] = None
    st.session_state["selected_turbine_id"] = None


def get_layer(name: str) -> bool:
    """Return whether the named map layer is enabled.

    Args:
        name: A layer name, e.g. `"wind"`, `"temperature"`, `"forecast"`.

    Returns:
        `False` if `name` is not a recognized layer.
    """
    layers = cast("dict[str, bool]", st.session_state["layers"])
    return layers.get(name, False)


def set_layer(name: str, value: bool) -> None:
    """Enable or disable the named map layer.

    Args:
        name: A layer name, e.g. `"wind"`, `"temperature"`, `"forecast"`.
        value: The new enabled state.
    """
    layers = cast("dict[str, bool]", st.session_state["layers"])
    layers[name] = value
    st.session_state["layers"] = layers


def get_nwp_cached(key: str) -> object | None:
    """Return a previously cached NWP result, if present.

    Args:
        key: The cache key (provider-defined).

    Returns:
        The cached value, or `None` if `key` has never been cached.
    """
    cache = cast("dict[str, object]", st.session_state["nwp_cache"])
    return cache.get(key)


def set_nwp_cached(key: str, value: object) -> None:
    """Store an NWP result in the process-lifetime cache.

    Args:
        key: The cache key (provider-defined).
        value: The value to cache.
    """
    cache = cast("dict[str, object]", st.session_state["nwp_cache"])
    cache[key] = value
    st.session_state["nwp_cache"] = cache


def get_history_window() -> str:
    """Return the selected history window (`"24h"`, `"7d"`, or `"all"`)."""
    return cast(str, st.session_state["history_window"])


def set_history_window(value: str) -> None:
    """Set the selected history window.

    Args:
        value: One of `config.TIME_WINDOWS`'s keys.
    """
    st.session_state["history_window"] = value


def get_history_x_metric() -> str:
    """Return the metric currently selected for the history chart's x-axis."""
    return cast(str, st.session_state["history_x_metric"])


def set_history_x_metric(value: str) -> None:
    """Set the metric selected for the history chart's x-axis.

    Args:
        value: One of `config.METRICS`.
    """
    st.session_state["history_x_metric"] = value


def get_is_mobile() -> bool:
    """Return the mobile/desktop hint used to pick `layout.viewport_padding`'s branch."""
    return cast(bool, st.session_state["is_mobile"])


def set_is_mobile(value: bool) -> None:
    """Set the mobile/desktop hint.

    Args:
        value: `True` for the mobile (bottom-panel) layout.
    """
    st.session_state["is_mobile"] = value
