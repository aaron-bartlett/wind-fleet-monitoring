"""Fleet/farm/turbine view-model builders — the aggregation composition layer.

Domain layer (`CLAUDE.md` §4.1): assembles the exact dataclasses each dashboard renders by
composing `src/data/queries`, `clock`, `geo`, and `health`. No SQL and no arithmetic that
belongs in SQL lives here — every number comes from a query or a `health`/`geo` helper; this
module's own job is shaping those results into named view-models and, for the fleet-wide map
roll-up, grouping per-farm data in Python instead of issuing a query per farm
(`PROJECT_SPEC.md` §12).
"""

from dataclasses import dataclass
from datetime import datetime

import duckdb

from config import Settings
from src.data import queries
from src.domain import geo, health
from src.domain.models import Farm, HealthResult, HealthStatus, Level, TelemetryRecord, Turbine
from src.errors import DataLoadError

# --------------------------------------------------------------------------------------
# View-models
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FleetSummary:
    """The Fleet Dashboard's headline figures (`PROJECT_SPEC.md` §10.2)."""

    current_power_kw: float
    total_energy_mwh: float
    farm_count: int
    turbine_count: int
    status_counts: dict[HealthStatus, int]
    now_utc: datetime


@dataclass(frozen=True, slots=True)
class FarmSummary:
    """The Farm Dashboard's headline figures (`PROJECT_SPEC.md` §10.3)."""

    farm: Farm
    local_time: datetime
    tz_label: str
    current_power_kw: float
    total_energy_mwh: float
    turbine_count: int
    status_counts: dict[HealthStatus, int]
    health_score: float | None
    alert_reason: str | None


@dataclass(frozen=True, slots=True)
class TurbineSummary:
    """The Turbine Dashboard's headline figures (`PROJECT_SPEC.md` §10.4)."""

    turbine: Turbine
    farm: Farm
    local_time: datetime
    tz_label: str
    record: TelemetryRecord | None
    health: HealthResult


@dataclass(frozen=True, slots=True)
class FarmMapRow:
    """One farm's fleet-map marker data: dot color input and alert badge."""

    farm: Farm
    turbine_count: int
    health_score: float | None
    alert_reason: str | None


# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def build_fleet_summary(
    con: duckdb.DuckDBPyConnection, settings: Settings, now: datetime
) -> FleetSummary:
    """Assemble the Fleet Dashboard's headline figures.

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings (staleness threshold).
        now: The resolved "now" (`clock.get_now`).

    Returns:
        A populated `FleetSummary`.
    """
    farms = queries.get_farms(con)
    turbines = queries.get_turbines(con)
    latest_records = queries.get_latest_records(con)
    results = health.classify_many(
        latest_records,
        [turbine.turbine_id for turbine in turbines],
        now,
        settings.stale_after_minutes,
    )
    return FleetSummary(
        current_power_kw=queries.get_current_power_kw(
            con,
            level=Level.FLEET,
            entity_id=None,
            now=now,
            stale_after_minutes=settings.stale_after_minutes,
        ),
        total_energy_mwh=queries.get_total_energy_mwh(con, level=Level.FLEET, entity_id=None),
        farm_count=len(farms),
        turbine_count=len(turbines),
        status_counts=health.status_counts(list(results.values())),
        now_utc=now,
    )


def build_farm_summary(
    con: duckdb.DuckDBPyConnection, settings: Settings, now: datetime, farm_id: str
) -> FarmSummary:
    """Assemble one farm's dashboard figures.

    A farm with zero turbines (8 of 10 seed farms, `PROJECT_SPEC.md` §6.3) still produces a
    valid summary: `turbine_count=0`, an all-zero `status_counts`, and `health_score=None`.

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings (staleness threshold).
        now: The resolved "now" (`clock.get_now`).
        farm_id: The farm to summarize.

    Returns:
        A populated `FarmSummary`.

    Raises:
        DataLoadError: `farm_id` does not exist in `farms`.
    """
    farm = _find_farm(queries.get_farms(con), farm_id)
    turbines = queries.get_turbines(con, farm_id=farm_id)
    latest_records = queries.get_latest_records(con, farm_id=farm_id)
    results = list(
        health.classify_many(
            latest_records,
            [turbine.turbine_id for turbine in turbines],
            now,
            settings.stale_after_minutes,
        ).values()
    )
    local_time, tz_label = geo.local_time(now, farm.latitude, farm.longitude)
    return FarmSummary(
        farm=farm,
        local_time=local_time,
        tz_label=tz_label,
        current_power_kw=queries.get_current_power_kw(
            con,
            level=Level.FARM,
            entity_id=farm_id,
            now=now,
            stale_after_minutes=settings.stale_after_minutes,
        ),
        total_energy_mwh=queries.get_total_energy_mwh(con, level=Level.FARM, entity_id=farm_id),
        turbine_count=len(turbines),
        status_counts=health.status_counts(results),
        health_score=health.farm_health_score(results),
        alert_reason=health.farm_alert(results),
    )


