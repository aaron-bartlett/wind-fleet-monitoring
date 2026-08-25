"""Table-driven tests for `src/domain/health.py` (`CLAUDE.md` §5.7).

Every threshold in `PROJECT_SPEC.md` §6.2 is exercised at, just below, and just above its
boundary. Uses only synthetic `TelemetryRecord`s — no DuckDB, no fixtures on disk.
"""

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.domain import health
from src.domain.models import HealthStatus, Severity, TelemetryRecord

_NOW = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
_STALE_AFTER_MINUTES = 15

# A clean, mid-range record: nothing here is close to any threshold, so overriding a single
# field in a test isolates exactly the rule under test.
_BASE_RECORD = TelemetryRecord(
    turbine_id="TURB001",
    farm_id="FARM01",
    timestamp=_NOW,
    received_at=_NOW,
    power_output_kw=2000.0,
    wind_speed_ms=10.0,
    rotor_rpm=15.0,
    blade_pitch_deg=10.0,
    gearbox_temp_c=70.0,
)


def _record(**overrides: object) -> TelemetryRecord:
    return replace(_BASE_RECORD, **overrides)  # type: ignore[arg-type]


def _classify(record: TelemetryRecord | None) -> health.HealthResult:
    return health.classify(record, _NOW, _STALE_AFTER_MINUTES)


def _metrics(breaches: tuple) -> set[str]:  # type: ignore[type-arg]
    return {b.metric for b in breaches}


# --------------------------------------------------------------------------------------
# Clean record
# --------------------------------------------------------------------------------------


def test_clean_record_is_healthy() -> None:
    result = _classify(_record())
    assert result.status == HealthStatus.HEALTHY
    assert result.minor == ()
    assert result.major == ()
    assert result.errors == ()


# --------------------------------------------------------------------------------------
# gearbox_temp_c: minor 95, major 110, physical [-40, 200]
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gearbox_temp_c", "expected_status", "expected_minor", "expected_major"),
    [
        (94.9, HealthStatus.HEALTHY, False, False),
        (95.0, HealthStatus.HEALTHY, False, False),  # strict `>`
        (95.1, HealthStatus.WARNING, True, False),
        (109.9, HealthStatus.WARNING, True, False),
        (110.0, HealthStatus.WARNING, True, False),  # strict `>`, falls to minor
        (110.1, HealthStatus.CRITICAL, False, True),
        (-40.0, HealthStatus.HEALTHY, False, False),  # physical bound is inclusive
        (200.0, HealthStatus.CRITICAL, False, True),  # in physical range, still major
    ],
)
def test_gearbox_temp_boundaries(
    gearbox_temp_c: float, expected_status: HealthStatus, expected_minor: bool, expected_major: bool
) -> None:
    result = _classify(_record(gearbox_temp_c=gearbox_temp_c))
    assert result.status == expected_status
    assert ("gearbox_temp_c" in _metrics(result.minor)) is expected_minor
    assert ("gearbox_temp_c" in _metrics(result.major)) is expected_major


@pytest.mark.parametrize("gearbox_temp_c", [-40.1, 200.1])
def test_gearbox_temp_physical_range_is_error(gearbox_temp_c: float) -> None:
    result = _classify(_record(gearbox_temp_c=gearbox_temp_c))
    assert result.status == HealthStatus.ERROR
    assert result.minor == () and result.major == ()
    assert any("gearbox_temp_c" in reason for reason in result.errors)


# --------------------------------------------------------------------------------------
# rotor_rpm: minor 18.5, major 22.0, stall major (<0.5 while 4<=wind<=25), physical [0, 40]
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rotor_rpm", "expected_status", "expected_minor", "expected_major"),
    [
        (18.4, HealthStatus.HEALTHY, False, False),
        (18.5, HealthStatus.HEALTHY, False, False),
        (18.6, HealthStatus.WARNING, True, False),
        (21.9, HealthStatus.WARNING, True, False),
        (22.0, HealthStatus.WARNING, True, False),  # strict `>`, falls to minor
        (22.1, HealthStatus.CRITICAL, False, True),  # major supersedes minor for same metric
    ],
)
def test_rotor_rpm_high_side_boundaries(
    rotor_rpm: float, expected_status: HealthStatus, expected_minor: bool, expected_major: bool
) -> None:
    result = _classify(_record(rotor_rpm=rotor_rpm))
    assert result.status == expected_status
    assert ("rotor_rpm" in _metrics(result.minor)) is expected_minor
    assert ("rotor_rpm" in _metrics(result.major)) is expected_major
    if expected_major:
        assert "rotor_rpm" not in _metrics(result.minor)


