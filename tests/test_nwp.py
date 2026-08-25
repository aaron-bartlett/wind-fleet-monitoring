"""Tests for src/domain/nwp.py: stub determinism/bounds, the HRRR skeleton, and provider selection."""

import math
from datetime import UTC, datetime

import pytest

import config
from src.domain.models import Bounds
from src.domain.nwp import HRRRProvider, StubNWPProvider, get_provider
from src.errors import ConfigError

FARM01_LAT, FARM01_LON = 41.25, -96.53
VALID_TIME = datetime(2026, 1, 2, 14, 0, tzinfo=UTC)


def test_point_forecast_is_deterministic() -> None:
    provider = StubNWPProvider()

    first = provider.point_forecast(FARM01_LAT, FARM01_LON, VALID_TIME)
    second = provider.point_forecast(FARM01_LAT, FARM01_LON, VALID_TIME)

    assert first == second


def test_point_forecast_differs_by_hour() -> None:
    provider = StubNWPProvider()
    other_hour = VALID_TIME.replace(hour=(VALID_TIME.hour + 6) % 24)

    first = provider.point_forecast(FARM01_LAT, FARM01_LON, VALID_TIME)
    second = provider.point_forecast(FARM01_LAT, FARM01_LON, other_hour)

    assert first != second


def test_point_forecast_within_documented_bounds() -> None:
    provider = StubNWPProvider()

    for hour in range(24):
        forecast = provider.point_forecast(FARM01_LAT, FARM01_LON, VALID_TIME.replace(hour=hour))
        assert 0.5 <= forecast.wind_speed_ms <= 22.0
        assert 0.0 <= forecast.wind_direction_deg < 360.0
        assert -25.0 <= forecast.air_temp_c <= 45.0
        assert forecast.is_simulated is True
        assert not math.isnan(forecast.wind_speed_ms)


def test_point_history_returns_expected_count() -> None:
    provider = StubNWPProvider()
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)

    history = provider.point_history(FARM01_LAT, FARM01_LON, start, end, step_hours=1)

    assert len(history) == 6
    assert [f.valid_time.hour for f in history] == [0, 1, 2, 3, 4, 5]
    assert all(f.is_simulated for f in history)


def test_point_history_respects_step_hours() -> None:
    provider = StubNWPProvider()
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

    history = provider.point_history(FARM01_LAT, FARM01_LON, start, end, step_hours=6)

    assert len(history) == 5


def test_point_history_rejects_non_positive_step() -> None:
    provider = StubNWPProvider()
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="step_hours"):
        provider.point_history(FARM01_LAT, FARM01_LON, start, end, step_hours=0)


def test_grid_returns_expected_shape() -> None:
    provider = StubNWPProvider()
    bounds = Bounds(lat_min=41.0, lat_max=42.0, lon_min=-97.0, lon_max=-96.0)

    field = provider.grid(bounds, "wind", VALID_TIME)

    assert field.values.shape == (config.NWP_GRID_RESOLUTION, config.NWP_GRID_RESOLUTION)
    assert field.lats.shape == (config.NWP_GRID_RESOLUTION, config.NWP_GRID_RESOLUTION)
    assert field.lons.shape == (config.NWP_GRID_RESOLUTION, config.NWP_GRID_RESOLUTION)
    assert field.is_simulated is True
    assert field.variable == "wind"


def test_grid_is_deterministic() -> None:
    provider = StubNWPProvider()
    bounds = Bounds(lat_min=41.0, lat_max=42.0, lon_min=-97.0, lon_max=-96.0)

    first = provider.grid(bounds, "temperature", VALID_TIME)
    second = provider.grid(bounds, "temperature", VALID_TIME)

    assert (first.values == second.values).all()


def test_hrrr_provider_methods_all_raise_not_implemented() -> None:
    provider = HRRRProvider()
    bounds = Bounds(lat_min=41.0, lat_max=42.0, lon_min=-97.0, lon_max=-96.0)

    with pytest.raises(NotImplementedError):
        provider.point_forecast(FARM01_LAT, FARM01_LON, VALID_TIME)
    with pytest.raises(NotImplementedError):
        provider.point_history(FARM01_LAT, FARM01_LON, VALID_TIME, VALID_TIME)
    with pytest.raises(NotImplementedError):
        provider.grid(bounds, "wind", VALID_TIME)


def test_get_provider_stub_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "NWP_PROVIDER", "stub")

    assert isinstance(get_provider(), StubNWPProvider)


def test_get_provider_hrrr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "NWP_PROVIDER", "hrrr")

    assert isinstance(get_provider(), HRRRProvider)


def test_get_provider_unknown_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "NWP_PROVIDER", "not-a-real-provider")

    with pytest.raises(ConfigError, match="NWP_PROVIDER"):
        get_provider()
