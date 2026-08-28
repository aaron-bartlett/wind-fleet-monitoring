"""Every read query the application runs against DuckDB (`CLAUDE.md` §4.3, §5.4).

This module is the sole owner of read SQL in the project; `src/data/db.py` owns the DDL and
ingest pipeline. No other module ever writes a SQL string — domain and UI code call the named,
typed functions below and never see SQL.

Return-value convention, so callers never have to guess:

- **Raises `QueryError`** when the request itself is invalid (an unrecognized `bucket`/
  `x_metric`/`y_metric`, a farm/turbine-scoped call missing its `entity_id`, a timeseries result
  wider than `max_points`) or DuckDB itself fails. This is always a caller bug or a query bug,
  never a fact about the fleet.
- **Returns `None`** when the query is well-formed but has no defined answer: no telemetry at all
  (`get_max_timestamp`), an unknown or telemetry-less turbine (`get_latest_record_for_turbine`),
  no farms (`get_fleet_bounds`), or a farm with no turbines (`get_farm_turbine_bounds`). This is
  real data the caller must already handle per `PROJECT_SPEC.md` §11 — not an error.
- **Returns `[]` / `0.0`** for a well-formed query that matched zero rows (`get_farms`,
  `get_turbines`, `get_latest_records`, `get_total_energy_mwh`, `get_current_power_kw`).
  `CLAUDE.md` §5.3 bans sentinel returns (`-1`, `""`); an empty list or a genuine zero sum is the
  correct value, not a sentinel standing in for failure.

# SPEC-GAP: `CLAUDE.md` §4.1 lists this module's allowed imports as stdlib/duckdb/pandas/
# config/errors only, but every function below is contractually typed against `Farm`/`Turbine`/
# `TelemetryRecord`/`Bounds`/`Level` (`IMPLEMENTATION_PLAN.md` Phase 4 data contracts), which live
# in `src/domain/models.py`. That module holds only frozen dataclasses/enums — no logic, no I/O,
# no dependency back on `src/data/` — so it is imported here as shared vocabulary, the same way
# `config.py`/`src/errors.py` are, rather than as a business-logic dependency. This keeps the
# substantive intent of the layering rule (no rendering libraries, no business rules in the data
# layer) while satisfying the Phase 4 contract as written.
"""

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import duckdb
import pandas as pd

import config
from src.domain.models import Bounds, Farm, Level, TelemetryRecord, Turbine
from src.errors import QueryError

Params = Sequence[object]

# --------------------------------------------------------------------------------------
# Execution helpers — the one place `duckdb.Error` is ever caught (`CLAUDE.md` §5.3).
# --------------------------------------------------------------------------------------


def _fetchall(
    con: duckdb.DuckDBPyConnection, sql: str, params: Params, context: str
) -> list[tuple[Any, ...]]:
    try:
        return con.execute(sql, params).fetchall()
    except duckdb.Error as exc:
        raise QueryError(f"{context}: {exc}") from exc


def _fetchone(
    con: duckdb.DuckDBPyConnection, sql: str, params: Params, context: str
) -> tuple[Any, ...] | None:
    try:
        return con.execute(sql, params).fetchone()
    except duckdb.Error as exc:
        raise QueryError(f"{context}: {exc}") from exc


def _fetchdf(
    con: duckdb.DuckDBPyConnection, sql: str, params: Params, context: str
) -> pd.DataFrame:
    try:
        return con.execute(sql, params).df()
    except duckdb.Error as exc:
        raise QueryError(f"{context}: {exc}") from exc