@pytest.mark.parametrize(
    ("rotor_rpm", "wind_speed_ms", "expected_status", "expect_stall"),
    [
        (0.51, 10.0, HealthStatus.HEALTHY, False),
        (0.5, 10.0, HealthStatus.HEALTHY, False),  # strict `<`
        (0.49, 10.0, HealthStatus.CRITICAL, True),
        (0.2, 3.0, HealthStatus.HEALTHY, False),  # low RPM but wind outside the stall window
        (0.2, 10.0, HealthStatus.CRITICAL, True),  # required case: PROJECT_SPEC.md §6.2
    ],
)
def test_rotor_stall_gate(
    rotor_rpm: float, wind_speed_ms: float, expected_status: HealthStatus, expect_stall: bool
) -> None:
    result = _classify(_record(rotor_rpm=rotor_rpm, wind_speed_ms=wind_speed_ms))
    assert result.status == expected_status
    assert ("rotor_rpm" in _metrics(result.major)) is expect_stall


def test_rotor_stall_message_names_wind_speed() -> None:
    result = _classify(_record(rotor_rpm=0.2, wind_speed_ms=10.0))
    (breach,) = result.major
    assert "stalled" in breach.message
    assert "10.0" in breach.message


# --------------------------------------------------------------------------------------
# blade_pitch_deg: minor 25 (gated by power_output_kw > 100), major 40 (ungated)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("blade_pitch_deg", "expected_status", "expected_minor", "expected_major"),
    [
        (24.9, HealthStatus.HEALTHY, False, False),
        (25.0, HealthStatus.HEALTHY, False, False),
        (25.1, HealthStatus.WARNING, True, False),
        (39.9, HealthStatus.WARNING, True, False),
        (40.0, HealthStatus.WARNING, True, False),  # strict `>`, falls to minor
        (40.1, HealthStatus.CRITICAL, False, True),
        (44.0, HealthStatus.CRITICAL, False, True),  # required case: PROJECT_SPEC.md §6.2
    ],
)
def test_blade_pitch_boundaries_gate_open(
    blade_pitch_deg: float,
    expected_status: HealthStatus,
    expected_minor: bool,
    expected_major: bool,
) -> None:
    result = _classify(_record(blade_pitch_deg=blade_pitch_deg, power_output_kw=2000.0))
    assert result.status == expected_status
    assert ("blade_pitch_deg" in _metrics(result.minor)) is expected_minor
    assert ("blade_pitch_deg" in _metrics(result.major)) is expected_major
    if expected_major:
        assert "blade_pitch_deg" not in _metrics(result.minor)


@pytest.mark.parametrize(
    ("power_output_kw", "expected_status"),
    [
        (99.9, HealthStatus.HEALTHY),  # gate closed
        (100.0, HealthStatus.HEALTHY),  # gate boundary, strict `>`, still closed
        (100.1, HealthStatus.WARNING),  # gate open
        (50.0, HealthStatus.HEALTHY),  # required case: PROJECT_SPEC.md §6.2
    ],
)
def test_blade_pitch_power_gate(power_output_kw: float, expected_status: HealthStatus) -> None:
    # wind_speed_ms=20 sits outside both power conditional windows ([4,15] and, since these
    # power values are all positive, the [4,25] zero-rule doesn't apply either) so only the
    # pitch gate under test can produce a breach.
    result = _classify(
        _record(blade_pitch_deg=30.0, power_output_kw=power_output_kw, wind_speed_ms=20.0)
    )
    assert result.status == expected_status


