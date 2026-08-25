"""Tests for config.py: settings loading, threshold/label coverage, and the power curve."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

import config
from src.errors import ConfigError


def test_load_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("DUCKDB_PATH", raising=False)
    monkeypatch.delenv("SIM_NOW", raising=False)
    monkeypatch.delenv("STALE_AFTER_MINUTES", raising=False)

    settings = config.load_settings()

    assert settings.data_dir == Path("data")
    assert settings.duckdb_path == Path("data/fleet.duckdb")
    assert settings.sim_now is None
    assert settings.stale_after_minutes == 15


def test_load_settings_sim_now_parses_to_tz_aware_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIM_NOW", "2026-01-02T23:55:00Z")

    settings = config.load_settings()

    assert settings.sim_now == datetime(2026, 1, 2, 23, 55, tzinfo=UTC)
    assert settings.sim_now is not None
    assert settings.sim_now.tzinfo is not None


def test_load_settings_sim_now_garbage_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIM_NOW", "garbage")

    with pytest.raises(ConfigError, match="SIM_NOW"):
        config.load_settings()


def test_load_settings_stale_after_minutes_invalid_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STALE_AFTER_MINUTES", "not-a-number")

    with pytest.raises(ConfigError, match="STALE_AFTER_MINUTES"):
        config.load_settings()


def test_every_metric_has_threshold_and_label() -> None:
    for metric in config.METRICS:
        assert metric in config.THRESHOLDS, f"{metric} missing from THRESHOLDS"
        assert metric in config.METRIC_LABELS, f"{metric} missing from METRIC_LABELS"


@pytest.mark.parametrize(
    ("wind_speed_ms", "expected_kw"),
    [
        (2.0, 0.0),
        (7.5, 1750.0),
        (12.0, 3500.0),
        (20.0, 3500.0),
        (30.0, 0.0),
    ],
)
def test_power_curve_expected_kw(wind_speed_ms: float, expected_kw: float) -> None:
    assert config.POWER_CURVE_EXPECTED_KW(wind_speed_ms) == pytest.approx(expected_kw)
