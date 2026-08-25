"""Tests for `src/data/queries.py`: every read query against the DuckDB schema.

Uses only `tests/fixtures/` (never the real `data/` CSVs), per `CLAUDE.md` §4.3.
"""

import math
from datetime import UTC, datetime

import duckdb
import pytest

from src.data import queries
from src.domain.models import Level
from src.errors import QueryError

# --------------------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------------------


def test_get_turbine_counts_by_farm_includes_zero_turbine_farm(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    counts = queries.get_turbine_counts_by_farm(db_con)
    assert counts == {"FARM01": 2, "FARM02": 2, "FARM03": 0}


def test_get_farms_returns_all_farms_ordered(db_con: duckdb.DuckDBPyConnection) -> None:
    farms = queries.get_farms(db_con)
    assert [f.farm_id for f in farms] == ["FARM01", "FARM02", "FARM03"]


def test_get_turbines_filters_by_farm(db_con: duckdb.DuckDBPyConnection) -> None:
    turbines = queries.get_turbines(db_con, farm_id="FARM02")
    assert {t.turbine_id for t in turbines} == {"TURB003", "TURB999"}


def test_get_latest_records_filters_by_farm(db_con: duckdb.DuckDBPyConnection) -> None:
    records = queries.get_latest_records(db_con, farm_id="FARM01")
    assert {r.turbine_id for r in records} == {"TURB001", "TURB002"}


def test_get_latest_record_for_turbine_returns_none_for_no_telemetry(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    assert queries.get_latest_record_for_turbine(db_con, "TURB999") is None


def test_get_latest_record_for_turbine_returns_the_latest_row(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    record = queries.get_latest_record_for_turbine(db_con, "TURB001")
    assert record is not None
    assert record.timestamp == datetime(2026, 1, 1, 0, 55, tzinfo=UTC)
    assert record.power_output_kw == 2080.0


# --------------------------------------------------------------------------------------
# get_power_timeseries — the time-spine LEFT JOIN
# --------------------------------------------------------------------------------------


def test_get_power_timeseries_produces_a_null_gap_row_and_a_complete_spine(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    # TURB001's own telemetry has a genuine missing interval at 00:15 (jumps 00:10 -> 00:20).
    # At *fleet* level this fixture's other two turbines both report at 00:15, so the fleet
    # sum would mask the gap. Querying at turbine level for TURB001 is a strictly clearer
    # (and still faithful) demonstration of the same spine/LEFT-JOIN mechanism: it is the one
    # scope in this fixture where the gap is actually observable end to end.
    df = queries.get_power_timeseries(
        db_con,
        level=Level.TURBINE,
        entity_id="TURB001",
        start=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 55, tzinfo=UTC),
        bucket="5 minutes",
        max_points=100,
    )
    assert len(df) == 12  # 00:00, 00:05, ..., 00:55 inclusive
    gap_row = df[df["bucket_start"] == datetime(2026, 1, 1, 0, 15, tzinfo=UTC)]
    assert len(gap_row) == 1
    assert math.isnan(gap_row["power_kw"].iloc[0])
    # Every other bucket is present and non-null (spine completeness, not just the one gap).
    non_gap = df[df["bucket_start"] != datetime(2026, 1, 1, 0, 15, tzinfo=UTC)]
    assert non_gap["power_kw"].notna().all()
    assert non_gap["power_kw"].sum() == pytest.approx(22050.0)


def test_get_power_timeseries_rejects_an_unrecognized_bucket(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(QueryError):
        queries.get_power_timeseries(
            db_con,
            level=Level.FLEET,
            entity_id=None,
            start=None,
            end=datetime(2026, 1, 1, 0, 55, tzinfo=UTC),
            bucket="3 minutes",  # not one of config.BUCKET_BY_WINDOW's values
            max_points=100,
        )


def test_get_power_timeseries_never_silently_truncates(db_con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(QueryError):
        queries.get_power_timeseries(
            db_con,
            level=Level.TURBINE,
            entity_id="TURB001",
            start=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 0, 55, tzinfo=UTC),
            bucket="5 minutes",
            max_points=5,  # the spine has 12 buckets
        )


def test_get_power_timeseries_missing_entity_id_raises(db_con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(QueryError):
        queries.get_power_timeseries(
            db_con,
            level=Level.FARM,
            entity_id=None,
            start=None,
            end=datetime(2026, 1, 1, 0, 55, tzinfo=UTC),
            bucket="5 minutes",
            max_points=100,
        )


# --------------------------------------------------------------------------------------
# Energy and current power
# --------------------------------------------------------------------------------------


def test_get_total_energy_mwh_matches_hand_computed_fleet_sum(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    # Hand-computed from tests/fixtures/telemetry.csv (post-dedup): fleet-wide
    # SUM(power_output_kw) = 69,690.0 kW across 35 rows.
    # 69,690 * (5 / 60) / 1000 = 5.8075 MWh.
    energy_mwh = queries.get_total_energy_mwh(db_con, level=Level.FLEET, entity_id=None)
    assert energy_mwh == pytest.approx(5.8075)


def test_get_total_energy_mwh_is_zero_for_a_turbine_with_no_telemetry(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    energy_mwh = queries.get_total_energy_mwh(db_con, level=Level.TURBINE, entity_id="TURB999")
    assert energy_mwh == 0.0


def test_get_current_power_kw_excludes_a_stale_record(db_con: duckdb.DuckDBPyConnection) -> None:
    # TURB001's latest record is at 00:55 (2080.0 kW).
    fresh_now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)  # 5 min later — still current
    stale_now = datetime(2026, 1, 1, 1, 15, tzinfo=UTC)  # 20 min later — now stale

    fresh_power_kw = queries.get_current_power_kw(
        db_con, level=Level.TURBINE, entity_id="TURB001", now=fresh_now, stale_after_minutes=15
    )
    assert fresh_power_kw == pytest.approx(2080.0)

    stale_power_kw = queries.get_current_power_kw(
        db_con, level=Level.TURBINE, entity_id="TURB001", now=stale_now, stale_after_minutes=15
    )
    assert stale_power_kw == 0.0


# --------------------------------------------------------------------------------------
# get_scatter_data — deterministic modulo-stride down-sampling
# --------------------------------------------------------------------------------------


def test_get_scatter_data_downsamples_deterministically(db_con: duckdb.DuckDBPyConnection) -> None:
    kwargs = {
        "turbine_id": "TURB001",
        "x_metric": "wind_speed_ms",
        "y_metric": "power_output_kw",
        "start": None,
        "end": datetime(2026, 1, 1, 0, 55, tzinfo=UTC),
        "max_points": 5,
    }
    first = queries.get_scatter_data(db_con, **kwargs)
    second = queries.get_scatter_data(db_con, **kwargs)
    assert 0 < len(first) <= 5
    assert first.equals(second)  # never `ORDER BY random()` — same input, same rows


def test_get_scatter_data_rejects_sql_injection_attempt(db_con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(QueryError):
        queries.get_scatter_data(
            db_con,
            turbine_id="TURB001",
            x_metric="; DROP TABLE telemetry",
            y_metric="power_output_kw",
            start=None,
            end=datetime(2026, 1, 1, 0, 55, tzinfo=UTC),
            max_points=100,
        )
    # The schema must be untouched — the invalid metric never reached a SQL string.
    assert queries.get_farms(db_con) != []


def test_get_scatter_data_rejects_unknown_metric(db_con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(QueryError):
        queries.get_scatter_data(
            db_con,
            turbine_id="TURB001",
            x_metric="wind_speed_ms",
            y_metric="not_a_real_metric",
            start=None,
            end=datetime(2026, 1, 1, 0, 55, tzinfo=UTC),
            max_points=100,
        )


# --------------------------------------------------------------------------------------
# get_scatter_sample_size — the pre-sample row count get_scatter_data down-samples from
# --------------------------------------------------------------------------------------


def test_get_scatter_sample_size_matches_unsampled_row_count(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    kwargs = {
        "turbine_id": "TURB001",
        "x_metric": "wind_speed_ms",
        "y_metric": "power_output_kw",
        "start": None,
        "end": datetime(2026, 1, 1, 0, 55, tzinfo=UTC),
    }
    total = queries.get_scatter_sample_size(db_con, **kwargs)
    # 12 raw TURB001 rows in this window, minus the fixture's one duplicate (turbine_id,
    # timestamp) — deduped on ingest, keeping the later received_at — leaves 11 with both
    # metrics non-NULL. Independent of any max_points a caller later down-samples to.
    assert total == 11

    down_sampled = queries.get_scatter_data(db_con, **kwargs, max_points=5)
    assert len(down_sampled) < total  # confirms down-sampling actually occurred


def test_get_scatter_sample_size_rejects_unknown_metric(db_con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(QueryError):
        queries.get_scatter_sample_size(
            db_con,
            turbine_id="TURB001",
            x_metric="wind_speed_ms",
            y_metric="not_a_real_metric",
            start=None,
            end=datetime(2026, 1, 1, 0, 55, tzinfo=UTC),
        )


# --------------------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------------------


def test_get_farm_turbine_bounds_none_for_farm_with_no_turbines(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    assert queries.get_farm_turbine_bounds(db_con, "FARM03") is None


def test_get_farm_turbine_bounds_for_farm_with_turbines(
    db_con: duckdb.DuckDBPyConnection,
) -> None:
    bounds = queries.get_farm_turbine_bounds(db_con, "FARM01")
    assert bounds is not None
    assert bounds.lat_min == pytest.approx(41.263)
    assert bounds.lat_max == pytest.approx(41.271)
    assert bounds.lon_min == pytest.approx(-96.518)
    assert bounds.lon_max == pytest.approx(-96.505)


def test_get_fleet_bounds_contains_all_farms(db_con: duckdb.DuckDBPyConnection) -> None:
    bounds = queries.get_fleet_bounds(db_con)
    assert bounds is not None
    assert bounds.lat_min == pytest.approx(35.12)
    assert bounds.lat_max == pytest.approx(41.25)
    assert bounds.lon_min == pytest.approx(-106.55)
    assert bounds.lon_max == pytest.approx(-96.53)
