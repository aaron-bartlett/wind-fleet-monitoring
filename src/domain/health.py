"""Turbine health classification: a telemetry record becomes a `HealthResult`.

Domain layer (`CLAUDE.md` §4.1): pure functions, no I/O, no Streamlit. This is the project's
single source of truth for turbine status — the map dot color, farm alert badge, and turbine
dashboard breach list all derive from `classify`'s output. `PROJECT_SPEC.md` §6.2 defines the
rules; `config.py` holds every numeric threshold this module reads.
"""

import math
from collections.abc import Sequence
from datetime import datetime

import config
from src.domain import clock
from src.domain.models import Breach, HealthResult, HealthStatus, Severity, TelemetryRecord

# --------------------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------------------


def classify(
    record: TelemetryRecord | None, now: datetime, stale_after_minutes: int
) -> HealthResult:
    """Classify one turbine's latest telemetry record.

    ERROR checks run first and short-circuit: a missing, stale, or structurally invalid
    record is classified without evaluating a single breach rule, so its `minor`/`major`
    tuples are always empty. Only once a record passes all three ERROR checks are the five
    metrics' breach rules evaluated (`PROJECT_SPEC.md` §6.2).

    Args:
        record: The turbine's latest telemetry record, or `None` if it has none at all.
        now: The reference "now" (tz-aware UTC), from `clock.get_now`.
        stale_after_minutes: Staleness threshold in minutes.

    Returns:
        A `HealthResult`.
    """
    if record is None:
        return HealthResult(
            status=HealthStatus.ERROR, minor=(), major=(), errors=("No telemetry received",)
        )

    if clock.is_stale(record.timestamp, now, stale_after_minutes):
        age_minutes = (now - record.timestamp).total_seconds() / 60
        reason = (
            f"Telemetry is {age_minutes:.1f} min old, exceeding the "
            f"{stale_after_minutes} min staleness threshold"
        )
        return HealthResult(status=HealthStatus.ERROR, minor=(), major=(), errors=(reason,))

    invalid_reasons = _invalid_metric_reasons(record)
    if invalid_reasons:
        return HealthResult(
            status=HealthStatus.ERROR, minor=(), major=(), errors=tuple(invalid_reasons)
        )

    minor, major = _collect_breaches(record)
    status = _status_from_breaches(minor, major)
    return HealthResult(status=status, minor=tuple(minor), major=tuple(major), errors=())


def classify_many(
    records: Sequence[TelemetryRecord],
    turbine_ids: Sequence[str],
    now: datetime,
    stale_after_minutes: int,
) -> dict[str, HealthResult]:
    """Classify the latest record for each of a set of turbine ids.

    Args:
        records: Latest telemetry records, in any order; at most one per `turbine_id` is used.
        turbine_ids: Every turbine id to classify. An id with no matching record in `records`
            is classified as `ERROR` ("No telemetry received") without any special-casing.
        now: The reference "now" (tz-aware UTC).
        stale_after_minutes: Staleness threshold in minutes.

    Returns:
        A dict from `turbine_id` to its `HealthResult`, one entry per id in `turbine_ids`.
    """
    by_turbine = {r.turbine_id: r for r in records}
    return {
        turbine_id: classify(by_turbine.get(turbine_id), now, stale_after_minutes)
        for turbine_id in turbine_ids
    }


# --------------------------------------------------------------------------------------
# ERROR checks (Step 1)
# --------------------------------------------------------------------------------------


def _invalid_metric_reasons(record: TelemetryRecord) -> list[str]:
    """Return one human-readable reason per metric that is missing, NaN, or out of range."""
    reasons: list[str] = []
    for metric in config.METRICS:
        value = record.get(metric)
        threshold = config.THRESHOLDS[metric]
        if value is None:
            reasons.append(f"{metric} is missing (NULL)")
        elif math.isnan(value):
            reasons.append(f"{metric} is NaN")
        elif value < threshold.physical_min or value > threshold.physical_max:
            reasons.append(
                f"{metric} = {value} is outside the physically possible range "
                f"[{threshold.physical_min}, {threshold.physical_max}]"
            )
    return reasons


# --------------------------------------------------------------------------------------
# Breach collection (Step 2) — each metric contributes at most one Breach; major supersedes
# minor for the same metric, enforced by writing every rule as major-check-then-minor-check.
# --------------------------------------------------------------------------------------


