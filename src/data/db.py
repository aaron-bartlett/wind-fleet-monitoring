"""DuckDB connection and ingest pipeline (`CLAUDE.md` §4.1 — the persistence layer).

Loads the three source CSVs into DuckDB with explicit schemas, deduplicates telemetry on
`(turbine_id, timestamp)`, builds the `latest_telemetry` helper view, and records enough about
each source file to let `is_ingest_current` skip re-ingest on a later run against unchanged data.
This module owns every `CREATE TABLE`/`CREATE VIEW` in the project; `src/data/queries.py` (a
later phase) owns every read.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from config import Settings
from src.errors import DataLoadError

logger = logging.getLogger(__name__)

_REQUIRED_SOURCE_FILES: tuple[str, ...] = ("farms.csv", "turbines.csv", "telemetry.csv")

_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "farms.csv": {"farm_id", "farm_name", "latitude", "longitude"},
    "turbines.csv": {"turbine_id", "farm_id", "latitude", "longitude"},
    "telemetry.csv": {
        "turbine_id",
        "farm_id",
        "timestamp",
        "received_at",
        "power_output_kw",
        "wind_speed_ms",
        "rotor_rpm",
        "blade_pitch_deg",
        "gearbox_temp_c",
    },
}

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_INGEST_TABLES: tuple[str, ...] = ("farms", "turbines", "telemetry", "ingest_meta")


@dataclass(frozen=True, slots=True)
class IngestSummary:
    """Counts and timing produced by one call to `ingest`, logged and shown in the sidebar."""

    farms: int
    turbines: int
    telemetry_rows: int
    duplicates_removed: int
    rows_with_nulls: int
    telemetry_start: datetime
    telemetry_end: datetime
    elapsed_seconds: float


def connect(settings: Settings) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection at `settings.duckdb_path`.

    Args:
        settings: Runtime settings; `duckdb_path` of the literal `Path(":memory:")` opens an
            in-memory database instead of a file (used by tests).

    Returns:
        An open DuckDB connection. The caller owns its lifecycle.
    """
    if str(settings.duckdb_path) == ":memory:":
        return duckdb.connect(":memory:")
    settings.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(settings.duckdb_path))


def ingest(con: duckdb.DuckDBPyConnection, settings: Settings) -> IngestSummary:
    """Load `farms.csv`, `turbines.csv`, and `telemetry.csv` from `settings.data_dir` into DuckDB.

    Builds `farms`, `turbines`, `telemetry` (deduplicated, indexed) and the `latest_telemetry`
    view, and records each source file's mtime/size in `ingest_meta` for `is_ingest_current`.
    Runs as a single transaction: any failure leaves no partial schema behind.

    Args:
        con: An open DuckDB connection, as returned by `connect`.
        settings: Runtime settings; `settings.data_dir` must contain the three source CSVs.

    Returns:
        A populated `IngestSummary`.

    Raises:
        DataLoadError: A source file is missing, a required column is absent, or a telemetry
            timestamp does not match `YYYY-MM-DDTHH:MM:SSZ`.
    """
    started = time.monotonic()
    paths = _resolve_source_paths(settings.data_dir)

    con.execute("BEGIN TRANSACTION")
    committed = False
    try:
        _stage_farms(con, paths["farms.csv"])
        _stage_turbines(con, paths["turbines.csv"])
        _stage_telemetry(con, paths["telemetry.csv"])

        con.execute(
            "CREATE TABLE farms AS SELECT farm_id, farm_name, latitude, longitude FROM stg_farms"
        )
        con.execute(
            "CREATE TABLE turbines AS "
            "SELECT turbine_id, farm_id, latitude, longitude FROM stg_turbines"
        )
        _cast_telemetry_timestamps(con)
        duplicates_removed = _dedup_telemetry(con)
        _create_telemetry_indexes(con)
        _create_latest_telemetry_view(con)
        _write_ingest_meta(con, paths)

        summary = _build_summary(con, duplicates_removed, started)
        con.execute("COMMIT")
        committed = True
    finally:
        if not committed:
            con.execute("ROLLBACK")

    logger.info(
        "Ingest complete: farms=%d turbines=%d telemetry=%d duplicates_removed=%d "
        "rows_with_nulls=%d range=[%s, %s] elapsed=%.3fs",
        summary.farms,
        summary.turbines,
        summary.telemetry_rows,
        summary.duplicates_removed,
        summary.rows_with_nulls,
        summary.telemetry_start,
        summary.telemetry_end,
        summary.elapsed_seconds,
    )
    return summary


