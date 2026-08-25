"""Tests for `src/domain/models.py` — pure dataclasses and their small derivations."""

from datetime import UTC, datetime

import pytest

import config
from src.domain.models import (
    Bounds,
    Breach,
    HealthResult,
    HealthStatus,
    Severity,
    TelemetryRecord,
    compass_point,
)


def test_bounds_expanded_widens_around_midpoint() -> None:
    box = Bounds(lat_min=40.0, lat_max=42.0, lon_min=-97.0, lon_max=-95.0)
    expanded = box.expanded(1.10)
    assert expanded.lat_min == pytest.approx(39.9)
    assert expanded.lat_max == pytest.approx(42.1)
    assert expanded.lon_min == pytest.approx(-97.1)
    assert expanded.lon_max == pytest.approx(-94.9)


def test_bounds_expanded_zero_span_falls_back_to_fixed_pad() -> None:
    point = Bounds(lat_min=41.25, lat_max=41.25, lon_min=-96.53, lon_max=-96.53)
    expanded = point.expanded(1.10)
    assert expanded.lat_min == pytest.approx(41.25 - config.ZERO_SPAN_PAD_DEG)
    assert expanded.lat_max == pytest.approx(41.25 + config.ZERO_SPAN_PAD_DEG)
    assert expanded.lon_min == pytest.approx(-96.53 - config.ZERO_SPAN_PAD_DEG)
    assert expanded.lon_max == pytest.approx(-96.53 + config.ZERO_SPAN_PAD_DEG)


def test_bounds_as_folium_ordering() -> None:
    box = Bounds(lat_min=40.0, lat_max=42.0, lon_min=-97.0, lon_max=-95.0)
    assert box.as_folium() == [[40.0, -97.0], [42.0, -95.0]]


def test_telemetry_record_lag_minutes() -> None:
    record = TelemetryRecord(
        turbine_id="TURB001",
        farm_id="FARM01",
        timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 0, 7, tzinfo=UTC),
        power_output_kw=2331.2,
        wind_speed_ms=8.0,
        rotor_rpm=14.0,
        blade_pitch_deg=3.6,
        gearbox_temp_c=81.6,
    )
    assert record.lag_minutes == pytest.approx(7.0)


def test_telemetry_record_get_returns_named_metric() -> None:
    record = TelemetryRecord(
        turbine_id="TURB001",
        farm_id="FARM01",
        timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
        power_output_kw=2331.2,
        wind_speed_ms=8.0,
        rotor_rpm=14.0,
        blade_pitch_deg=3.6,
        gearbox_temp_c=81.6,
    )
    assert record.get("power_output_kw") == 2331.2
    assert record.get("gearbox_temp_c") == 81.6


def test_telemetry_record_get_unknown_metric_raises_key_error() -> None:
    record = TelemetryRecord(
        turbine_id="TURB001",
        farm_id="FARM01",
        timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
        power_output_kw=2331.2,
        wind_speed_ms=8.0,
        rotor_rpm=14.0,
        blade_pitch_deg=3.6,
        gearbox_temp_c=81.6,
    )
    with pytest.raises(KeyError):
        record.get("air_temp_c")


@pytest.mark.parametrize(
    ("degrees", "expected"),
    [
        (0.0, "N"),
        (359.0, "N"),
        (337.5, "NNW"),
        (90.0, "E"),
    ],
)
def test_compass_point(degrees: float, expected: str) -> None:
    assert compass_point(degrees) == expected


def test_health_result_color_matches_config() -> None:
    result = HealthResult(
        status=HealthStatus.CRITICAL,
        minor=(),
        major=(
            Breach(
                metric="gearbox_temp_c",
                value=126.5,
                threshold=110.0,
                severity=Severity.MAJOR,
                message="Gearbox temp 126.5 C exceeds major limit 110 C.",
            ),
        ),
        errors=(),
    )
    assert result.color == config.HEALTH_COLORS["Critical"]
