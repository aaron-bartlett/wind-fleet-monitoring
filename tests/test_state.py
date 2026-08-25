"""Tests for `src/ui/state.py`: defaults and the transition table in `PROJECT_SPEC.md` §7.2.

`st.session_state` is unusable outside a running Streamlit script (no `ScriptRunContext`), so
every test monkeypatches `streamlit.session_state` to a plain `dict` before exercising
`state.py` — a `dict` supports every operation `state.py` performs (`in`, `[key]`, `.get`),
so no Streamlit runtime is required. Per `CLAUDE.md` §5.7, this file asserts on values, not on
"no exception raised".
"""

import pytest
import streamlit

from src.domain.models import Level
from src.ui import state


@pytest.fixture(autouse=True)
def _stub_session_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(streamlit, "session_state", {})


# --------------------------------------------------------------------------------------
# init_state
# --------------------------------------------------------------------------------------


def test_init_state_sets_every_default() -> None:
    state.init_state()

    assert state.get_level() == Level.FLEET
    assert state.get_selected_farm_id() is None
    assert state.get_selected_turbine_id() is None
    assert state.get_layer("wind") is False
    assert state.get_layer("temperature") is False
    assert state.get_layer("forecast") is False
    assert state.get_nwp_cached("anything") is None
    assert state.get_history_window() == "24h"
    assert state.get_history_x_metric() == "wind_speed_ms"
    assert state.get_is_mobile() is False


def test_init_state_does_not_clobber_an_existing_value() -> None:
    state.init_state()
    state.select_farm("FARM01")

    state.init_state()  # simulates a rerun

    assert state.get_level() == Level.FARM
    assert state.get_selected_farm_id() == "FARM01"


# --------------------------------------------------------------------------------------
# select_farm
# --------------------------------------------------------------------------------------


def test_select_farm_sets_level_and_farm_clears_turbine() -> None:
    state.init_state()
    state.select_turbine("T01")  # pre-existing turbine selection must be cleared

    state.select_farm("FARM01")

    assert state.get_level() == Level.FARM
    assert state.get_selected_farm_id() == "FARM01"
    assert state.get_selected_turbine_id() is None


def test_select_farm_leaves_layers_and_nwp_cache_and_history_untouched() -> None:
    state.init_state()
    state.set_layer("wind", True)
    state.set_nwp_cached("k", "v")
    state.set_history_window("7d")
    state.set_history_x_metric("gearbox_temp_c")
    state.set_is_mobile(True)

    state.select_farm("FARM01")

    assert state.get_layer("wind") is True
    assert state.get_nwp_cached("k") == "v"
    assert state.get_history_window() == "7d"
    assert state.get_history_x_metric() == "gearbox_temp_c"
    assert state.get_is_mobile() is True


# --------------------------------------------------------------------------------------
# select_turbine
# --------------------------------------------------------------------------------------


def test_select_turbine_sets_level_and_turbine_preserves_farm() -> None:
    state.init_state()
    state.select_farm("FARM01")

    state.select_turbine("T01")

    assert state.get_level() == Level.TURBINE
    assert state.get_selected_turbine_id() == "T01"
    assert state.get_selected_farm_id() == "FARM01"  # not cleared


def test_select_turbine_swaps_a_different_turbine() -> None:
    state.init_state()
    state.select_farm("FARM01")
    state.select_turbine("T01")

    state.select_turbine("T02")

    assert state.get_selected_turbine_id() == "T02"
    assert state.get_selected_farm_id() == "FARM01"


def test_select_turbine_leaves_layers_and_nwp_cache_untouched() -> None:
    state.init_state()
    state.select_farm("FARM01")
    state.set_layer("temperature", True)
    state.set_nwp_cached("k", "v")

    state.select_turbine("T01")

    assert state.get_layer("temperature") is True
    assert state.get_nwp_cached("k") == "v"


# --------------------------------------------------------------------------------------
# reset_view
# --------------------------------------------------------------------------------------


def test_reset_view_returns_to_fleet_and_clears_both_selections() -> None:
    state.init_state()
    state.select_farm("FARM01")
    state.select_turbine("T01")

    state.reset_view()

    assert state.get_level() == Level.FLEET
    assert state.get_selected_farm_id() is None
    assert state.get_selected_turbine_id() is None


def test_reset_view_preserves_layers_and_nwp_cache() -> None:
    """`PROJECT_SPEC.md` §7.2: Reset View must preserve layer checkbox states and `nwp_cache`."""
    state.init_state()
    state.select_farm("FARM01")
    state.select_turbine("T01")
    state.set_layer("wind", True)
    state.set_layer("forecast", True)
    state.set_nwp_cached("point:1,2", {"wind_speed_ms": 7.0})

    state.reset_view()

    assert state.get_layer("wind") is True
    assert state.get_layer("forecast") is True
    assert state.get_nwp_cached("point:1,2") == {"wind_speed_ms": 7.0}


def test_reset_view_preserves_history_window_and_x_metric() -> None:
    state.init_state()
    state.set_history_window("all")
    state.set_history_x_metric("rotor_rpm")

    state.reset_view()

    assert state.get_history_window() == "all"
    assert state.get_history_x_metric() == "rotor_rpm"


# --------------------------------------------------------------------------------------
# Layer, NWP cache, history, and mobile-hint accessors
# --------------------------------------------------------------------------------------


def test_get_layer_defaults_false_for_unknown_name() -> None:
    state.init_state()
    assert state.get_layer("not_a_real_layer") is False


def test_set_layer_round_trips() -> None:
    state.init_state()
    state.set_layer("forecast", True)
    assert state.get_layer("forecast") is True
    state.set_layer("forecast", False)
    assert state.get_layer("forecast") is False


def test_nwp_cache_round_trips_and_missing_key_is_none() -> None:
    state.init_state()
    assert state.get_nwp_cached("missing") is None
    state.set_nwp_cached("k", 42)
    assert state.get_nwp_cached("k") == 42


@pytest.mark.parametrize("window", ["24h", "7d", "all"])
def test_history_window_round_trips(window: str) -> None:
    state.init_state()
    state.set_history_window(window)
    assert state.get_history_window() == window


def test_history_x_metric_round_trips() -> None:
    state.init_state()
    state.set_history_x_metric("blade_pitch_deg")
    assert state.get_history_x_metric() == "blade_pitch_deg"


def test_is_mobile_round_trips() -> None:
    state.init_state()
    state.set_is_mobile(True)
    assert state.get_is_mobile() is True
    state.set_is_mobile(False)
    assert state.get_is_mobile() is False