# --------------------------------------------------------------------------------------
# Allowlist validation — why `bucket`/`x_metric`/`y_metric` cannot be `?`-bound.
#
# `?` binds a *value* into a literal position (a comparison operand, an INSERT value). It
# cannot bind a *column identifier* or the unit-string inside an `INTERVAL '...'` literal,
# because those are parts of the query's grammar, not runtime values: DuckDB parses
# `SELECT {x_metric} AS x FROM ...` and `INTERVAL '{bucket}'` before any parameter is ever
# substituted. `x_metric`/`y_metric` select *which column* to project — an identifier
# position; `bucket` is spliced into an `INTERVAL` literal used both by `time_bucket` and by
# `generate_series`'s step — also a non-bindable position. The only safe pattern for a value
# that must occupy such a position is to validate it against a closed allowlist first, then
# interpolate the now-known-safe string — never accept the raw caller value. This is why these
# three parameters are the sole exception to "always bind parameters" (`CLAUDE.md` §5.4):
# every other value below (farm_id, turbine_id, timestamps, stride, max_points) is bound.
# --------------------------------------------------------------------------------------

_VALID_BUCKETS = set(config.BUCKET_BY_WINDOW.values())


def _validate_bucket(bucket: str) -> None:
    if bucket not in _VALID_BUCKETS:
        raise QueryError(f"Invalid bucket {bucket!r}; must be one of {sorted(_VALID_BUCKETS)}")


def _validate_metric(metric: str) -> None:
    if metric not in config.METRICS:
        raise QueryError(f"Invalid metric {metric!r}; must be one of {config.METRICS}")


def _level_filter(level: Level, entity_id: str | None) -> tuple[str, list[str]]:
    """Return a `WHERE`-clause fragment and its bind params for the given drill-down level.

    `level` is a closed `StrEnum` matched exhaustively, so this dispatch is safe from
    injection by construction — it never builds SQL from caller-supplied text.

    Raises:
        QueryError: `entity_id` is `None` for `Level.FARM` or `Level.TURBINE`.
    """
    match level:
        case Level.FLEET:
            return "TRUE", []
        case Level.FARM:
            if entity_id is None:
                raise QueryError("entity_id (farm_id) is required when level=Level.FARM")
            return "farm_id = ?", [entity_id]
        case Level.TURBINE:
            if entity_id is None:
                raise QueryError("entity_id (turbine_id) is required when level=Level.TURBINE")
            return "turbine_id = ?", [entity_id]


# --------------------------------------------------------------------------------------
# Row -> dataclass helpers
# --------------------------------------------------------------------------------------


def _row_to_farm(row: tuple[Any, ...]) -> Farm:
    return Farm(farm_id=row[0], farm_name=row[1], latitude=row[2], longitude=row[3])


def _row_to_turbine(row: tuple[Any, ...]) -> Turbine:
    return Turbine(turbine_id=row[0], farm_id=row[1], latitude=row[2], longitude=row[3])


def _row_to_telemetry_record(row: tuple[Any, ...]) -> TelemetryRecord:
    return TelemetryRecord(
        turbine_id=row[0],
        farm_id=row[1],
        timestamp=row[2],
        received_at=row[3],
        power_output_kw=row[4],
        wind_speed_ms=row[5],
        rotor_rpm=row[6],
        blade_pitch_deg=row[7],
        gearbox_temp_c=row[8],
    )


_TELEMETRY_RECORD_COLUMNS = (
    "turbine_id, farm_id, timestamp, received_at, "
    "power_output_kw, wind_speed_ms, rotor_rpm, blade_pitch_deg, gearbox_temp_c"
)


# --------------------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------------------


def get_farms(con: duckdb.DuckDBPyConnection) -> list[Farm]:
    """Return every farm, ordered by `farm_id`."""
    rows = _fetchall(
        con,
        "SELECT farm_id, farm_name, latitude, longitude FROM farms ORDER BY farm_id",
        [],
        "get_farms",
    )
    return [_row_to_farm(row) for row in rows]