def is_ingest_current(con: duckdb.DuckDBPyConnection, settings: Settings) -> bool:
    """Return whether the DuckDB schema already reflects the current source CSVs.

    Compares each source file's live mtime and size against the values `ingest` recorded in
    `ingest_meta`. Used to skip a redundant re-ingest on process restart (`PROJECT_SPEC.md` §12).

    Args:
        con: An open DuckDB connection.
        settings: Runtime settings naming the source directory to check against.

    Returns:
        `True` only if every expected table exists and every source file's mtime and size are
        unchanged since the last ingest; `False` otherwise (including "never ingested").
    """
    existing_tables = {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN (?, ?, ?, ?)",
            list(_INGEST_TABLES),
        ).fetchall()
    }
    if existing_tables != set(_INGEST_TABLES):
        return False

    stored_meta = dict(con.execute("SELECT key, value FROM ingest_meta").fetchall())

    for name in _REQUIRED_SOURCE_FILES:
        path = settings.data_dir / name
        if not path.exists():
            return False
        stat = path.stat()
        if stored_meta.get(f"{name}:mtime") != str(stat.st_mtime):
            return False
        if stored_meta.get(f"{name}:size") != str(stat.st_size):
            return False
    return True


def _resolve_source_paths(data_dir: Path) -> dict[str, Path]:
    """Return `{filename: path}` for the three required source files.

    Raises:
        DataLoadError: One of the required files does not exist.
    """
    paths: dict[str, Path] = {}
    for name in _REQUIRED_SOURCE_FILES:
        path = data_dir / name
        if not path.exists():
            raise DataLoadError(f"Missing required data file: {path}")
        paths[name] = path
    return paths