def test_blade_pitch_major_has_no_power_gate() -> None:
    # A high enough pitch is major regardless of power output — only the minor rule is gated.
    result = _classify(_record(blade_pitch_deg=44.0, power_output_kw=0.0, wind_speed_ms=1.0))
    assert result.status == HealthStatus.CRITICAL
    assert "blade_pitch_deg" in _metrics(result.major)


# --------------------------------------------------------------------------------------
# wind_speed_ms: minor 25 (cut-out), no major rule, physical [0, 60]
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wind_speed_ms", "expected_status", "expected_minor"),
    [
        (24.9, HealthStatus.HEALTHY, False),
        (25.0, HealthStatus.HEALTHY, False),
        (25.1, HealthStatus.WARNING, True),
        (0.0, HealthStatus.HEALTHY, False),
        (60.0, HealthStatus.WARNING, True),  # physical bound, but still > 25 cut-out
    ],
)
def test_wind_speed_boundaries(
    wind_speed_ms: float, expected_status: HealthStatus, expected_minor: bool
) -> None:
    result = _classify(_record(wind_speed_ms=wind_speed_ms))
    assert result.status == expected_status
    assert ("wind_speed_ms" in _metrics(result.minor)) is expected_minor
    assert result.major == ()


@pytest.mark.parametrize("wind_speed_ms", [-0.1, 60.1])
def test_wind_speed_physical_range_is_error(wind_speed_ms: float) -> None:
    result = _classify(_record(wind_speed_ms=wind_speed_ms))
    assert result.status == HealthStatus.ERROR
    assert any("wind_speed_ms" in reason for reason in result.errors)


# --------------------------------------------------------------------------------------
# power_output_kw: major <=0 while 4<=wind<=25; minor <40% of curve while 4<=wind<=15;
# physical [-50, 5000]
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("power_output_kw", "wind_speed_ms", "expected_status", "expect_major"),
    [
        (0.1, 20.0, HealthStatus.HEALTHY, False),  # in zero-rule window, not in check window
        (0.0, 20.0, HealthStatus.CRITICAL, True),  # `<=0` boundary
        (-0.1, 20.0, HealthStatus.CRITICAL, True),
        (0.0, 10.0, HealthStatus.CRITICAL, True),  # required case: PROJECT_SPEC.md §6.2
        (-50.0, 20.0, HealthStatus.CRITICAL, True),  # physical low bound, still `<=0`
    ],
)
def test_power_zero_rule(
    power_output_kw: float, wind_speed_ms: float, expected_status: HealthStatus, expect_major: bool
) -> None:
    result = _classify(_record(power_output_kw=power_output_kw, wind_speed_ms=wind_speed_ms))
    assert result.status == expected_status
    assert ("power_output_kw" in _metrics(result.major)) is expect_major


@pytest.mark.parametrize(
    ("power_output_kw", "expected_status"),
    [
        (700.0, HealthStatus.HEALTHY),  # wind=7.5 -> expectation 1750, 40% = 700, strict `<`
        (700.1, HealthStatus.HEALTHY),
        (699.9, HealthStatus.WARNING),
        (400.0, HealthStatus.WARNING),  # required case at wind=10: expectation ~2722, 40% ~1089
    ],
)
def test_power_underperform_rule(power_output_kw: float, expected_status: HealthStatus) -> None:
    wind_speed_ms = 7.5 if power_output_kw != 400.0 else 10.0
    result = _classify(_record(power_output_kw=power_output_kw, wind_speed_ms=wind_speed_ms))
    assert result.status == expected_status
    if expected_status == HealthStatus.WARNING:
        assert "power_output_kw" in _metrics(result.minor)


@pytest.mark.parametrize("power_output_kw", [5000.0])
def test_power_physical_high_boundary_healthy(power_output_kw: float) -> None:
    result = _classify(_record(power_output_kw=power_output_kw, wind_speed_ms=1.0))
    assert result.status == HealthStatus.HEALTHY


@pytest.mark.parametrize("power_output_kw", [-50.1, 5000.1])
def test_power_physical_range_is_error(power_output_kw: float) -> None:
    result = _classify(_record(power_output_kw=power_output_kw, wind_speed_ms=1.0))
    assert result.status == HealthStatus.ERROR
    assert any("power_output_kw" in reason for reason in result.errors)