def build_turbine_summary(
    con: duckdb.DuckDBPyConnection, settings: Settings, now: datetime, turbine_id: str
) -> TurbineSummary:
    """Assemble one turbine's dashboard figures.

    A turbine with no telemetry at all (e.g. `TURB999` in the test fixtures) still produces a
    valid summary with `record=None` and `health.status == HealthStatus.ERROR`
    (`PROJECT_SPEC.md` §11) — only an unregistered `turbine_id` raises.

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings (staleness threshold).
        now: The resolved "now" (`clock.get_now`).
        turbine_id: The turbine to summarize.

    Returns:
        A populated `TurbineSummary`.

    Raises:
        DataLoadError: `turbine_id` does not exist in `turbines`.
    """
    turbine = _find_turbine(queries.get_turbines(con), turbine_id)
    farm = _find_farm(queries.get_farms(con), turbine.farm_id)
    record = queries.get_latest_record_for_turbine(con, turbine_id)
    local_time, tz_label = geo.local_time(now, farm.latitude, farm.longitude)
    return TurbineSummary(
        turbine=turbine,
        farm=farm,
        local_time=local_time,
        tz_label=tz_label,
        record=record,
        health=health.classify(record, now, settings.stale_after_minutes),
    )


def build_farm_map_rows(
    con: duckdb.DuckDBPyConnection, settings: Settings, now: datetime
) -> list[FarmMapRow]:
    """Assemble every farm's fleet-map marker data in a fixed, small number of queries.

    Issues exactly one `get_farms`, one `get_turbine_counts_by_farm`, and one
    `get_latest_records` call for the *entire* fleet, then groups and classifies in Python —
    never a query per farm (`PROJECT_SPEC.md` §12; `IMPLEMENTATION_PLAN.md` Phase 8). A
    turbine with no row in `latest_telemetry` is represented by `health.classify(None, ...)`
    without a per-turbine lookup: only its *count* (from `get_turbine_counts_by_farm`) is
    needed to make up the difference against the farm's records actually present.

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings (staleness threshold).
        now: The resolved "now" (`clock.get_now`).

    Returns:
        One `FarmMapRow` per farm, including farms with zero turbines
        (`health_score=None`, `alert_reason=None`).
    """
    farms = queries.get_farms(con)
    turbine_counts = queries.get_turbine_counts_by_farm(con)
    latest_records = queries.get_latest_records(con)

    records_by_farm: dict[str, list[TelemetryRecord]] = {}
    for record in latest_records:
        records_by_farm.setdefault(record.farm_id, []).append(record)

    rows: list[FarmMapRow] = []
    for farm in farms:
        turbine_count = turbine_counts.get(farm.farm_id, 0)
        farm_records = records_by_farm.get(farm.farm_id, [])
        missing = turbine_count - len(farm_records)
        results = [
            health.classify(record, now, settings.stale_after_minutes) for record in farm_records
        ] + [health.classify(None, now, settings.stale_after_minutes) for _ in range(missing)]
        rows.append(
            FarmMapRow(
                farm=farm,
                turbine_count=turbine_count,
                health_score=health.farm_health_score(results),
                alert_reason=health.farm_alert(results),
            )
        )
    return rows


def build_turbine_map_rows(
    con: duckdb.DuckDBPyConnection, settings: Settings, now: datetime, farm_id: str
) -> list[tuple[Turbine, HealthResult]]:
    """Assemble one farm's turbine-layer marker data.

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings (staleness threshold).
        now: The resolved "now" (`clock.get_now`).
        farm_id: The farm whose turbines to classify.

    Returns:
        One `(Turbine, HealthResult)` pair per turbine belonging to `farm_id`, in
        `turbine_id` order. A turbine with no telemetry classifies as `HealthStatus.ERROR`.
    """
    turbines = queries.get_turbines(con, farm_id=farm_id)
    latest_records = queries.get_latest_records(con, farm_id=farm_id)
    results = health.classify_many(
        latest_records,
        [turbine.turbine_id for turbine in turbines],
        now,
        settings.stale_after_minutes,
    )
    return [(turbine, results[turbine.turbine_id]) for turbine in turbines]


# --------------------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------------------


def _find_farm(farms: list[Farm], farm_id: str) -> Farm:
    """Return the farm matching `farm_id`.

    Raises:
        DataLoadError: No farm has this id.
    """
    for farm in farms:
        if farm.farm_id == farm_id:
            return farm
    raise DataLoadError(f"Unknown farm_id: {farm_id!r}")


def _find_turbine(turbines: list[Turbine], turbine_id: str) -> Turbine:
    """Return the turbine matching `turbine_id`.

    Raises:
        DataLoadError: No turbine has this id.
    """
    for turbine in turbines:
        if turbine.turbine_id == turbine_id:
            return turbine
    raise DataLoadError(f"Unknown turbine_id: {turbine_id!r}")