def get_turbines(con: duckdb.DuckDBPyConnection, farm_id: str | None = None) -> list[Turbine]:
    """Return turbines, optionally restricted to one farm, ordered by `turbine_id`.

    Args:
        con: Open DuckDB connection.
        farm_id: When given, only turbines belonging to this farm are returned.

    Returns:
        A list of `Turbine`; empty (not `None`) when no turbines match.
    """
    if farm_id is None:
        rows = _fetchall(
            con,
            "SELECT turbine_id, farm_id, latitude, longitude FROM turbines ORDER BY turbine_id",
            [],
            "get_turbines",
        )
    else:
        rows = _fetchall(
            con,
            "SELECT turbine_id, farm_id, latitude, longitude FROM turbines "
            "WHERE farm_id = ? ORDER BY turbine_id",
            [farm_id],
            "get_turbines",
        )
    return [_row_to_turbine(row) for row in rows]


def get_turbine_counts_by_farm(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Return `{farm_id: turbine_count}` for every farm, including farms with zero turbines.

    A single `LEFT JOIN ... GROUP BY` — never a query per farm (`PROJECT_SPEC.md` §12).
    """
    rows = _fetchall(
        con,
        "SELECT farms.farm_id, COUNT(turbines.turbine_id) "
        "FROM farms LEFT JOIN turbines ON turbines.farm_id = farms.farm_id "
        "GROUP BY farms.farm_id",
        [],
        "get_turbine_counts_by_farm",
    )
    return {str(row[0]): int(row[1]) for row in rows}


# --------------------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------------------


def get_max_timestamp(con: duckdb.DuckDBPyConnection) -> datetime | None:
    """Return the latest telemetry `timestamp` across the whole fleet, or `None` if empty."""
    row = _fetchone(con, "SELECT MAX(timestamp) FROM telemetry", [], "get_max_timestamp")
    if row is None or row[0] is None:
        return None
    value = row[0]
    assert isinstance(value, datetime)
    return value


def get_latest_records(
    con: duckdb.DuckDBPyConnection, farm_id: str | None = None
) -> list[TelemetryRecord]:
    """Return each turbine's latest telemetry record, optionally restricted to one farm."""
    if farm_id is None:
        rows = _fetchall(
            con,
            f"SELECT {_TELEMETRY_RECORD_COLUMNS} FROM latest_telemetry ORDER BY turbine_id",
            [],
            "get_latest_records",
        )
    else:
        rows = _fetchall(
            con,
            f"SELECT {_TELEMETRY_RECORD_COLUMNS} FROM latest_telemetry "
            "WHERE farm_id = ? ORDER BY turbine_id",
            [farm_id],
            "get_latest_records",
        )
    return [_row_to_telemetry_record(row) for row in rows]


def get_latest_record_for_turbine(
    con: duckdb.DuckDBPyConnection, turbine_id: str
) -> TelemetryRecord | None:
    """Return one turbine's latest telemetry record.

    Returns:
        `None` when the turbine has no telemetry at all — a legitimate outcome
        `health.classify` handles, not an error.
    """
    row = _fetchone(
        con,
        f"SELECT {_TELEMETRY_RECORD_COLUMNS} FROM latest_telemetry WHERE turbine_id = ?",
        [turbine_id],
        "get_latest_record_for_turbine",
    )
    return None if row is None else _row_to_telemetry_record(row)


def get_power_timeseries(
    con: duckdb.DuckDBPyConnection,
    *,
    level: Level,
    entity_id: str | None,
    start: datetime | None,
    end: datetime,
    bucket: str,
    max_points: int,
) -> pd.DataFrame:
    """Return bucketed fleet/farm/turbine power over `[start, end]`, with genuine gap rows.

    A generated time spine (`generate_series` at `bucket` resolution) is `LEFT JOIN`ed onto
    the per-bucket `SUM(power_output_kw)`, so a missing interval appears as a row with
    `power_kw = NaN` rather than being silently skipped (`PROJECT_SPEC.md` §11 — a genuine
    gap, not an interpolated line).

    Args:
        con: Open DuckDB connection.
        level: Aggregation scope.
        entity_id: `farm_id`/`turbine_id` for `Level.FARM`/`Level.TURBINE`; ignored for
            `Level.FLEET`.
        start: Window start; `None` resolves to the entity's earliest telemetry timestamp
            (or `end`, as a single-bucket fallback, if it has none at all).
        end: Window end (inclusive).
        bucket: One of `config.BUCKET_BY_WINDOW`'s values, e.g. `"5 minutes"`.
        max_points: Upper bound on returned rows.

    Returns:
        A DataFrame with columns `bucket_start` (tz-aware UTC) and `power_kw` (float, `NaN`
        for a missing interval).

    Raises:
        QueryError: `bucket` is not a recognized value, `entity_id` is missing for a
            farm/turbine-scoped call, or the result would exceed `max_points` — the caller
            must choose a coarser bucket rather than have the result silently truncated.
    """
    _validate_bucket(bucket)
    filter_sql, filter_params = _level_filter(level, entity_id)

    resolved_start = (
        start if start is not None else _resolve_min_timestamp(con, filter_sql, filter_params, end)
    )

    sql = (
        "WITH spine AS ("
        "SELECT bucket_start FROM generate_series("
        f"time_bucket(INTERVAL '{bucket}', ?::TIMESTAMPTZ), "
        f"time_bucket(INTERVAL '{bucket}', ?::TIMESTAMPTZ), "
        f"INTERVAL '{bucket}'"
        ") AS t(bucket_start)"
        "), agg AS ("
        f"SELECT time_bucket(INTERVAL '{bucket}', timestamp) AS bucket_start, "
        "SUM(power_output_kw) AS power_kw "
        "FROM telemetry "
        f"WHERE {filter_sql} AND timestamp >= ? AND timestamp <= ? "
        "GROUP BY 1"
        ") "
        "SELECT spine.bucket_start, agg.power_kw "
        "FROM spine LEFT JOIN agg USING (bucket_start) "
        "ORDER BY spine.bucket_start"
    )
    params: list[object] = [resolved_start, end, *filter_params, resolved_start, end]
    df = _fetchdf(con, sql, params, "get_power_timeseries")
    if len(df) > max_points:
        raise QueryError(
            f"get_power_timeseries: result has {len(df)} points, exceeding max_points="
            f"{max_points}; choose a coarser bucket."
        )
    return df


def _resolve_min_timestamp(
    con: duckdb.DuckDBPyConnection,
    filter_sql: str,
    filter_params: Params,
    fallback: datetime,
) -> datetime:
    """Return the earliest telemetry `timestamp` matching `filter_sql`, or `fallback` if none."""
    row = _fetchone(
        con,
        f"SELECT MIN(timestamp) FROM telemetry WHERE {filter_sql}",
        filter_params,
        "get_power_timeseries",
    )
    if row is None or row[0] is None:
        return fallback
    value = row[0]
    assert isinstance(value, datetime)
    return value


def get_farm_wind_speed_series(
    con: duckdb.DuckDBPyConnection,
    *,
    farm_id: str,
    start: datetime,
    end: datetime,
    max_points: int,
) -> pd.DataFrame:
    """Return hourly mean measured wind speed for one farm over ``[start, end]``.

    Averages ``telemetry.wind_speed_ms`` across the farm's turbines into 1-hour buckets;
    ``NULL`` speeds are excluded and empty hours are simply absent (the wind rose bins by
    whatever hours have data, so no gap spine is needed). The ``INTERVAL '1 hour'`` literal is
    a fixed constant, not caller input, so it needs no allowlist check.

    Args:
        con: Open DuckDB connection.
        farm_id: The farm to aggregate.
        start: Window start (inclusive).
        end: Window end (inclusive).
        max_points: Upper bound on returned rows.

    Returns:
        A DataFrame with columns ``bucket_start`` (tz-aware UTC) and ``wind_speed_ms``
        (float), ascending by ``bucket_start``. Empty when the farm has no non-null wind
        speed in the window.

    Raises:
        QueryError: the result would exceed ``max_points`` — the caller must narrow the
            window rather than have the rose silently truncated.
    """
    sql = (
        "SELECT time_bucket(INTERVAL '1 hour', timestamp) AS bucket_start, "
        "avg(wind_speed_ms) AS wind_speed_ms "
        "FROM telemetry "
        "WHERE farm_id = ? AND timestamp >= ? AND timestamp <= ? AND wind_speed_ms IS NOT NULL "
        "GROUP BY 1 ORDER BY 1"
    )
    df = _fetchdf(con, sql, [farm_id, start, end], "get_farm_wind_speed_series")
    if len(df) > max_points:
        raise QueryError(
            f"get_farm_wind_speed_series: result has {len(df)} points, exceeding "
            f"max_points={max_points}; narrow the window."
        )
    return df


def get_total_energy_mwh(
    con: duckdb.DuckDBPyConnection, *, level: Level, entity_id: str | None
) -> float:
    """Return total energy (MWh) over the full dataset for the given scope.

    `PROJECT_SPEC.md` §16: rendered as "Total Energy (MWh)", never "Total Power Output" —
    power and energy are not the same unit. `0.0` (not `None`) when no rows match; this is
    the true sum, not a missing-data sentinel.
    """
    filter_sql, filter_params = _level_filter(level, entity_id)
    sql = (
        "SELECT COALESCE(SUM(power_output_kw), 0) * (? / 60.0) / 1000.0 "
        f"FROM telemetry WHERE {filter_sql}"
    )
    params: list[object] = [config.TELEMETRY_INTERVAL_MINUTES, *filter_params]
    row = _fetchone(con, sql, params, "get_total_energy_mwh")
    assert row is not None
    return float(row[0])


def get_current_power_kw(
    con: duckdb.DuckDBPyConnection,
    *,
    level: Level,
    entity_id: str | None,
    now: datetime,
    stale_after_minutes: int,
) -> float:
    """Return current summed power (kW) for the given scope, excluding stale readings.

    A turbine whose latest record is older than `now - stale_after_minutes`, or whose
    `power_output_kw` is `NULL`, does not contribute to the sum.
    """
    filter_sql, filter_params = _level_filter(level, entity_id)
    cutoff = now - timedelta(minutes=stale_after_minutes)
    sql = (
        "SELECT COALESCE(SUM(power_output_kw), 0) FROM latest_telemetry "
        f"WHERE {filter_sql} AND power_output_kw IS NOT NULL AND timestamp >= ?"
    )
    params: list[object] = [*filter_params, cutoff]
    row = _fetchone(con, sql, params, "get_current_power_kw")
    assert row is not None
    return float(row[0])


def _scatter_where(
    *, turbine_id: str, x_metric: str, y_metric: str, start: datetime | None, end: datetime
) -> tuple[str, list[object]]:
    """Build the shared `WHERE` clause + bind params for `get_scatter_data`/`get_scatter_sample_size`.

    Raises:
        QueryError: `x_metric` or `y_metric` is not a recognized metric name.
    """
    _validate_metric(x_metric)
    _validate_metric(y_metric)

    start_clause = "timestamp >= ? AND " if start is not None else ""
    params: list[object] = [turbine_id]
    if start is not None:
        params.append(start)
    params.append(end)

    where_clause = (
        f"turbine_id = ? AND {x_metric} IS NOT NULL AND {y_metric} IS NOT NULL "
        f"AND {start_clause}timestamp <= ?"
    )
    return where_clause, params


def get_scatter_data(
    con: duckdb.DuckDBPyConnection,
    *,
    turbine_id: str,
    x_metric: str,
    y_metric: str,
    start: datetime | None,
    end: datetime,
    max_points: int,
) -> pd.DataFrame:
    """Return `(x, y)` telemetry pairs for one turbine, down-sampled to `max_points`.

    Down-sampling is a deterministic modulo stride over a `ROW_NUMBER()` — never
    `ORDER BY random()` — so the same inputs always return the same rows
    (`PROJECT_SPEC.md` §12: "deterministic reservoir sampling").

    Args:
        con: Open DuckDB connection.
        turbine_id: The turbine to read.
        x_metric: One of `config.METRICS`.
        y_metric: One of `config.METRICS`.
        start: Window start; `None` means no lower bound.
        end: Window end (inclusive).
        max_points: Upper bound on returned rows.

    Returns:
        A DataFrame with columns `x` and `y` (both float), length at most `max_points`.

    Raises:
        QueryError: `x_metric` or `y_metric` is not a recognized metric name.
    """
    where_clause, filter_params = _scatter_where(
        turbine_id=turbine_id, x_metric=x_metric, y_metric=y_metric, start=start, end=end
    )

    total = _scalar_count(con, where_clause, filter_params)
    stride = max(1, math.ceil(total / max_points)) if total else 1

    sql = (
        "WITH ranked AS ("
        f"SELECT {x_metric} AS x, {y_metric} AS y, "
        "ROW_NUMBER() OVER (ORDER BY timestamp) - 1 AS rn "
        f"FROM telemetry WHERE {where_clause}"
        ") SELECT x, y FROM ranked WHERE rn % CAST(? AS BIGINT) = 0 ORDER BY rn"
    )
    return _fetchdf(con, sql, [*filter_params, stride], "get_scatter_data")


def get_scatter_sample_size(
    con: duckdb.DuckDBPyConnection,
    *,
    turbine_id: str,
    x_metric: str,
    y_metric: str,
    start: datetime | None,
    end: datetime,
) -> int:
    """Return the pre-sample row count `get_scatter_data` would down-sample from.

    Lets a caller (the Turbine Dashboard's historical scatter, `PROJECT_SPEC.md` §10.4) pass an
    honest `sampled_from` to `charts.build_scatter_with_regression` — never silently truncating.
    Shares `get_scatter_data`'s exact filter (same metrics, window, and NULL exclusion) so the
    two counts are always comparable.

    Args:
        con: Open DuckDB connection.
        turbine_id: The turbine to read.
        x_metric: One of `config.METRICS`.
        y_metric: One of `config.METRICS`.
        start: Window start; `None` means no lower bound.
        end: Window end (inclusive).

    Returns:
        The number of rows matching the filter, before any down-sampling.

    Raises:
        QueryError: `x_metric` or `y_metric` is not a recognized metric name.
    """
    where_clause, filter_params = _scatter_where(
        turbine_id=turbine_id, x_metric=x_metric, y_metric=y_metric, start=start, end=end
    )
    return _scalar_count(con, where_clause, filter_params)


def _scalar_count(con: duckdb.DuckDBPyConnection, where_clause: str, params: Params) -> int:
    row = _fetchone(
        con, f"SELECT COUNT(*) FROM telemetry WHERE {where_clause}", params, "get_scatter_data"
    )
    assert row is not None
    return int(row[0])


# --------------------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------------------


def get_fleet_bounds(con: duckdb.DuckDBPyConnection) -> Bounds | None:
    """Return the lat/lon box containing every farm, or `None` if there are no farms."""
    row = _fetchone(
        con,
        "SELECT MIN(latitude), MAX(latitude), MIN(longitude), MAX(longitude) FROM farms",
        [],
        "get_fleet_bounds",
    )
    return _row_to_bounds(row)


def get_farm_turbine_bounds(con: duckdb.DuckDBPyConnection, farm_id: str) -> Bounds | None:
    """Return the lat/lon box containing one farm's turbines, or `None` if it has none."""
    row = _fetchone(
        con,
        "SELECT MIN(latitude), MAX(latitude), MIN(longitude), MAX(longitude) "
        "FROM turbines WHERE farm_id = ?",
        [farm_id],
        "get_farm_turbine_bounds",
    )
    return _row_to_bounds(row)


def _row_to_bounds(row: tuple[Any, ...] | None) -> Bounds | None:
    if row is None or row[0] is None:
        return None
    return Bounds(
        lat_min=float(row[0]), lat_max=float(row[1]), lon_min=float(row[2]), lon_max=float(row[3])
    )
