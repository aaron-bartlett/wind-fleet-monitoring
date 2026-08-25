"""Shared dataclasses and enums for the domain, data, and UI layers (`CLAUDE.md` §4.3).

Pure data plus a handful of self-contained derivations (`Bounds.expanded`, `compass_point`,
`TelemetryRecord.lag_minutes`). Every shared type in the project is defined exactly once here
so downstream modules never redefine their own version. This module has no I/O and no
Streamlit/Folium/Plotly imports, per the layering rule in `CLAUDE.md` §4.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

import config

# --------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------


class HealthStatus(StrEnum):
    """A turbine's classified health, from `domain.health.classify`."""

    HEALTHY = "Healthy"
    WARNING = "Warning"
    CRITICAL = "Critical"
    ERROR = "Error"


class Level(StrEnum):
    """The current drill-down level of the map/dashboard state machine."""

    FLEET = "fleet"
    FARM = "farm"
    TURBINE = "turbine"


class Severity(StrEnum):
    """How serious a single threshold breach is."""

    MINOR = "minor"
    MAJOR = "major"


# --------------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Breach:
    """A single threshold violation on one metric of one telemetry record."""

    metric: str
    value: float
    threshold: float
    severity: Severity
    message: str


@dataclass(frozen=True, slots=True)
class HealthResult:
    """The outcome of classifying one turbine's latest telemetry record."""

    status: HealthStatus
    minor: tuple[Breach, ...]
    major: tuple[Breach, ...]
    errors: tuple[str, ...]

    @property
    def color(self) -> str:
        """The hex color for this result's status (`config.HEALTH_COLORS`)."""
        return config.HEALTH_COLORS[self.status.value]


# --------------------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TelemetryRecord:
    """One turbine telemetry reading. `timestamp` and `received_at` are always tz-aware UTC."""

    turbine_id: str
    farm_id: str
    timestamp: datetime
    received_at: datetime
    power_output_kw: float | None
    wind_speed_ms: float | None
    rotor_rpm: float | None
    blade_pitch_deg: float | None
    gearbox_temp_c: float | None

    @property
    def lag_minutes(self) -> float:
        """Ingest lag: time between the measurement and its arrival, in minutes."""
        return (self.received_at - self.timestamp).total_seconds() / 60

    def get(self, metric: str) -> float | None:
        """Return the value of the named metric.

        Args:
            metric: One of `config.METRICS`.

        Returns:
            The metric's value, or `None` if it was NULL in telemetry.

        Raises:
            KeyError: `metric` does not name one of this record's telemetry fields.
        """
        match metric:
            case "power_output_kw":
                return self.power_output_kw
            case "wind_speed_ms":
                return self.wind_speed_ms
            case "rotor_rpm":
                return self.rotor_rpm
            case "blade_pitch_deg":
                return self.blade_pitch_deg
            case "gearbox_temp_c":
                return self.gearbox_temp_c
            case _:
                raise KeyError(metric)


# --------------------------------------------------------------------------------------
# Geography
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bounds:
    """A rectangular lat/lon bounding box."""

    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def expanded(self, factor: float) -> Bounds:
        """Return a box widened so each axis spans `factor` times its original span.

        The expansion is centered on the original midpoint. An axis with zero span (all
        points share a coordinate) falls back to a fixed pad of `config.ZERO_SPAN_PAD_DEG`
        on each side rather than staying a degenerate line.

        Args:
            factor: The multiplier applied to each axis span (e.g. 1.10 for +10%).

        Returns:
            A new, wider `Bounds`.
        """
        lat_min, lat_max = _expand_axis(self.lat_min, self.lat_max, factor)
        lon_min, lon_max = _expand_axis(self.lon_min, self.lon_max, factor)
        return Bounds(lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max)

    def as_folium(self) -> list[list[float]]:
        """Return `[[lat_min, lon_min], [lat_max, lon_max]]`, the shape `fit_bounds` expects."""
        return [[self.lat_min, self.lon_min], [self.lat_max, self.lon_max]]


def _expand_axis(low: float, high: float, factor: float) -> tuple[float, float]:
    """Widen one axis of a `Bounds` to `factor` times its span, centered on its midpoint."""
    span = high - low
    midpoint = (high + low) / 2
    if span == 0:
        return (midpoint - config.ZERO_SPAN_PAD_DEG, midpoint + config.ZERO_SPAN_PAD_DEG)
    half_span = (span * factor) / 2
    return (midpoint - half_span, midpoint + half_span)


def compass_point(degrees: float) -> str:
    """Return the 16-point compass abbreviation for a bearing.

    Args:
        degrees: A bearing in degrees, conventionally in `[0, 360)` (values outside that
            range wrap via modulo).

    Returns:
        One of `config.COMPASS_POINTS`, e.g. `"N"`, `"NNE"`, `"NW"`.
    """
    sector = round((degrees % 360) / 22.5) % len(config.COMPASS_POINTS)
    return config.COMPASS_POINTS[sector]


# --------------------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Farm:
    """A wind farm, as recorded in `farms.csv`."""

    farm_id: str
    farm_name: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class Turbine:
    """A wind turbine, as recorded in `turbines.csv` (denormalized `farm_name` dropped)."""

    turbine_id: str
    farm_id: str
    latitude: float
    longitude: float


# --------------------------------------------------------------------------------------
# NWP
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PointForecast:
    """Wind and temperature conditions at a single point and time."""

    valid_time: datetime
    wind_speed_ms: float
    wind_direction_deg: float  # meteorological convention: direction wind is FROM
    air_temp_c: float
    is_simulated: bool


@dataclass(frozen=True, slots=True)
class GridField:
    """A gridded NWP variable over a bounding box at a single valid time."""

    lats: NDArray[np.float64]
    lons: NDArray[np.float64]
    values: NDArray[np.float64]
    variable: str
    valid_time: datetime
    is_simulated: bool
