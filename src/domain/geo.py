"""Timezone and bounds helpers built on the query layer (`CLAUDE.md` §4.1).

Domain layer: no Streamlit, no SQL. `local_time` wraps `timezonefinder`/`zoneinfo` behind a
single seam so the (expensive-to-construct) `TimezoneFinder` is built once per process;
`fleet_bounds`/`farm_view_bounds` turn `src/data/queries` bounds queries into the padded boxes
`map_view.py` fits to (`PROJECT_SPEC.md` §8.1).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import duckdb
from timezonefinder import TimezoneFinder

import config
from src.data import queries
from src.domain.models import Bounds, Farm

# Construction does a one-time load of the timezone-boundary shapefile index; a module-level
# singleton means every call in the process reuses it instead of paying that cost repeatedly.
_TIMEZONE_FINDER = TimezoneFinder()


def local_time(utc_dt: datetime, latitude: float, longitude: float) -> tuple[datetime, str]:
    """Convert a UTC datetime to local time at a coordinate.

    Args:
        utc_dt: A tz-aware UTC datetime.
        latitude: Coordinate latitude.
        longitude: Coordinate longitude.

    Returns:
        `(local_datetime, tz_abbreviation)`. Falls back to `(utc_dt, "UTC")` when the
        coordinate has no resolvable timezone (e.g. an ocean coordinate).
    """
    tz_name = _TIMEZONE_FINDER.timezone_at(lat=latitude, lng=longitude)
    if tz_name is None:
        return utc_dt, "UTC"
    local_dt = utc_dt.astimezone(ZoneInfo(tz_name))
    return local_dt, local_dt.strftime("%Z")


def fleet_bounds(con: duckdb.DuckDBPyConnection) -> Bounds | None:
    """Return the fleet-wide bounding box, expanded per `config.BOUNDS_EXPANSION`.

    Returns:
        `None` when there are no farms.
    """
    bounds = queries.get_fleet_bounds(con)
    if bounds is None:
        return None
    return bounds.expanded(config.BOUNDS_EXPANSION)


def farm_view_bounds(con: duckdb.DuckDBPyConnection, farm_id: str, farm: Farm) -> Bounds:
    """Return the bounding box for one farm's turbine layer.

    Falls back to a fixed box centered on the farm's own coordinate when the farm has no
    turbines, or exactly one (a single point has no span to expand); otherwise expands the
    turbines' bounding box per `config.BOUNDS_EXPANSION`.

    Args:
        con: Open DuckDB connection.
        farm_id: The farm to compute bounds for.
        farm: The farm's own record, used for the single-point fallback.

    Returns:
        A non-degenerate `Bounds`.
    """
    bounds = queries.get_farm_turbine_bounds(con, farm_id)
    if bounds is None or _is_single_point(bounds):
        return _point_box(farm.latitude, farm.longitude)
    return bounds.expanded(config.BOUNDS_EXPANSION)


def _is_single_point(bounds: Bounds) -> bool:
    return bounds.lat_min == bounds.lat_max and bounds.lon_min == bounds.lon_max


def _point_box(latitude: float, longitude: float) -> Bounds:
    return Bounds(
        lat_min=latitude - config.ZERO_SPAN_PAD_DEG,
        lat_max=latitude + config.ZERO_SPAN_PAD_DEG,
        lon_min=longitude - config.ZERO_SPAN_PAD_DEG,
        lon_max=longitude + config.ZERO_SPAN_PAD_DEG,
    )