def _collect_breaches(record: TelemetryRecord) -> tuple[list[Breach], list[Breach]]:
    """Evaluate all five metrics' breach rules against an already-validated record."""
    power_kw = record.power_output_kw
    wind_ms = record.wind_speed_ms
    rotor_rpm = record.rotor_rpm
    pitch_deg = record.blade_pitch_deg
    gearbox_c = record.gearbox_temp_c
    # Guaranteed non-None: classify() only reaches here after _invalid_metric_reasons found none.
    assert power_kw is not None
    assert wind_ms is not None
    assert rotor_rpm is not None
    assert pitch_deg is not None
    assert gearbox_c is not None

    minor: list[Breach] = []
    major: list[Breach] = []
    for breach in (
        _gearbox_breach(gearbox_c),
        _rotor_breach(rotor_rpm, wind_ms),
        _pitch_breach(pitch_deg, power_kw),
        _wind_breach(wind_ms),
        _power_breach(power_kw, wind_ms),
    ):
        if breach is None:
            continue
        (major if breach.severity is Severity.MAJOR else minor).append(breach)
    return minor, major


def _gearbox_breach(gearbox_c: float) -> Breach | None:
    t = config.THRESHOLDS["gearbox_temp_c"]
    assert t.major_max is not None
    assert t.minor_max is not None
    if gearbox_c > t.major_max:
        return Breach(
            metric="gearbox_temp_c",
            value=gearbox_c,
            threshold=t.major_max,
            severity=Severity.MAJOR,
            message=f"Gearbox temp {gearbox_c:.1f} °C exceeds major limit {t.major_max:g} °C",
        )
    if gearbox_c > t.minor_max:
        return Breach(
            metric="gearbox_temp_c",
            value=gearbox_c,
            threshold=t.minor_max,
            severity=Severity.MINOR,
            message=f"Gearbox temp {gearbox_c:.1f} °C exceeds minor limit {t.minor_max:g} °C",
        )
    return None


def _rotor_breach(rotor_rpm: float, wind_ms: float) -> Breach | None:
    t = config.THRESHOLDS["rotor_rpm"]
    assert t.major_max is not None
    assert t.minor_max is not None
    stall_lo, stall_hi = config.ROTOR_STALL_WIND_RANGE
    if rotor_rpm < config.ROTOR_STALL_RPM and stall_lo <= wind_ms <= stall_hi:
        return Breach(
            metric="rotor_rpm",
            value=rotor_rpm,
            threshold=config.ROTOR_STALL_RPM,
            severity=Severity.MAJOR,
            message=(
                f"Rotor speed {rotor_rpm:.2f} RPM indicates a stalled rotor "
                f"(< {config.ROTOR_STALL_RPM:g} RPM) at wind speed {wind_ms:.1f} m/s"
            ),
        )
    if rotor_rpm > t.major_max:
        return Breach(
            metric="rotor_rpm",
            value=rotor_rpm,
            threshold=t.major_max,
            severity=Severity.MAJOR,
            message=f"Rotor speed {rotor_rpm:.1f} RPM exceeds major limit {t.major_max:g} RPM",
        )
    if rotor_rpm > t.minor_max:
        return Breach(
            metric="rotor_rpm",
            value=rotor_rpm,
            threshold=t.minor_max,
            severity=Severity.MINOR,
            message=f"Rotor speed {rotor_rpm:.1f} RPM exceeds minor limit {t.minor_max:g} RPM",
        )
    return None


def _pitch_breach(pitch_deg: float, power_kw: float) -> Breach | None:
    t = config.THRESHOLDS["blade_pitch_deg"]
    assert t.major_max is not None
    assert t.minor_max is not None
    if pitch_deg > t.major_max:
        return Breach(
            metric="blade_pitch_deg",
            value=pitch_deg,
            threshold=t.major_max,
            severity=Severity.MAJOR,
            message=f"Blade pitch {pitch_deg:.1f}° exceeds major limit {t.major_max:g}°",
        )
    if pitch_deg > t.minor_max and power_kw > config.PITCH_CONDITIONAL_POWER_KW:
        return Breach(
            metric="blade_pitch_deg",
            value=pitch_deg,
            threshold=t.minor_max,
            severity=Severity.MINOR,
            message=(
                f"Blade pitch {pitch_deg:.1f}° exceeds minor limit {t.minor_max:g}° "
                f"at power output {power_kw:.0f} kW"
            ),
        )
    return None


