"""Tests for `src/domain/clock.py`: "now" resolution, staleness, and window starts.

Uses only `tests/fixtures/` (never the real `data/` CSVs), per `CLAUDE.md` §4.3.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from config import Settings
from src.domain import clock

# --------------------------------------------------------------------------------------
# get_now
# --------------------------------------------------------------------------------------


def test_get_now_honors_sim_now(db_con: duckdb.DuckDBPyConnection, fixtures_dir: Path) -> None:
    sim_now = datetime(2030, 6, 15, 12, 0, tzinfo=UTC)
    settings = Settings(
        data_dir=fixtures_dir, duckdb_path=Path(":memory:"), sim_now=sim_now, stale_after_minutes=15
    )
    assert clock.get_now(db_con, settings) == sim_now


def test_get_now_falls_back_to_max_timestamp(
    db_con: duckdb.DuckDBPyConnection, fixtures_dir: Path
) -> None:
    settings = Settings(
        data_dir=fixtures_dir, duckdb_path=Path(":memory:"), sim_now=None, stale_after_minutes=15
    )
    # Matches the fixture's known telemetry_end (asserted in tests/test_ingest.py).
    assert clock.get_now(db_con, settings) == datetime(2026, 1, 1, 0, 55, tzinfo=UTC)


def test_get_now_falls_back_to_wall_clock_when_no_telemetry(fixtures_dir: Path) -> None:
    settings = Settings(
        data_dir=fixtures_dir, duckdb_path=Path(":memory:"), sim_now=None, stale_after_minutes=15
    )
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE telemetry (timestamp TIMESTAMPTZ)")

    before = datetime.now(UTC)
    now = clock.get_now(con, settings)
    after = datetime.now(UTC)

    assert now.tzinfo is not None
    assert before <= now <= after


# --------------------------------------------------------------------------------------
# is_stale
# --------------------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("age_minutes", "expected_stale"),
    [
        (14, False),
        (15, False),  # exactly at the boundary is not stale (strict `<`)
        (16, True),
    ],
)
def test_is_stale_boundary(age_minutes: int, expected_stale: bool) -> None:
    record_timestamp = _NOW - timedelta(minutes=age_minutes)
    assert clock.is_stale(record_timestamp, _NOW, stale_after_minutes=15) is expected_stale


def test_is_stale_raises_on_naive_record_timestamp() -> None:
    with pytest.raises(ValueError, match="record_timestamp"):
        clock.is_stale(datetime(2026, 1, 1), _NOW, stale_after_minutes=15)


def test_is_stale_raises_on_naive_now() -> None:
    with pytest.raises(ValueError, match="now"):
        clock.is_stale(_NOW, datetime(2026, 1, 1), stale_after_minutes=15)


# --------------------------------------------------------------------------------------
# window_start
# --------------------------------------------------------------------------------------


def test_window_start_all_is_none() -> None:
    assert clock.window_start(_NOW, "all") is None


def test_window_start_24h() -> None:
    assert clock.window_start(_NOW, "24h") == _NOW - timedelta(hours=24)


def test_window_start_7d() -> None:
    assert clock.window_start(_NOW, "7d") == _NOW - timedelta(days=7)


def test_window_start_unknown_key_raises() -> None:
    with pytest.raises(ValueError, match="Unknown window_key"):
        clock.window_start(_NOW, "1y")
