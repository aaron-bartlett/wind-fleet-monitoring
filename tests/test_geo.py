"""Tests for `src/domain/geo.py`: local time and bounds helpers.

Uses only `tests/fixtures/` (never the real `data/` CSVs), per `CLAUDE.md` §4.3.
"""

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

import config
from src.data import queries
from src.domain import geo
from src.domain.models import Bounds, Farm

_UTC_NOON = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

# --------------------------------------------------------------------------------------
# local_time
# --------------------------------------------------------------------------------------


def test_local_time_farm01_is_america_chicago() -> None:
    # FARM01 (tests/fixtures/farms.csv): 41.25, -96.53 — squarely in America/Chicago.
    local_dt, tz_label = geo.local_time(_UTC_NOON, latitude=41.25, longitude=-96.53)

    offset = local_dt.utcoffset()
    assert offset is not None
    # CST (UTC-6) or CDT (UTC-5) depending on DST; either way, several hours behind UTC.
    assert timedelta(hours=-6) <= offset <= timedelta(hours=-5)
    assert tz_label != ""


def test_local_time_ocean_coordinate_does_not_raise() -> None:
    # (0, 0) has no land timezone; whatever timezonefinder resolves it to must not raise and
    # must be UTC-equivalent (zero offset).
    local_dt, tz_label = geo.local_time(_UTC_NOON, latitude=0.0, longitude=0.0)
    assert local_dt.utcoffset() == timedelta(0)
    assert tz_label != ""


class _NullTimezoneFinder:
    """Stand-in for `TimezoneFinder` that always fails to resolve a coordinate."""

    def timezone_at(self, **_kwargs: float) -> str | None:
        return None


def test_local_time_falls_back_to_utc_when_lookup_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `TimezoneFinder` instances don't allow attribute patching (Cython extension type), so
    # the singleton itself is swapped for a fake with the same interface.
    monkeypatch.setattr(geo, "_TIMEZONE_FINDER", _NullTimezoneFinder())
    local_dt, tz_label = geo.local_time(_UTC_NOON, latitude=12.3, longitude=45.6)
    assert local_dt == _UTC_NOON
    assert tz_label == "UTC"


# --------------------------------------------------------------------------------------
# fleet_bounds
# --------------------------------------------------------------------------------------


def test_fleet_bounds_is_expanded(db_con: duckdb.DuckDBPyConnection) -> None:
    raw = queries.get_fleet_bounds(db_con)
    assert raw is not None
    expanded = geo.fleet_bounds(db_con)
    assert expanded == raw.expanded(config.BOUNDS_EXPANSION)
    # A genuinely wider box, not a no-op.
    assert expanded.lat_max - expanded.lat_min > raw.lat_max - raw.lat_min


def test_fleet_bounds_none_when_no_farms(
    monkeypatch: pytest.MonkeyPatch, db_con: duckdb.DuckDBPyConnection
) -> None:
    monkeypatch.setattr(queries, "get_fleet_bounds", lambda _con: None)
    assert geo.fleet_bounds(db_con) is None


# --------------------------------------------------------------------------------------
# farm_view_bounds
# --------------------------------------------------------------------------------------


def test_farm_view_bounds_zero_turbines_falls_back_to_point_box(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    # FARM03 (tests/fixtures/turbines.csv) has no turbines at all.
    farm = Farm(farm_id="FARM03", farm_name="Red Canyon", latitude=35.12, longitude=-106.55)
    bounds = geo.farm_view_bounds(db_con, "FARM03", farm)

    assert bounds.lat_min == pytest.approx(35.12 - config.ZERO_SPAN_PAD_DEG)
    assert bounds.lat_max == pytest.approx(35.12 + config.ZERO_SPAN_PAD_DEG)
    assert bounds.lon_min == pytest.approx(-106.55 - config.ZERO_SPAN_PAD_DEG)
    assert bounds.lon_max == pytest.approx(-106.55 + config.ZERO_SPAN_PAD_DEG)
    assert bounds.lat_max > bounds.lat_min  # non-degenerate
    assert bounds.lon_max > bounds.lon_min


def test_farm_view_bounds_single_turbine_falls_back_to_point_box(
    monkeypatch: pytest.MonkeyPatch, db_con: duckdb.DuckDBPyConnection
) -> None:
    single_point = Bounds(lat_min=10.0, lat_max=10.0, lon_min=20.0, lon_max=20.0)
    monkeypatch.setattr(queries, "get_farm_turbine_bounds", lambda _con, _farm_id: single_point)
    farm = Farm(farm_id="FARMX", farm_name="Solo", latitude=10.0, longitude=20.0)

    bounds = geo.farm_view_bounds(db_con, "FARMX", farm)

    assert bounds.lat_max > bounds.lat_min
    assert bounds.lon_max > bounds.lon_min
    assert bounds.lat_min == pytest.approx(10.0 - config.ZERO_SPAN_PAD_DEG)
    assert bounds.lon_min == pytest.approx(20.0 - config.ZERO_SPAN_PAD_DEG)


def test_farm_view_bounds_multiple_turbines_is_expanded(db_con: duckdb.DuckDBPyConnection) -> None:
    # FARM01 has two turbines at distinct coordinates (tests/fixtures/turbines.csv).
    raw = queries.get_farm_turbine_bounds(db_con, "FARM01")
    assert raw is not None
    farm = Farm(farm_id="FARM01", farm_name="Prairie Ridge", latitude=41.25, longitude=-96.53)

    bounds = geo.farm_view_bounds(db_con, "FARM01", farm)

    assert bounds == raw.expanded(config.BOUNDS_EXPANSION)