def _wind_breach(wind_ms: float) -> Breach | None:
    t = config.THRESHOLDS["wind_speed_ms"]
    assert t.minor_max is not None
    if wind_ms > t.minor_max:
        return Breach(
            metric="wind_speed_ms",
            value=wind_ms,
            threshold=t.minor_max,
            severity=Severity.MINOR,
            message=f"Wind speed {wind_ms:.1f} m/s exceeds cut-out limit {t.minor_max:g} m/s",
        )
    return None


def _power_breach(power_kw: float, wind_ms: float) -> Breach | None:
    zero_lo, zero_hi = config.POWER_ZERO_WIND_RANGE
    if power_kw <= 0 and zero_lo <= wind_ms <= zero_hi:
        return Breach(
            metric="power_output_kw",
            value=power_kw,
            threshold=0.0,
            severity=Severity.MAJOR,
            message=f"Power output {power_kw:.1f} kW is non-positive at wind speed {wind_ms:.1f} m/s",
        )
    check_lo, check_hi = config.POWER_CHECK_WIND_RANGE
    if check_lo <= wind_ms <= check_hi:
        expected_kw = config.POWER_CURVE_EXPECTED_KW(wind_ms)
        floor_kw = config.POWER_UNDERPERFORM_FRACTION * expected_kw
        if power_kw < floor_kw:
            return Breach(
                metric="power_output_kw",
                value=power_kw,
                threshold=floor_kw,
                severity=Severity.MINOR,
                message=(
                    f"Power output {power_kw:.1f} kW is below 40% of expected {expected_kw:.0f} kW "
                    f"at wind speed {wind_ms:.1f} m/s"
                ),
            )
    return None


def _status_from_breaches(minor: Sequence[Breach], major: Sequence[Breach]) -> HealthStatus:
    # SPEC-GAP: "two minor breaches" is unspecified between Warning and Critical; resolved as
    # Warning = 1-2 minor, Critical = >=3 minor or >=1 major (see PROJECT_SPEC.md §16).
    if major or len(minor) >= config.MINOR_TO_CRITICAL:
        return HealthStatus.CRITICAL
    if minor:
        return HealthStatus.WARNING
    return HealthStatus.HEALTHY


# --------------------------------------------------------------------------------------
# Farm/fleet roll-ups
# --------------------------------------------------------------------------------------


def farm_health_score(results: Sequence[HealthResult]) -> float | None:
    """Weighted-mean health score over a farm's non-ERROR turbines (`PROJECT_SPEC.md` §6.3).

    Args:
        results: `HealthResult`s for a farm's turbines.

    Returns:
        A score in `[0, 1]` (`config.FARM_SCORE_WEIGHTS`), or `None` when the farm has no
        turbines or every turbine is `ERROR` (`ERROR` is excluded from both sum and count).
    """
    weights = [
        config.FARM_SCORE_WEIGHTS[result.status.name]
        for result in results
        if result.status is not HealthStatus.ERROR
    ]
    if not weights:
        return None
    return sum(weights) / len(weights)


def farm_alert(results: Sequence[HealthResult]) -> str | None:
    """Determine whether a farm's alert badge fires, and why (`PROJECT_SPEC.md` §6.3).

    Fires when any turbine is `CRITICAL`, or when the `ERROR` fraction exceeds
    `config.FARM_ALERT_ERROR_FRACTION`. Both conditions are independently configurable and,
    if both fire, both reasons are reported.

    Args:
        results: `HealthResult`s for a farm's turbines.

    Returns:
        A human-readable reason (reasons joined by `"; "` if more than one fired), or `None`.
    """
    reasons: list[str] = []

    critical_count = sum(1 for result in results if result.status is HealthStatus.CRITICAL)
    if config.FARM_ALERT_ON_ANY_CRITICAL and critical_count > 0:
        plural = "s" if critical_count != 1 else ""
        reasons.append(f"{critical_count} turbine{plural} in Critical state")

    if results:
        error_count = sum(1 for result in results if result.status is HealthStatus.ERROR)
        error_fraction = error_count / len(results)
        if error_fraction > config.FARM_ALERT_ERROR_FRACTION:
            reasons.append(
                f"{error_count} of {len(results)} turbines reporting Error ({error_fraction:.0%})"
            )

    return "; ".join(reasons) if reasons else None


def status_counts(results: Sequence[HealthResult]) -> dict[HealthStatus, int]:
    """Tally results by status.

    Args:
        results: `HealthResult`s to count.

    Returns:
        A dict with all four `HealthStatus` members as keys, including those with count `0`.
    """
    counts: dict[HealthStatus, int] = dict.fromkeys(HealthStatus, 0)
    for result in results:
        counts[result.status] += 1
    return counts
