"""Tests for `src/domain/aggregates.py`: the view-model builders each dashboard renders.

Uses only `tests/fixtures/` (never the real `data/` CSVs), per `CLAUDE.md` §4.3.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from config import Settings
from src.data import queries
from src.domain import aggregates
from src.domain.models import HealthStatus
from src.errors import DataLoadError

_SETTINGS = Settings(
    data_dir=Path("unused"), duckdb_path=Path(":memory:"), sim_now=None, stale_after_minutes=15
)

# TURB001's latest record; every fixture turbine reports at or before this timestamp.
_FRESH_NOW = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
# Well past every fixture record's staleness window (15 min after the last 00:55 reading).
_STALE_NOW = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# build_fleet_summary
# --------------------------------------------------------------------------------------


def test_build_fleet_summary_counts_match_fixture(db_con: duckdb.DuckDBPyConnection) -> None:
    summary = aggregates.build_fleet_summary(db_con, _SETTINGS, _FRESH_NOW)
    assert summary.farm_count == 3
    assert summary.turbine_count == 4  # TURB001, TURB002, TURB003, TURB999
    assert sum(summary.status_counts.values()) == 4
    assert summary.now_utc == _FRESH_NOW


def test_build_fleet_summary_matches_hand_computed_energy(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    # Hand-computed from tests/fixtures/telemetry.csv (post-dedup), cross-checked against
    # test_queries.py's independently hand-computed fleet total: 5.8075 MWh.
    summary = aggregates.build_fleet_summary(db_con, _SETTINGS, _FRESH_NOW)
    assert summary.total_energy_mwh == pytest.approx(5.8075)


def test_build_fleet_summary_current_power_excludes_stale_records(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    fresh = aggregates.build_fleet_summary(db_con, _SETTINGS, _FRESH_NOW)
    assert fresh.current_power_kw > 0.0

    stale = aggregates.build_fleet_summary(db_con, _SETTINGS, _STALE_NOW)
    assert stale.current_power_kw == 0.0
    assert stale.status_counts[HealthStatus.ERROR] == 4  # every turbine now stale


# --------------------------------------------------------------------------------------
# build_farm_summary
# --------------------------------------------------------------------------------------


def test_build_farm_summary_with_turbines_matches_hand_computed_energy(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    # FARM01 = TURB001 (sum 22,050.0, see test_queries.py) + TURB002 (sum 24,120.0).
    # (22050 + 24120) * (5 / 60) / 1000 = 3.8475 MWh.
    summary = aggregates.build_farm_summary(db_con, _SETTINGS, _FRESH_NOW, "FARM01")
    assert summary.farm.farm_id == "FARM01"
    assert summary.turbine_count == 2
    assert summary.total_energy_mwh == pytest.approx(3.8475)
    assert summary.tz_label  # some non-empty timezone abbreviation was resolved


def test_build_farm_summary_for_farm_with_no_turbines_is_zeroed_not_error(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    summary = aggregates.build_farm_summary(db_con, _SETTINGS, _FRESH_NOW, "FARM03")
    assert summary.turbine_count == 0
    assert summary.current_power_kw == 0.0
    assert summary.total_energy_mwh == 0.0
    assert summary.health_score is None
    assert summary.alert_reason is None
    assert sum(summary.status_counts.values()) == 0


def test_build_farm_summary_unknown_farm_id_raises(db_con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(DataLoadError):
        aggregates.build_farm_summary(db_con, _SETTINGS, _FRESH_NOW, "NOPE")


# --------------------------------------------------------------------------------------
# build_turbine_summary
# --------------------------------------------------------------------------------------


def test_build_turbine_summary_with_telemetry(db_con: duckdb.DuckDBPyConnection) -> None:
    summary = aggregates.build_turbine_summary(db_con, _SETTINGS, _FRESH_NOW, "TURB001")
    assert summary.turbine.turbine_id == "TURB001"
    assert summary.farm.farm_id == "FARM01"
    assert summary.record is not None
    assert summary.record.power_output_kw == 2080.0


def test_build_turbine_summary_no_telemetry_classifies_as_error(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    summary = aggregates.build_turbine_summary(db_con, _SETTINGS, _FRESH_NOW, "TURB999")
    assert summary.turbine.turbine_id == "TURB999"
    assert summary.farm.farm_id == "FARM02"
    assert summary.record is None
    assert summary.health.status is HealthStatus.ERROR


def test_build_turbine_summary_unknown_turbine_id_raises(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(DataLoadError):
        aggregates.build_turbine_summary(db_con, _SETTINGS, _FRESH_NOW, "NOPE")


# --------------------------------------------------------------------------------------
# build_turbine_map_rows
# --------------------------------------------------------------------------------------


def test_build_turbine_map_rows_includes_turbine_with_no_telemetry(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    rows = aggregates.build_turbine_map_rows(db_con, _SETTINGS, _FRESH_NOW, "FARM02")
    status_by_id = {turbine.turbine_id: result.status for turbine, result in rows}
    assert set(status_by_id) == {"TURB003", "TURB999"}
    assert status_by_id["TURB999"] == HealthStatus.ERROR
    assert status_by_id["TURB003"] == HealthStatus.HEALTHY  # clean latest record


def test_build_turbine_map_rows_empty_for_farm_with_no_turbines(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    assert aggregates.build_turbine_map_rows(db_con, _SETTINGS, _FRESH_NOW, "FARM03") == []


# --------------------------------------------------------------------------------------
# build_farm_map_rows — the fixed-query-count contract (PROJECT_SPEC.md §12)
# --------------------------------------------------------------------------------------


def test_build_farm_map_rows_covers_every_farm_including_the_empty_one(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    rows = aggregates.build_farm_map_rows(db_con, _SETTINGS, _FRESH_NOW)
    by_id = {row.farm.farm_id: row for row in rows}
    assert set(by_id) == {"FARM01", "FARM02", "FARM03"}

    assert by_id["FARM01"].turbine_count == 2
    assert by_id["FARM01"].health_score is not None

    assert by_id["FARM03"].turbine_count == 0
    assert by_id["FARM03"].health_score is None
    assert by_id["FARM03"].alert_reason is None


def test_build_farm_map_rows_flags_the_error_heavy_farm(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    # FARM02 has 2 turbines; TURB999 has no telemetry at all, so it is ERROR (50% > 20%
    # FARM_ALERT_ERROR_FRACTION threshold) regardless of TURB003's own status.
    rows = aggregates.build_farm_map_rows(db_con, _SETTINGS, _FRESH_NOW)
    farm02 = next(row for row in rows if row.farm.farm_id == "FARM02")
    assert farm02.alert_reason is not None
    assert "Error" in farm02.alert_reason


def test_build_farm_map_rows_issues_a_fixed_number_of_queries_never_per_farm(
    db_con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-farm query loop would scale with farm count; this asserts it never happens."""
    calls: list[str] = []

    def _counted(name: str, original: Any) -> Any:
        def wrapper(*args: object, **kwargs: object) -> object:
            calls.append(name)
            return original(*args, **kwargs)

        return wrapper

    for name in ("get_farms", "get_turbine_counts_by_farm", "get_latest_records"):
        monkeypatch.setattr(queries, name, _counted(name, getattr(queries, name)))

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("build_farm_map_rows must not query per farm")

    monkeypatch.setattr(queries, "get_turbines", _forbidden)
    monkeypatch.setattr(queries, "get_latest_record_for_turbine", _forbidden)

    rows = aggregates.build_farm_map_rows(db_con, _SETTINGS, _FRESH_NOW)

    assert len(rows) == 3
    assert len(calls) == 3  # exactly one call to each of the three allowed query functions
    assert set(calls) == {"get_farms", "get_turbine_counts_by_farm", "get_latest_records"}