# --------------------------------------------------------------------------------------
# Minor-count escalation: 1-2 minor -> WARNING, >=3 minor or any major -> CRITICAL
# --------------------------------------------------------------------------------------


def test_exactly_two_minor_breaches_is_warning() -> None:
    result = _classify(
        _record(wind_speed_ms=26.0, gearbox_temp_c=100.0, blade_pitch_deg=10.0, power_output_kw=1.0)
    )
    assert result.status == HealthStatus.WARNING
    assert len(result.minor) == 2
    assert result.major == ()
    assert _metrics(result.minor) == {"wind_speed_ms", "gearbox_temp_c"}


def test_exactly_three_minor_breaches_is_critical() -> None:
    result = _classify(
        _record(
            wind_speed_ms=26.0,
            gearbox_temp_c=100.0,
            blade_pitch_deg=30.0,
            power_output_kw=2000.0,
        )
    )
    assert result.status == HealthStatus.CRITICAL
    assert len(result.minor) == 3
    assert result.major == ()
    assert _metrics(result.minor) == {"wind_speed_ms", "gearbox_temp_c", "blade_pitch_deg"}


def test_one_major_is_critical_even_with_no_minors() -> None:
    result = _classify(_record(gearbox_temp_c=111.0))
    assert result.status == HealthStatus.CRITICAL
    assert len(result.major) == 1
    assert result.minor == ()


# --------------------------------------------------------------------------------------
# ERROR: missing telemetry, invalid metrics, staleness
# --------------------------------------------------------------------------------------


def test_record_none_is_error() -> None:
    result = _classify(None)
    assert result.status == HealthStatus.ERROR
    assert result.errors == ("No telemetry received",)
    assert result.minor == () and result.major == ()


def test_null_metric_is_error() -> None:
    result = _classify(_record(wind_speed_ms=None))
    assert result.status == HealthStatus.ERROR
    assert any("wind_speed_ms" in reason for reason in result.errors)
    assert result.minor == () and result.major == ()


def test_nan_metric_is_error() -> None:
    result = _classify(_record(gearbox_temp_c=math.nan))
    assert result.status == HealthStatus.ERROR
    assert any("gearbox_temp_c" in reason for reason in result.errors)


def test_multiple_invalid_metrics_all_named() -> None:
    result = _classify(_record(wind_speed_ms=None, gearbox_temp_c=math.nan))
    assert result.status == HealthStatus.ERROR
    assert len(result.errors) == 2
    joined = " ".join(result.errors)
    assert "wind_speed_ms" in joined
    assert "gearbox_temp_c" in joined


def test_physically_impossible_value_is_error_not_critical() -> None:
    # A value beyond the physical range is ERROR even though it would also read as a major
    # breach numerically — Step 1 short-circuits before Step 2 ever runs.
    result = _classify(_record(gearbox_temp_c=250.0))
    assert result.status == HealthStatus.ERROR
    assert result.major == () and result.minor == ()


@pytest.mark.parametrize(
    ("age_minutes", "expected_status"),
    [
        (14, HealthStatus.HEALTHY),
        (15, HealthStatus.HEALTHY),  # matches clock.is_stale's own boundary test: not yet stale
        (16, HealthStatus.ERROR),
    ],
)
def test_staleness_boundary(age_minutes: int, expected_status: HealthStatus) -> None:
    record = _record(timestamp=_NOW - timedelta(minutes=age_minutes))
    result = _classify(record)
    assert result.status == expected_status
    if expected_status == HealthStatus.ERROR:
        assert any("stale" in reason.lower() or "min old" in reason for reason in result.errors)


# --------------------------------------------------------------------------------------
# classify_many
# --------------------------------------------------------------------------------------


def test_classify_many_marks_missing_turbine_as_error() -> None:
    present = _record(turbine_id="TURB001")
    results = health.classify_many(
        records=[present],
        turbine_ids=["TURB001", "TURB002"],
        now=_NOW,
        stale_after_minutes=_STALE_AFTER_MINUTES,
    )
    assert results["TURB001"].status == HealthStatus.HEALTHY
    assert results["TURB002"].status == HealthStatus.ERROR
    assert results["TURB002"].errors == ("No telemetry received",)


