"""NWP (weather) provider interface, deterministic stub, and the real HRRR-backed provider.

`StubNWPProvider` is the default (`config.NWP_PROVIDER`); it stays in the domain layer
(`CLAUDE.md` §4.1) as pure deterministic pseudo-randomness — identical `(lat, lon, valid_time)`
must give identical output or the wind rose flickers on every Streamlit rerun.

`HRRRProvider` fetches live HRRR 80 m winds and 2 m temperature. All of its network + GRIB +
heavy scientific-stack work lives in `src/data/hrrr.py` (imported inside the method bodies, so
stub-mode startup never loads `eccodes`); this class only resolves cycles, checks the CONUS
domain, and shapes the result into `PointForecast` / `GridField`. It raises
`NWPUnavailableError` (a `WindFleetError`) whenever it cannot serve a request; callers in the
UI catch that and render a message rather than letting it stop the app (`CLAUDE.md` §5.3).

SPEC-GAP: `PROJECT_SPEC.md` §9 / `CLAUDE.md` §5.8 specify `HRRRProvider` as a ToDo skeleton;
built by explicit request. The prompt asked for 100 m; HRRR `sfc` gives wind at 80 m AGL (the
hub-height proxy) and temperature only at 2 m AGL.
"""

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import numpy as np

import config
from src.domain.models import Bounds, GridField, PointForecast
from src.errors import ConfigError, NWPUnavailableError

logger = logging.getLogger(__name__)

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
# HRRRProvider — real HRRR via herbie-data (SPEC-GAP: PROJECT_SPEC.md §9 marks this a ToDo)
# --------------------------------------------------------------------------------------


class HRRRProvider:
    """Real HRRR-backed NWP provider (`NWP_PROVIDER=hrrr`).

    Wind speed/direction come from the HRRR 80 m U/V components and temperature from the 2 m
    field (`PROJECT_SPEC.md` §9 asks for "the farm's conditions" — a farm box spans only a few
    HRRR cells, so the nearest cell and a farm-scoped grid agree). The fetch, decode, and
    regrid all happen in `src/data/hrrr.py`; this class owns only cycle resolution, the CONUS
    check, and result shaping.

    Every method raises `NWPUnavailableError` when the point/box is outside the HRRR domain or
    no run can be loaded for the resolved cycle (missing from the archive, offline, transient).
    """

    name: str = "hrrr"

    def point_forecast(self, lat: float, lon: float, valid_time: datetime) -> PointForecast:
        """Return HRRR conditions at the native cell nearest `(lat, lon)` for `valid_time`.

        Raises:
            NWPUnavailableError: `(lat, lon)` is off the HRRR grid, or the covering run is
                unavailable.
        """
        from src.data import hrrr

        if not hrrr.bbox_intersects_domain(lat, lat, lon, lon):
            raise NWPUnavailableError(f"({lat:.3f}, {lon:.3f}) is outside the HRRR domain.")

        cycle = hrrr.cycle_for(valid_time)
        wind = hrrr.fetch_field(cycle, "wind")
        temperature = hrrr.fetch_field(cycle, "temperature")
        speed_ms, direction_deg = hrrr.nearest_sample(wind, lat, lon)
        temp_c, _ = hrrr.nearest_sample(temperature, lat, lon)
        if direction_deg is None:  # a wind field always carries direction — defensive
            raise NWPUnavailableError(f"HRRR wind direction missing for {cycle:%Y-%m-%d %HZ}.")
        return PointForecast(
            valid_time=cycle,
            wind_speed_ms=speed_ms,
            wind_direction_deg=direction_deg,
            air_temp_c=temp_c,
            is_simulated=False,
        )

    def point_history(
        self, lat: float, lon: float, start: datetime, end: datetime, step_hours: int = 1
    ) -> list[PointForecast]:
        """Return one HRRR `PointForecast` per `step_hours` cycle across `[start, end]`.

        A cycle missing from the archive is skipped (logged), so the wind rose still renders
        from whatever runs exist.

        Raises:
            NWPUnavailableError: `(lat, lon)` is off the HRRR grid, or *no* run in the window
                could be loaded.
        """
        from src.data import hrrr

        if step_hours <= 0:
            raise ValueError(f"step_hours must be positive, got {step_hours}")
        if not hrrr.bbox_intersects_domain(lat, lat, lon, lon):
            raise NWPUnavailableError(f"({lat:.3f}, {lon:.3f}) is outside the HRRR domain.")

        step = timedelta(hours=step_hours)
        cycle = hrrr.cycle_for(start)
        last_cycle = hrrr.cycle_for(end)
        forecasts: list[PointForecast] = []
        while cycle <= last_cycle:
            try:
                forecasts.append(self.point_forecast(lat, lon, cycle))
            except NWPUnavailableError:
                logger.warning("Skipping unavailable HRRR cycle %s", cycle)
            cycle += step

        if not forecasts:
            raise NWPUnavailableError(
                f"No HRRR runs available between {start:%Y-%m-%d %HZ} and {end:%Y-%m-%d %HZ}."
            )
        return forecasts

    def grid(
        self, bounds: Bounds, variable: Literal["wind", "temperature"], valid_time: datetime
    ) -> GridField:
        """Return an HRRR field over `bounds`, resampled to a regular lat/lon mesh.

        Raises:
            NWPUnavailableError: `bounds` does not overlap the HRRR domain, or the covering
                run is unavailable.
        """
        from src.data import hrrr

        if not hrrr.bbox_intersects_domain(
            bounds.lat_min, bounds.lat_max, bounds.lon_min, bounds.lon_max
        ):
            raise NWPUnavailableError("The requested area is outside the HRRR domain.")

        cycle = hrrr.cycle_for(valid_time)
        native = hrrr.fetch_field(cycle, variable)
        lats, lons, values = hrrr.regrid_to_mesh(
            native,
            bounds.lat_min,
            bounds.lat_max,
            bounds.lon_min,
            bounds.lon_max,
            config.HRRR_GRID_RESOLUTION,
        )
        return GridField(
            lats=lats,
            lons=lons,
            values=values,
            variable=variable,
            valid_time=cycle,
            is_simulated=False,
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
