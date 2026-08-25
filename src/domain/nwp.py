"""NWP (weather) provider interface + stub, since telemetry has no wind-direction or air-temp
column (`PROJECT_SPEC.md` §9).

Domain layer (`CLAUDE.md` §4.1): pure functions and deterministic pseudo-randomness, no I/O,
no Streamlit. `StubNWPProvider` is what v1 actually wires up (`config.NWP_PROVIDER`); it must be
fully deterministic given `(lat, lon, valid_time)` because Streamlit reruns constantly and a
flickering wind rose would be a visible bug. `HRRRProvider` is a documented ToDo skeleton —
every method raises `NotImplementedError`, and per `CLAUDE.md` §2.2 `herbie-data` is never
imported at module scope (it is not installed in v1; see `requirements-optional.txt`).
"""

import math
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import numpy as np

import config
from src.domain.models import Bounds, GridField, PointForecast
from src.errors import ConfigError

# --------------------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------------------


class NWPProvider(Protocol):
    """Wind and temperature data for a point or grid, independent of its source."""

    name: str

    def point_forecast(self, lat: float, lon: float, valid_time: datetime) -> PointForecast:
        """Return conditions at one point and time."""
        ...

    def point_history(
        self, lat: float, lon: float, start: datetime, end: datetime, step_hours: int = 1
    ) -> list[PointForecast]:
        """Return conditions at one point, sampled every `step_hours` across `[start, end]`."""
        ...

    def grid(
        self, bounds: Bounds, variable: Literal["wind", "temperature"], valid_time: datetime
    ) -> GridField:
        """Return a gridded field of one variable over `bounds` at `valid_time`."""
        ...


# --------------------------------------------------------------------------------------
# StubNWPProvider — the v1 implementation
# --------------------------------------------------------------------------------------

# Bounds the stub's synthetic values stay within, matching the documented ranges in
# IMPLEMENTATION_PLAN.md Phase 7.
_WIND_SPEED_BOUNDS_MS = (0.5, 22.0)
_AIR_TEMP_BOUNDS_C = (-25.0, 45.0)


class StubNWPProvider:
    """Deterministic synthetic wind/temperature data — the provider actually used in v1.

    Every value is a function of `(round(lat, 3), round(lon, 3), hour bucket, NWP_STUB_SEED)`
    only, so identical calls (including across Streamlit reruns) return identical results.
    Every returned object carries `is_simulated=True`; the UI must render a "Simulated" badge
    wherever stub data appears (`PROJECT_SPEC.md` §9).
    """

    name: str = "stub"

    def point_forecast(self, lat: float, lon: float, valid_time: datetime) -> PointForecast:
        """Return deterministic synthetic conditions at `(lat, lon)` and `valid_time`.

        Args:
            lat: Point latitude.
            lon: Point longitude.
            valid_time: The time to simulate conditions for (tz-aware).

        Returns:
            A `PointForecast` with `is_simulated=True`.
        """
        rng = _seeded_rng(lat, lon, valid_time)
        hour = _fractional_hour_utc(valid_time)
        return PointForecast(
            valid_time=valid_time,
            wind_speed_ms=_simulated_wind_speed_ms(hour, rng),
            wind_direction_deg=_simulated_wind_direction_deg(lat, hour, rng),
            air_temp_c=_simulated_air_temp_c(lat, hour, rng),
            is_simulated=True,
        )

    def point_history(
        self, lat: float, lon: float, start: datetime, end: datetime, step_hours: int = 1
    ) -> list[PointForecast]:
        """Return one `point_forecast` per `step_hours` step across `[start, end]`, inclusive.

        Args:
            lat: Point latitude.
            lon: Point longitude.
            start: First valid time.
            end: Last valid time (inclusive).
            step_hours: Spacing between samples, in hours.

        Returns:
            Forecasts in ascending time order.

        Raises:
            ValueError: `step_hours` is not positive.
        """
        if step_hours <= 0:
            raise ValueError(f"step_hours must be positive, got {step_hours}")
        step = timedelta(hours=step_hours)
        forecasts: list[PointForecast] = []
        valid_time = start
        while valid_time <= end:
            forecasts.append(self.point_forecast(lat, lon, valid_time))
            valid_time += step
        return forecasts

    def grid(
        self, bounds: Bounds, variable: Literal["wind", "temperature"], valid_time: datetime
    ) -> GridField:
        """Return a synthetic `NWP_GRID_RESOLUTION` x `NWP_GRID_RESOLUTION` grid over `bounds`.

        Args:
            bounds: The area to cover.
            variable: `"wind"` (speed magnitude, m/s) or `"temperature"` (°C).
            valid_time: The time to simulate conditions for.

        Returns:
            A `GridField` with `is_simulated=True`.
        """
        resolution = config.NWP_GRID_RESOLUTION
        lats_1d = np.linspace(bounds.lat_min, bounds.lat_max, resolution)
        lons_1d = np.linspace(bounds.lon_min, bounds.lon_max, resolution)
        lon_grid, lat_grid = np.meshgrid(lons_1d, lats_1d)
        values = np.empty((resolution, resolution), dtype=np.float64)
        for i in range(resolution):
            for j in range(resolution):
                forecast = self.point_forecast(
                    float(lat_grid[i, j]), float(lon_grid[i, j]), valid_time
                )
                values[i, j] = forecast.wind_speed_ms if variable == "wind" else forecast.air_temp_c
        return GridField(
            lats=lat_grid,
            lons=lon_grid,
            values=values,
            variable=variable,
            valid_time=valid_time,
            is_simulated=True,
        )


