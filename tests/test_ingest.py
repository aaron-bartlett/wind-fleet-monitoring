"""Tests for `src/data/db.py`: schema, dedup, TIMESTAMPTZ casting, and change detection.

Uses only `tests/fixtures/` (never the real `data/` CSVs), per `CLAUDE.md` §4.3.
"""

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from config import Settings
from src.data import db
from src.errors import DataLoadError


def _settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir, duckdb_path=Path(":memory:"), sim_now=None, stale_after_minutes=15
    )


def _ingest(data_dir: Path) -> tuple[duckdb.DuckDBPyConnection, db.IngestSummary]:
    settings = _settings(data_dir)
    con = db.connect(settings)
    summary = db.ingest(con, settings)
    return con, summary


# --------------------------------------------------------------------------------------
# Row counts, dedup, and schema shape
# --------------------------------------------------------------------------------------


def test_ingest_produces_expected_row_counts(fixtures_dir: Path) -> None:
    _, summary = _ingest(fixtures_dir)
    assert summary.farms == 3
    assert summary.turbines == 4
    assert summary.telemetry_rows == 35
    assert summary.duplicates_removed == 1
    assert summary.rows_with_nulls == 1
    assert summary.telemetry_start == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert summary.telemetry_end == datetime(2026, 1, 1, 0, 55, tzinfo=UTC)
    assert summary.elapsed_seconds >= 0.0


def test_dedup_keeps_the_row_with_the_greatest_received_at(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    row = db_con.execute(
        "SELECT power_output_kw, received_at FROM telemetry "
        "WHERE turbine_id = 'TURB001' AND timestamp = TIMESTAMPTZ '2026-01-01 00:25:00+00'"
    ).fetchone()
    assert row is not None
    power_output_kw, received_at = row
    # The fixture's duplicate pair for this timestamp has received_at at :27 and :33; the
    # :33 row (power 1800.0) must be the survivor, not the earlier :27 row (power 2100.0).
    assert power_output_kw == 1800.0
    assert received_at == datetime(2026, 1, 1, 0, 33, tzinfo=UTC)


def test_turbines_table_has_no_farm_name_column(db_con: duckdb.DuckDBPyConnection) -> None:
    columns = {row[0] for row in db_con.execute("DESCRIBE turbines").fetchall()}
    assert "farm_name" not in columns
    assert columns == {"turbine_id", "farm_id", "latitude", "longitude"}


def test_telemetry_timestamps_are_tz_aware_utc(db_con: duckdb.DuckDBPyConnection) -> None:
    row = db_con.execute(
        "SELECT timestamp, received_at FROM telemetry "
        "WHERE turbine_id = 'TURB001' AND timestamp = TIMESTAMPTZ '2026-01-01 00:00:00+00'"
    ).fetchone()
    assert row is not None
    timestamp, received_at = row
    assert timestamp.tzinfo is not None
    assert received_at.tzinfo is not None
    assert timestamp == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert received_at == datetime(2026, 1, 1, 0, 2, tzinfo=UTC)


def test_latest_telemetry_has_one_row_per_turbine_with_telemetry(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    rows = db_con.execute("SELECT turbine_id, timestamp FROM latest_telemetry").fetchall()
    turbine_ids = {row[0] for row in rows}
    assert turbine_ids == {"TURB001", "TURB002", "TURB003"}
    assert "TURB999" not in turbine_ids  # no telemetry at all

    turb001_latest = next(ts for tid, ts in rows if tid == "TURB001")
    assert turb001_latest == datetime(2026, 1, 1, 0, 55, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Fail-fast, fail-loud errors
# --------------------------------------------------------------------------------------


def test_malformed_timestamp_raises_data_load_error(fixtures_dir: Path) -> None:
    with pytest.raises(DataLoadError) as exc_info:
        _ingest(fixtures_dir / "bad_timestamp")
    message = str(exc_info.value)
    assert "telemetry.csv" in message
    assert "format" in message


def test_missing_column_raises_data_load_error(fixtures_dir: Path) -> None:
    with pytest.raises(DataLoadError) as exc_info:
        _ingest(fixtures_dir / "missing_column")
    message = str(exc_info.value)
    assert "telemetry.csv" in message
    assert "gearbox_temp_c" in message


def test_missing_file_raises_data_load_error(fixtures_dir: Path, tmp_path: Path) -> None:
    shutil.copy(fixtures_dir / "farms.csv", tmp_path / "farms.csv")
    shutil.copy(fixtures_dir / "turbines.csv", tmp_path / "turbines.csv")
    # telemetry.csv deliberately not copied.

    with pytest.raises(DataLoadError) as exc_info:
        _ingest(tmp_path)
    message = str(exc_info.value)
    assert str(tmp_path / "telemetry.csv") in message


# --------------------------------------------------------------------------------------
# is_ingest_current
# --------------------------------------------------------------------------------------


def test_is_ingest_current_true_immediately_after_ingest(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    for name in ("farms.csv", "turbines.csv", "telemetry.csv"):
        shutil.copy(fixtures_dir / name, tmp_path / name)
    settings = _settings(tmp_path)
    con = db.connect(settings)
    db.ingest(con, settings)

    assert db.is_ingest_current(con, settings) is True


def test_is_ingest_current_false_after_source_file_mtime_changes(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    for name in ("farms.csv", "turbines.csv", "telemetry.csv"):
        shutil.copy(fixtures_dir / name, tmp_path / name)
    settings = _settings(tmp_path)
    con = db.connect(settings)
    db.ingest(con, settings)
    assert db.is_ingest_current(con, settings) is True

    telemetry_path = tmp_path / "telemetry.csv"
    original_mtime = telemetry_path.stat().st_mtime
    # An explicit forward bump rather than a bare "touch" — some filesystems have 1-second
    # mtime resolution, which would make a same-instant touch a flaky no-op.
    bumped_mtime = original_mtime + 10
    os.utime(telemetry_path, (bumped_mtime, bumped_mtime))

    assert db.is_ingest_current(con, settings) is False


def test_is_ingest_current_false_before_any_ingest(fixtures_dir: Path) -> None:
    settings = _settings(fixtures_dir)
    con = db.connect(settings)
    assert db.is_ingest_current(con, settings) is False
