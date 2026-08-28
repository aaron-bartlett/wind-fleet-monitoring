"""Tests for src/domain/nwp.py: stub determinism/bounds, the HRRR provider, provider selection.

The HRRR tests never touch the network — they monkeypatch `src.data.hrrr.fetch_field` with a
fabricated `NativeField` (`CLAUDE.md` §5.7).
"""

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

import config
from src.data import hrrr
from src.data.hrrr import NativeField
from src.domain.models import Bounds
from src.domain.nwp import HRRRProvider, StubNWPProvider, get_provider
from src.errors import ConfigError, NWPUnavailableError

FARM01_LAT, FARM01_LON = 41.25, -96.53
VALID_TIME = datetime(2026, 1, 2, 14, 0, tzinfo=UTC)


def _fake_native(
    variable: str,
    valid_time: datetime,
    *,
    lat_range: tuple[float, float] = (39.0, 44.0),
    lon_range: tuple[float, float] = (-100.0, -93.0),
    n: int = 120,
) -> NativeField:
    """A small synthetic HRRR native field with a smooth south->north gradient."""
    lats = np.linspace(lat_range[0], lat_range[1], n)
    lons = np.linspace(lon_range[0], lon_range[1], n)
    lon2d, lat2d = np.meshgrid(lons, lats)
    span = lat_range[1] - lat_range[0]
    if variable == "wind":
        values = 5.0 + 3.0 * (lat2d - lat_range[0]) / span  # 5..8 m/s
        direction: np.ndarray | None = np.full((n, n), 225.0)
    else:
        values = 12.0 - 6.0 * (lat2d - lat_range[0]) / span  # 12..6 degC
        direction = None
    return NativeField(
        lat2d=lat2d.astype(np.float64),
        lon2d=lon2d.astype(np.float64),
        values2d=values.astype(np.float64),
        direction2d=None if direction is None else direction.astype(np.float64),
        valid_time=valid_time,
    )


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch, fake: Callable[[datetime, str], NativeField]
) -> None:
    monkeypatch.setattr(hrrr, "fetch_field", fake)


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


def test_hrrr_grid_returns_real_gridfield(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, lambda cycle, variable: _fake_native(variable, cycle))
    bounds = Bounds(lat_min=41.0, lat_max=42.0, lon_min=-97.5, lon_max=-96.0)

    field = HRRRProvider().grid(bounds, "wind", VALID_TIME)

    res = config.HRRR_GRID_RESOLUTION
    assert field.values.shape == (res, res)
    assert field.lats.shape == (res, res)
    assert field.is_simulated is False
    assert field.variable == "wind"
    assert field.valid_time == hrrr.cycle_for(VALID_TIME)
    assert field.lats.min() == pytest.approx(41.0)
    assert field.lats.max() == pytest.approx(42.0)
    assert not np.isnan(field.values).any()
    # Values interpolated from the 5..8 m/s fake gradient stay inside it.
    assert 5.0 <= float(field.values.min()) <= float(field.values.max()) <= 8.0


def test_hrrr_grid_outside_domain_raises_without_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_cycle: datetime, _variable: str) -> NativeField:
        raise AssertionError("fetch_field must not be called for an out-of-domain box")

    _patch_fetch(monkeypatch, _boom)

    with pytest.raises(NWPUnavailableError):
        HRRRProvider().grid(
            Bounds(lat_min=0.0, lat_max=1.0, lon_min=10.0, lon_max=11.0), "wind", VALID_TIME
        )


def test_hrrr_point_forecast_uses_nearest_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, lambda cycle, variable: _fake_native(variable, cycle))

    forecast = HRRRProvider().point_forecast(41.0, -97.0, VALID_TIME)

    assert forecast.is_simulated is False
    assert forecast.valid_time == hrrr.cycle_for(VALID_TIME)
    assert 5.0 <= forecast.wind_speed_ms <= 8.0
    assert 0.0 <= forecast.wind_direction_deg < 360.0
    assert forecast.wind_direction_deg == pytest.approx(225.0)
    assert 6.0 <= forecast.air_temp_c <= 12.0


def test_hrrr_point_forecast_off_grid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # In-CONUS query point, but the (fake) native grid sits far away -> nearest cell > 5 km.
    _patch_fetch(
        monkeypatch,
        lambda cycle, variable: _fake_native(
            variable, cycle, lat_range=(48.0, 49.0), lon_range=(-125.0, -124.0)
        ),
    )

    with pytest.raises(NWPUnavailableError):
        HRRRProvider().point_forecast(30.0, -95.0, VALID_TIME)


def test_hrrr_point_history_counts_and_skips_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = hrrr.cycle_for(VALID_TIME) + timedelta(hours=2)

    def _fake(cycle: datetime, variable: str) -> NativeField:
        if cycle == missing:
            raise NWPUnavailableError(f"no run for {cycle}")
        return _fake_native(variable, cycle)

    _patch_fetch(monkeypatch, _fake)
    start = VALID_TIME
    end = VALID_TIME + timedelta(hours=4)

    history = HRRRProvider().point_history(41.0, -97.0, start, end, step_hours=1)

    assert len(history) == 4  # 5 cycles in [start, end], one skipped
    assert all(f.is_simulated is False for f in history)
    assert missing not in [f.valid_time for f in history]


def test_hrrr_point_history_all_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(_cycle: datetime, _variable: str) -> NativeField:
        raise NWPUnavailableError("archive empty")

    _patch_fetch(monkeypatch, _fake)

    with pytest.raises(NWPUnavailableError):
        HRRRProvider().point_history(
            41.0, -97.0, VALID_TIME, VALID_TIME + timedelta(hours=3), step_hours=1
        )


def test_hrrr_grid_propagates_fetch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(_cycle: datetime, _variable: str) -> NativeField:
        raise NWPUnavailableError("offline")

    _patch_fetch(monkeypatch, _fake)

    with pytest.raises(NWPUnavailableError):
        HRRRProvider().grid(
            Bounds(lat_min=41.0, lat_max=42.0, lon_min=-97.0, lon_max=-96.0), "wind", VALID_TIME
        )


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