def _seeded_rng(lat: float, lon: float, valid_time: datetime) -> np.random.Generator:
    """Build a `numpy` RNG seeded from `(lat, lon, hour bucket, NWP_STUB_SEED)`.

    Rounding lat/lon and bucketing to the hour is what makes the stub deterministic across
    Streamlit reruns: the same point and hour always draw the same seed.
    """
    hour_bucket = int(valid_time.timestamp() // 3600)
    key = (round(lat, 3), round(lon, 3), hour_bucket, config.NWP_STUB_SEED)
    seed = hash(key) % (2**32)  # hash() of a tuple of numbers is not str-randomized
    return np.random.default_rng(seed)


def _fractional_hour_utc(valid_time: datetime) -> float:
    """Return `valid_time`'s hour-of-day in UTC as a float, for a smooth diurnal signal."""
    utc_dt = valid_time.astimezone(UTC)
    return utc_dt.hour + utc_dt.minute / 60.0 + utc_dt.second / 3600.0


def _simulated_wind_speed_ms(hour: float, rng: np.random.Generator) -> float:
    """Smooth diurnal wind speed (afternoon peak) plus small seeded noise, clipped to bounds."""
    diurnal = 8.0 + 5.0 * math.sin(2 * math.pi * (hour - 4.0) / 24.0)
    noise = rng.normal(0.0, 1.2)
    low, high = _WIND_SPEED_BOUNDS_MS
    return float(np.clip(diurnal + noise, low, high))


def _simulated_wind_direction_deg(lat: float, hour: float, rng: np.random.Generator) -> float:
    """A slowly-rotating bearing derived from latitude and hour, in `[0, 360)`."""
    base = (lat * 3.0 + hour * 12.0) % 360.0
    jitter = rng.normal(0.0, 8.0)
    return float((base + jitter) % 360.0)


def _simulated_air_temp_c(lat: float, hour: float, rng: np.random.Generator) -> float:
    """Latitude- and hour-dependent air temperature plus small seeded noise, clipped to bounds."""
    lat_effect = 20.0 - 0.4 * abs(lat)
    diurnal = 8.0 * math.sin(2 * math.pi * (hour - 9.0) / 24.0)
    noise = rng.normal(0.0, 1.5)
    low, high = _AIR_TEMP_BOUNDS_C
    return float(np.clip(lat_effect + diurnal + noise, low, high))


# --------------------------------------------------------------------------------------
# HRRRProvider — ToDo skeleton (PROJECT_SPEC.md §9); not built in v1
# --------------------------------------------------------------------------------------


class HRRRProvider:
    """Real HRRR-backed NWP provider — documented skeleton only, not implemented in v1.

    Every method below raises `NotImplementedError`. When this provider is wired up, the
    implementation must: use `herbie-data` to fetch the HRRR run covering the requested time
    (imported inside the method body, never at module scope — `CLAUDE.md` §2.2, since
    `herbie-data` is not installed per `requirements-optional.txt`); pick the grid point
    nearest the **farm** coordinate, since all turbines in a farm share the farm's conditions
    (`PROJECT_SPEC.md` §9); derive wind speed and direction from the 80 m U/V wind components;
    and take the 2 m temperature field. HRRR covers CONUS only, so a farm outside that domain
    must raise `NWPUnavailableError` rather than returning a value or crashing.
    """

    name: str = "hrrr"

    def point_forecast(self, lat: float, lon: float, valid_time: datetime) -> PointForecast:
        """Fetch a single-point HRRR forecast nearest `(lat, lon)`, valid at `valid_time`.

        Raises:
            NotImplementedError: Always — this is a v1 ToDo skeleton (`PROJECT_SPEC.md` §9).
        """
        raise NotImplementedError(
            "HRRRProvider.point_forecast is not implemented in v1; see PROJECT_SPEC.md §9. "
            "Intended implementation: fetch HRRR via herbie-data, pick the nearest grid "
            "point to the farm coordinate, derive wind from 80 m U/V and temperature from "
            "the 2 m field; raise NWPUnavailableError for farms outside the CONUS domain."
        )

    def point_history(
        self, lat: float, lon: float, start: datetime, end: datetime, step_hours: int = 1
    ) -> list[PointForecast]:
        """Fetch a series of single-point HRRR forecasts across `[start, end]`.

        Raises:
            NotImplementedError: Always — this is a v1 ToDo skeleton (`PROJECT_SPEC.md` §9).
        """
        raise NotImplementedError(
            "HRRRProvider.point_history is not implemented in v1; see PROJECT_SPEC.md §9. "
            "Intended implementation: repeat point_forecast's HRRR fetch for each run between "
            "start and end, stepping by step_hours."
        )

    def grid(
        self, bounds: Bounds, variable: Literal["wind", "temperature"], valid_time: datetime
    ) -> GridField:
        """Fetch a gridded HRRR field over `bounds` at `valid_time`.

        Raises:
            NotImplementedError: Always — this is a v1 ToDo skeleton (`PROJECT_SPEC.md` §9).
        """
        raise NotImplementedError(
            "HRRRProvider.grid is not implemented in v1; see PROJECT_SPEC.md §9. Intended "
            "implementation: fetch the HRRR grid via herbie-data, subset it to bounds, and "
            "derive wind speed or 2 m temperature per variable; raise NWPUnavailableError "
            "when bounds fall outside the CONUS domain."
        )


# --------------------------------------------------------------------------------------
# Provider selection
# --------------------------------------------------------------------------------------


def get_provider() -> NWPProvider:
    """Return the configured `NWPProvider` (`config.NWP_PROVIDER`).

    Returns:
        A `StubNWPProvider` for `"stub"`, or an `HRRRProvider` for `"hrrr"`.

    Raises:
        ConfigError: `config.NWP_PROVIDER` names neither `"stub"` nor `"hrrr"`.
    """
    if config.NWP_PROVIDER == "stub":
        return StubNWPProvider()
    if config.NWP_PROVIDER == "hrrr":
        return HRRRProvider()
    raise ConfigError(f"Unknown NWP_PROVIDER {config.NWP_PROVIDER!r}; expected 'stub' or 'hrrr'.")