def _stage_farms(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    con.execute(
        "CREATE OR REPLACE TABLE stg_farms AS SELECT * FROM read_csv_auto(?, header=true)",
        [str(path)],
    )
    _check_required_columns(con, "stg_farms", "farms.csv")


def _stage_turbines(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    con.execute(
        "CREATE OR REPLACE TABLE stg_turbines AS SELECT * FROM read_csv_auto(?, header=true)",
        [str(path)],
    )
    _check_required_columns(con, "stg_turbines", "turbines.csv")


def _stage_telemetry(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    # `timestamp`/`received_at` are forced to VARCHAR here so DuckDB's type sniffer never
    # silently auto-parses them; the explicit strptime cast in `_cast_telemetry_timestamps`
    # is the only place a malformed timestamp can be produced or caught.
    con.execute(
        "CREATE OR REPLACE TABLE stg_telemetry AS "
        "SELECT * FROM read_csv_auto(?, header=true, "
        "types={'timestamp': 'VARCHAR', 'received_at': 'VARCHAR'})",
        [str(path)],
    )
    _check_required_columns(con, "stg_telemetry", "telemetry.csv")


def _check_required_columns(
    con: duckdb.DuckDBPyConnection, staging_table: str, source_file: str
) -> None:
    """Raise DataLoadError naming `source_file` and the missing column(s), if any are absent.

    `staging_table` is always one of this module's own internal constants, never CSV-derived
    content, so interpolating it into `DESCRIBE` is not the kind of value CLAUDE.md §5.4 requires
    binding — there is no `?` placeholder for a table identifier in DuckDB's grammar either way.
    """
    actual_columns = {row[0] for row in con.execute(f"DESCRIBE {staging_table}").fetchall()}
    missing = _REQUIRED_COLUMNS[source_file] - actual_columns
    if missing:
        raise DataLoadError(
            f"{source_file} is missing required column(s): {', '.join(sorted(missing))}"
        )


def _cast_telemetry_timestamps(con: duckdb.DuckDBPyConnection) -> None:
    """Cast `timestamp`/`received_at` to tz-aware UTC via `strptime`.

    Raises:
        DataLoadError: A value does not match `YYYY-MM-DDTHH:MM:SSZ`. `strptime` raises a
            `duckdb.Error` at execution time (not bind time) the moment it hits an unparseable
            row, which is what makes this fail loudly rather than silently coercing to NULL.
    """
    try:
        con.execute(
            "CREATE TABLE stg_telemetry_typed AS "
            "SELECT turbine_id, farm_id, "
            "strptime(timestamp, ?) AT TIME ZONE 'UTC' AS timestamp, "
            "strptime(received_at, ?) AT TIME ZONE 'UTC' AS received_at, "
            "power_output_kw, wind_speed_ms, rotor_rpm, blade_pitch_deg, gearbox_temp_c "
            "FROM stg_telemetry",
            [_TIMESTAMP_FORMAT, _TIMESTAMP_FORMAT],
        )
    except duckdb.Error as exc:
        raise DataLoadError(
            "telemetry.csv contains a 'timestamp' or 'received_at' value that does not match "
            f"the required format 'YYYY-MM-DDTHH:MM:SSZ': {exc}"
        ) from exc


def _dedup_telemetry(con: duckdb.DuckDBPyConnection) -> int:
    """Keep, per `(turbine_id, timestamp)`, only the row with the greatest `received_at`.

    Returns:
        The number of rows removed as duplicates (row count before minus after).
    """
    before = con.execute("SELECT COUNT(*) FROM stg_telemetry_typed").fetchone()
    assert before is not None
    con.execute(
        "CREATE TABLE telemetry AS "
        "SELECT turbine_id, farm_id, timestamp, received_at, "
        "power_output_kw, wind_speed_ms, rotor_rpm, blade_pitch_deg, gearbox_temp_c "
        "FROM ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "    PARTITION BY turbine_id, timestamp ORDER BY received_at DESC"
        "  ) AS rn"
        "  FROM stg_telemetry_typed"
        ") WHERE rn = 1"
    )
    after = con.execute("SELECT COUNT(*) FROM telemetry").fetchone()
    assert after is not None
    return int(before[0]) - int(after[0])


def _create_telemetry_indexes(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE INDEX idx_tel_turbine_ts ON telemetry(turbine_id, timestamp)")
    con.execute("CREATE INDEX idx_tel_farm_ts ON telemetry(farm_id, timestamp)")


def _create_latest_telemetry_view(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        "CREATE OR REPLACE VIEW latest_telemetry AS "
        "SELECT * EXCLUDE (rn) FROM ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY turbine_id ORDER BY timestamp DESC) AS rn"
        "  FROM telemetry"
        ") WHERE rn = 1"
    )


def _write_ingest_meta(con: duckdb.DuckDBPyConnection, paths: dict[str, Path]) -> None:
    con.execute("CREATE TABLE ingest_meta (key VARCHAR, value VARCHAR)")
    rows: list[tuple[str, str]] = []
    for name, path in paths.items():
        stat = path.stat()
        rows.append((f"{name}:mtime", str(stat.st_mtime)))
        rows.append((f"{name}:size", str(stat.st_size)))
    con.executemany("INSERT INTO ingest_meta (key, value) VALUES (?, ?)", rows)


def _build_summary(
    con: duckdb.DuckDBPyConnection, duplicates_removed: int, started: float
) -> IngestSummary:
    farms_count = con.execute("SELECT COUNT(*) FROM farms").fetchone()
    turbines_count = con.execute("SELECT COUNT(*) FROM turbines").fetchone()
    telemetry_count = con.execute("SELECT COUNT(*) FROM telemetry").fetchone()
    nulls_count = con.execute(
        "SELECT COUNT(*) FROM telemetry WHERE "
        "power_output_kw IS NULL OR wind_speed_ms IS NULL OR rotor_rpm IS NULL OR "
        "blade_pitch_deg IS NULL OR gearbox_temp_c IS NULL"
    ).fetchone()
    time_range = con.execute("SELECT MIN(timestamp), MAX(timestamp) FROM telemetry").fetchone()
    assert farms_count is not None
    assert turbines_count is not None
    assert telemetry_count is not None
    assert nulls_count is not None
    assert time_range is not None
    telemetry_start, telemetry_end = time_range
    if telemetry_start is None or telemetry_end is None:
        raise DataLoadError("telemetry.csv produced zero rows after ingest.")
    return IngestSummary(
        farms=int(farms_count[0]),
        turbines=int(turbines_count[0]),
        telemetry_rows=int(telemetry_count[0]),
        duplicates_removed=duplicates_removed,
        rows_with_nulls=int(nulls_count[0]),
        telemetry_start=telemetry_start,
        telemetry_end=telemetry_end,
        elapsed_seconds=time.monotonic() - started,
    )