# --------------------------------------------------------------------------------------
# farm_health_score
# --------------------------------------------------------------------------------------


def _result(status: HealthStatus) -> health.HealthResult:
    return health.HealthResult(status=status, minor=(), major=(), errors=())


def test_farm_health_score_weighted_mean() -> None:
    results = [
        _result(HealthStatus.HEALTHY),
        _result(HealthStatus.WARNING),
        _result(HealthStatus.CRITICAL),
    ]
    score = health.farm_health_score(results)
    assert score == pytest.approx((1.0 + 0.6 + 0.0) / 3)


def test_farm_health_score_all_error_is_none() -> None:
    assert (
        health.farm_health_score([_result(HealthStatus.ERROR), _result(HealthStatus.ERROR)]) is None
    )


def test_farm_health_score_empty_is_none() -> None:
    assert health.farm_health_score([]) is None


def test_farm_health_score_excludes_error_from_denominator() -> None:
    results = [_result(HealthStatus.HEALTHY), _result(HealthStatus.ERROR)]
    assert health.farm_health_score(results) == pytest.approx(1.0)


# --------------------------------------------------------------------------------------
# farm_alert
# --------------------------------------------------------------------------------------


def test_farm_alert_fires_on_one_critical() -> None:
    results = [_result(HealthStatus.CRITICAL)] + [_result(HealthStatus.HEALTHY)] * 9
    reason = health.farm_alert(results)
    assert reason is not None
    assert "Critical" in reason


def test_farm_alert_does_not_fire_at_20_percent_error() -> None:
    results = [_result(HealthStatus.ERROR)] * 2 + [_result(HealthStatus.HEALTHY)] * 8
    assert health.farm_alert(results) is None


def test_farm_alert_fires_above_20_percent_error() -> None:
    results = [_result(HealthStatus.ERROR)] * 3 + [_result(HealthStatus.HEALTHY)] * 7
    reason = health.farm_alert(results)
    assert reason is not None
    assert "Error" in reason


def test_farm_alert_does_not_fire_at_19_percent_error_no_critical() -> None:
    results = [_result(HealthStatus.ERROR)] * 19 + [_result(HealthStatus.HEALTHY)] * 81
    assert health.farm_alert(results) is None


def test_farm_alert_joins_both_reasons() -> None:
    results = (
        [_result(HealthStatus.CRITICAL)]
        + [_result(HealthStatus.ERROR)] * 3
        + [_result(HealthStatus.HEALTHY)] * 6
    )
    reason = health.farm_alert(results)
    assert reason is not None
    assert "; " in reason
    assert "Critical" in reason
    assert "Error" in reason


def test_farm_alert_empty_is_none() -> None:
    assert health.farm_alert([]) is None


# --------------------------------------------------------------------------------------
# status_counts
# --------------------------------------------------------------------------------------


def test_status_counts_includes_zero_statuses() -> None:
    results = [_result(HealthStatus.HEALTHY), _result(HealthStatus.CRITICAL)]
    counts = health.status_counts(results)
    assert counts == {
        HealthStatus.HEALTHY: 1,
        HealthStatus.WARNING: 0,
        HealthStatus.CRITICAL: 1,
        HealthStatus.ERROR: 0,
    }


def test_status_counts_empty() -> None:
    counts = health.status_counts([])
    assert all(count == 0 for count in counts.values())
    assert set(counts) == set(HealthStatus)


# --------------------------------------------------------------------------------------
# Breach messages are complete, human-readable sentences
# --------------------------------------------------------------------------------------


def test_breach_message_names_metric_value_and_threshold() -> None:
    result = _classify(_record(gearbox_temp_c=126.5))
    (breach,) = result.major
    assert breach.metric == "gearbox_temp_c"
    assert breach.value == 126.5
    assert breach.threshold == 110.0
    assert breach.severity == Severity.MAJOR
    assert "126.5" in breach.message
    assert "110" in breach.message
