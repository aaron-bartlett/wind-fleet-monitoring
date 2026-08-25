"""Smoke tests: the app builds a Folium map and parses map clicks without a Streamlit runtime.

`PROJECT_SPEC.md` §13 requires a smoke check that the app "builds a Folium map object ... for
each of the three levels without raising"; `IMPLEMENTATION_PLAN.md` Phase 11 covers the fleet
level, Phase 12 adds the farm/turbine layer, and Phase 13 adds the Turbine Dashboard's two pure
data-prep helpers (`_scatter_y_metric`, `_breach_severity_by_metric`) — no `st.*` calls, so
they're callable without a Streamlit runtime, unlike `dashboards.turbine.render` itself.
"""

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import folium
import pytest

import config
from config import Settings
from src.domain import aggregates, geo
from src.domain.models import HealthStatus, Level
from src.ui import map_view
from src.ui.dashboards import turbine as turbine_dashboard

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def settings(fixtures_dir: Path) -> Settings:
    return Settings(
        data_dir=fixtures_dir,
        duckdb_path=Path(":memory:"),
        sim_now=_NOW,
        stale_after_minutes=15,
    )


def _build_fleet_map(con: duckdb.DuckDBPyConnection, settings: Settings) -> folium.Map:
    farm_rows = aggregates.build_farm_map_rows(con, settings, _NOW)
    bounds = geo.fleet_bounds(con)
    assert bounds is not None
    padding = ((config.DESKTOP_PANEL_PX, 0), (0, 0))
    return map_view.build_map(farm_rows, None, bounds, Level.FLEET, None, None, padding, {})


def test_build_map_returns_a_folium_map(
    db_con: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    fleet_map = _build_fleet_map(db_con, settings)
    assert isinstance(fleet_map, folium.Map)


def test_build_map_html_contains_every_farm_id(
    db_con: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    fleet_map = _build_fleet_map(db_con, settings)
    html = fleet_map.get_root().render()
    for farm_id in ("FARM01", "FARM02", "FARM03"):
        assert farm_id in html


def test_farm_with_no_health_score_renders_error_color(
    db_con: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    farm_rows = aggregates.build_farm_map_rows(db_con, settings, _NOW)
    farm03 = next(row for row in farm_rows if row.farm.farm_id == "FARM03")
    assert farm03.health_score is None  # FARM03 has zero turbines in the fixture set

    fleet_map = _build_fleet_map(db_con, settings)
    html = fleet_map.get_root().render()
    assert config.HEALTH_COLORS["Error"] in html


def _build_farm_map(con: duckdb.DuckDBPyConnection, settings: Settings, farm_id: str) -> folium.Map:
    farm_rows = aggregates.build_farm_map_rows(con, settings, _NOW)
    turbine_rows = aggregates.build_turbine_map_rows(con, settings, _NOW, farm_id)
    farm_row = next(row for row in farm_rows if row.farm.farm_id == farm_id)
    bounds = geo.farm_view_bounds(con, farm_id, farm_row.farm)
    padding = ((config.DESKTOP_PANEL_PX, 0), (0, 0))
    return map_view.build_map(
        farm_rows, turbine_rows, bounds, Level.FARM, farm_id, None, padding, {}
    )


def test_build_map_farm_level_returns_a_folium_map(
    db_con: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    farm_map = _build_farm_map(db_con, settings, "FARM01")
    assert isinstance(farm_map, folium.Map)


def test_build_map_farm_level_html_contains_every_turbine_id(
    db_con: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    farm_map = _build_farm_map(db_con, settings, "FARM01")
    html = farm_map.get_root().render()
    assert "TURB001" in html
    assert "TURB002" in html


def test_build_map_farm_level_with_zero_turbines_does_not_raise(
    db_con: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    farm_map = _build_farm_map(db_con, settings, "FARM03")
    assert isinstance(farm_map, folium.Map)


def test_turbine_with_no_telemetry_renders_error_color(
    db_con: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    turbine_rows = aggregates.build_turbine_map_rows(db_con, settings, _NOW, "FARM02")
    turb999_result = next(
        result for turbine, result in turbine_rows if turbine.turbine_id == "TURB999"
    )
    assert turb999_result.status.value == "Error"

    farm_map = _build_farm_map(db_con, settings, "FARM02")
    html = farm_map.get_root().render()
    assert config.HEALTH_COLORS["Error"] in html


class TestExtractClickedId:
    def test_none_input_returns_none(self) -> None:
        assert map_view.extract_clicked_id(None) is None

    def test_empty_dict_returns_none(self) -> None:
        assert map_view.extract_clicked_id({}) is None

    def test_all_none_values_returns_none(self) -> None:
        malformed = {"last_object_clicked_popup": None, "last_object_clicked_tooltip": None}
        assert map_view.extract_clicked_id(malformed) is None

    def test_parses_a_farm_popup(self) -> None:
        map_return = {"last_object_clicked_popup": "__farm__FARM01"}
        assert map_view.extract_clicked_id(map_return) == ("farm", "FARM01")

    def test_falls_back_to_tooltip_when_popup_unparseable(self) -> None:
        map_return = {
            "last_object_clicked_popup": None,
            "last_object_clicked_tooltip": "__farm__FARM02",
        }
        assert map_view.extract_clicked_id(map_return) == ("farm", "FARM02")

    def test_parses_a_turbine_popup(self) -> None:
        map_return = {"last_object_clicked_popup": "__turbine__TURB001"}
        assert map_view.extract_clicked_id(map_return) == ("turbine", "TURB001")


# --------------------------------------------------------------------------------------
# Turbine Dashboard data-prep helpers (render-free — see module docstring)
# --------------------------------------------------------------------------------------


class TestScatterYMetric:
    def test_swaps_to_wind_speed_when_x_is_power_output(self) -> None:
        assert turbine_dashboard._scatter_y_metric("power_output_kw") == "wind_speed_ms"

    def test_stays_power_output_for_wind_speed_x(self) -> None:
        assert turbine_dashboard._scatter_y_metric("wind_speed_ms") == "power_output_kw"

    def test_stays_power_output_for_any_other_metric_x(self) -> None:
        assert turbine_dashboard._scatter_y_metric("gearbox_temp_c") == "power_output_kw"


def test_breach_severity_by_metric_handles_turb999_no_telemetry_without_raising(
    db_con: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    summary = aggregates.build_turbine_summary(db_con, settings, _NOW, "TURB999")
    assert summary.record is None
    assert summary.health.status is HealthStatus.ERROR

    severities = turbine_dashboard._breach_severity_by_metric(summary.health)
    assert severities == {}  # ERROR short-circuits before breach collection (health.py)
