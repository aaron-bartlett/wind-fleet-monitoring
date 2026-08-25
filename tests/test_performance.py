"""Performance regression tests for the aggregate builders and the bucketed/scatter queries.

Builds a synthetic **50 turbines / 10 farms / 30 days of 5-minute telemetry (~430k rows)**
DuckDB entirely with set-based SQL inside this module (`IMPLEMENTATION_PLAN.md` Phase 15) —
never touching `data/` or `tests/fixtures/`, per `CLAUDE.md` §4.3. The schema mirrors
`src/data/db.py`'s post-ingest shape (both telemetry indexes, the `latest_telemetry` view)
closely enough that every function under test runs its real production query plan at
fleet scale, exercising `PROJECT_SPEC.md` §12's scalability requirements: fixed-query-count
roll-ups, SQL-side bucketing/down-sampling, and the point caps browsers actually receive.
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

import config
from src.data import queries
from src.domain import aggregates, clock
from src.domain.models import Level

_FARM_COUNT = 10
_TURBINES_PER_FARM = 5
_TURBINE_COUNT = _FARM_COUNT * _TURBINES_PER_FARM
_DAYS = 30
_INTERVALS_PER_TURBINE = _DAYS * 24 * 60 // config.TELEMETRY_INTERVAL_MINUTES  # 8,640
_EXPECTED_TELEMETRY_ROWS = _TURBINE_COUNT * _INTERVALS_PER_TURBINE  # 432,000

# One bucket past the synthetic fleet's last telemetry row (day 30, interval 8,639 = day 30
# 23:55 UTC), matching how the real app's `clock.get_now` resolves to the dataset's own latest
# timestamp (`PROJECT_SPEC.md` §6.1) rather than an arbitrary wall-clock date.
_NOW = datetime(2026, 1, 31, 0, 0, tzinfo=UTC)

_SETTINGS = config.Settings(
    data_dir=Path("unused"), duckdb_path=Path(":memory:"), sim_now=None, stale_after_minutes=15
)


def _build_synthetic_fleet_db() -> duckdb.DuckDBPyConnection:
    """Build the 50-turbine/10-farm/30-day fleet directly in an in-memory DuckDB.

    Every table is a single set-based `CREATE TABLE ... AS SELECT` over `generate_series` —
    never a Python-side row loop — so building ~430k telemetry rows costs a fraction of a
    second, in keeping with the project's own "aggregate in SQL, not pandas" stance.
    """
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE farms AS "
        "SELECT 'FARM' || LPAD(CAST(i AS VARCHAR), 2, '0') AS farm_id, "
        "'Farm ' || i AS farm_name, "
        "40.0 + i * 0.5 AS latitude, -97.0 + i * 0.5 AS longitude "
        f"FROM generate_series(1, {_FARM_COUNT}) AS t(i)"
    )
    con.execute(
        "CREATE TABLE turbines AS "
        "SELECT 'TURB' || LPAD(CAST(k AS VARCHAR), 3, '0') AS turbine_id, "
        "'FARM' || LPAD(CAST(CAST(FLOOR((k - 1) / "
        f"{_TURBINES_PER_FARM}.0) AS INTEGER) + 1 AS VARCHAR), 2, '0') AS farm_id, "
        f"40.0 + (CAST(FLOOR((k - 1) / {_TURBINES_PER_FARM}.0) AS INTEGER) + 1) * 0.5 "
        f"+ (k % {_TURBINES_PER_FARM}) * 0.01 AS latitude, "
        f"-97.0 + (CAST(FLOOR((k - 1) / {_TURBINES_PER_FARM}.0) AS INTEGER) + 1) * 0.5 "
        f"+ (k % {_TURBINES_PER_FARM}) * 0.01 AS longitude "
        f"FROM generate_series(1, {_TURBINE_COUNT}) AS t(k)"
    )
    con.execute(
        "CREATE TABLE telemetry AS "
        "SELECT t.turbine_id, t.farm_id, "
        "CAST('2026-01-01 00:00:00+00' AS TIMESTAMPTZ) + (g.step * INTERVAL '5 minutes') "
        "AS timestamp, "
        "CAST('2026-01-01 00:00:00+00' AS TIMESTAMPTZ) + (g.step * INTERVAL '5 minutes') "
        "+ INTERVAL '2 minutes' AS received_at, "
        "3000.0 + 200 * SIN(g.step * 0.01) AS power_output_kw, "
        "9.0 + 2 * SIN(g.step * 0.02) AS wind_speed_ms, "
        "14.0 + SIN(g.step * 0.03) AS rotor_rpm, "
        "4.0 AS blade_pitch_deg, "
        "70.0 + 5 * SIN(g.step * 0.05) AS gearbox_temp_c "
        "FROM turbines t "
        f"CROSS JOIN generate_series(0, {_INTERVALS_PER_TURBINE - 1}) AS g(step)"
    )
    con.execute("CREATE INDEX idx_tel_turbine_ts ON telemetry(turbine_id, timestamp)")
    con.execute("CREATE INDEX idx_tel_farm_ts ON telemetry(farm_id, timestamp)")
    con.execute(
        "CREATE OR REPLACE VIEW latest_telemetry AS "
        "SELECT * EXCLUDE (rn) FROM ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY turbine_id ORDER BY timestamp DESC) AS rn"
        "  FROM telemetry"
        ") WHERE rn = 1"
    )
    return con


@pytest.fixture(scope="module")
def perf_con() -> Iterator[duckdb.DuckDBPyConnection]:
    """A module-scoped, fleet-scale synthetic connection — built once for the whole suite."""
    con = _build_synthetic_fleet_db()
    yield con
    con.close()


def test_synthetic_fleet_has_expected_scale(perf_con: duckdb.DuckDBPyConnection) -> None:
    row = perf_con.execute("SELECT COUNT(*) FROM telemetry").fetchone()
    assert row is not None
    assert row[0] == _EXPECTED_TELEMETRY_ROWS == 432_000


def test_build_farm_map_rows_completes_in_under_two_seconds(
    perf_con: duckdb.DuckDBPyConnection,
) -> None:
    started = time.monotonic()
    rows = aggregates.build_farm_map_rows(perf_con, _SETTINGS, _NOW)
    elapsed = time.monotonic() - started

    assert len(rows) == _FARM_COUNT
    assert elapsed < 2.0


def test_fleet_timeseries_all_bucket_stays_within_max_timeseries_points(
    perf_con: duckdb.DuckDBPyConnection,
) -> None:
    df = queries.get_power_timeseries(
        perf_con,
        level=Level.FLEET,
        entity_id=None,
        start=None,
        end=_NOW,
        bucket=config.BUCKET_BY_WINDOW["all"],
        max_points=config.MAX_TIMESERIES_POINTS,
    )
    assert len(df) <= config.MAX_TIMESERIES_POINTS


def test_scatter_data_downsamples_to_max_points(perf_con: duckdb.DuckDBPyConnection) -> None:
    df = queries.get_scatter_data(
        perf_con,
        turbine_id="TURB001",
        x_metric="wind_speed_ms",
        y_metric="power_output_kw",
        start=None,
        end=_NOW,
        max_points=config.MAX_SCATTER_POINTS,
    )
    assert len(df) <= config.MAX_SCATTER_POINTS


@pytest.mark.parametrize(
    ("level", "entity_id", "window_key"),
    [
        (Level.FLEET, None, "24h"),
        (Level.FLEET, None, "7d"),
        (Level.FLEET, None, "all"),
        (Level.FARM, "FARM01", "24h"),
        (Level.FARM, "FARM01", "7d"),
        (Level.TURBINE, "TURB001", "all"),
    ],
)
def test_no_timeseries_query_exceeds_the_scatter_point_cap(
    perf_con: duckdb.DuckDBPyConnection, level: Level, entity_id: str | None, window_key: str
) -> None:
    """`PROJECT_SPEC.md` §12: no query function returns more than `MAX_SCATTER_POINTS` rows to
    the browser, across every level/window combination the dashboards actually drive."""
    start = clock.window_start(_NOW, window_key)
    df = queries.get_power_timeseries(
        perf_con,
        level=level,
        entity_id=entity_id,
        start=start,
        end=_NOW,
        bucket=config.BUCKET_BY_WINDOW[window_key],
        max_points=config.MAX_TIMESERIES_POINTS,
    )
    assert len(df) <= config.MAX_SCATTER_POINTS
