"""Live HRRR fetch + regridding for the real NWP provider (`CLAUDE.md` §4.1 — data layer).

`src/domain/nwp.py::HRRRProvider` is the only caller. This module owns everything that touches
the network, GRIB, and the heavy scientific stack: it resolves a `valid_time` to an HRRR
cycle, downloads the 80 m wind and 2 m temperature messages via Herbie, converts them to plain
`float64` arrays, and resamples the native (curvilinear Lambert) grid onto a regular lat/lon
mesh that `src/ui/map_view.py` can paint as an `ImageOverlay`.

`herbie`, `xarray`, and `scipy.interpolate` are imported **inside the functions that need
them**, so `import src.data.hrrr` — and therefore the whole test suite and the stub-mode app —
works even when the HRRR stack is not installed. Only a real HRRR fetch pulls them in.

Takes plain float bounds rather than a `domain.models.Bounds` so the data layer never imports
the domain layer (`CLAUDE.md` §4.1 layering rule).

SPEC-GAP: built despite `PROJECT_SPEC.md` §9 marking `HRRRProvider` a ToDo and `CLAUDE.md` §5.8
saying not to — by explicit request. The prompt asked for 100 m fields; HRRR's `sfc` product
tops out at 80 m AGL for wind and 2 m for temperature, so those levels are used. See README §16.
"""

from __future__ import annotations

import functools
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import numpy as np
from numpy.typing import NDArray

import config
from src.errors import NWPUnavailableError

if TYPE_CHECKING:
    import xarray as xr

logger = logging.getLogger(__name__)

Variable = Literal["wind", "temperature"]

_KELVIN_OFFSET_C = 273.15
_EARTH_RADIUS_KM = 6371.0088
# Degrees of padding around the view box when selecting native points, so linear interpolation
# near an edge still has points on every side.
_SELECT_HALO_DEG = 0.35


@dataclass(frozen=True, slots=True, eq=False)
class NativeField:
    """One HRRR variable on its native grid, already converted to SI/display units.

    `values2d` is wind speed in m/s (`variable == "wind"`) or air temperature in °C. For wind,
    `direction2d` carries the meteorological "from" bearing in degrees; it is `None` otherwise.
    All arrays share the native `(y, x)` shape.
    """

    lat2d: NDArray[np.float64]
    lon2d: NDArray[np.float64]
    values2d: NDArray[np.float64]
    direction2d: NDArray[np.float64] | None
    valid_time: datetime


def cycle_for(valid_time: datetime) -> datetime:
    """Resolve `valid_time` to the HRRR cycle at or before it (hour-truncated, UTC).

    Args:
        valid_time: A tz-aware datetime.

    Returns:
        The top-of-hour UTC datetime whose F00 analysis is used.

    Raises:
        ValueError: `valid_time` is naive.
    """
    if valid_time.tzinfo is None:
        raise ValueError("valid_time must be tz-aware.")
    return valid_time.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def bbox_intersects_domain(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> bool:
    """Return whether a lat/lon box overlaps the approximate HRRR CONUS domain.

    A cheap reject (`config.HRRR_DOMAIN_LATLON_BBOX`) so an obviously out-of-domain request
    never triggers a download.
    """
    d_lat_min, d_lat_max, d_lon_min, d_lon_max = config.HRRR_DOMAIN_LATLON_BBOX
    return (
        lat_min <= d_lat_max
        and lat_max >= d_lat_min
        and lon_min <= d_lon_max
        and lon_max >= d_lon_min
    )


@functools.lru_cache(maxsize=6)
def fetch_field(cycle: datetime, variable: Variable) -> NativeField:
    """Download one HRRR field for `cycle` and return it on the native grid.

    Cached per `(cycle, variable)` for the process lifetime so repeated grid/point requests
    against one cycle download and decode it once.

    Args:
        cycle: An HRRR cycle datetime (use `cycle_for`).
        variable: `"wind"` (80 m speed + direction) or `"temperature"` (80 m air temp).

    Returns:
        A `NativeField` in m/s / degrees / °C.

    Raises:
        NWPUnavailableError: No archived run for `cycle`, or the download/decode failed
            (offline, transient, or a GRIB parse error).
    """
    import xarray as xr
    from herbie import Herbie

    search = (
        f":(?:UGRD|VGRD):{config.HRRR_WIND_LEVEL}:"
        if variable == "wind"
        else f":TMP:{config.HRRR_TEMP_LEVEL}:"
    )
    try:
        run = Herbie(
            cycle.strftime("%Y-%m-%d %H:%M"),
            model=config.HRRR_MODEL,
            product=config.HRRR_PRODUCT,
            fxx=config.HRRR_FXX,
            save_dir=str(config.HRRR_CACHE_DIR),
        )
        if getattr(run, "grib", None) is None:
            raise NWPUnavailableError(f"No archived HRRR run for {cycle:%Y-%m-%d %HZ}.")
        dataset = run.xarray(search)
    except NWPUnavailableError:
        raise
    # The Herbie constructor (future/pre-archive dates -> AssertionError) and `.xarray`
    # (cfgrib/fsspec/network) raise a wide, version-dependent set of error types; here they all
    # mean the same thing — the field is unavailable — and must never escape as a raw traceback.
    except Exception as exc:
        logger.exception("HRRR fetch failed for %s %s", cycle, variable)
        raise NWPUnavailableError(
            f"Could not load HRRR {variable} for {cycle:%Y-%m-%d %HZ}: {exc}"
        ) from exc

    if isinstance(dataset, list):
        dataset = xr.merge(dataset)

    lat2d = np.asarray(dataset["latitude"].to_numpy(), dtype=np.float64)
    lon2d = np.asarray(dataset["longitude"].to_numpy(), dtype=np.float64)
    lon2d = np.where(lon2d > 180.0, lon2d - 360.0, lon2d)

    if variable == "wind":
        u = _pick(dataset, ("u", "u80", "UGRD", "unknown"))
        v = _pick(dataset, ("v", "v80", "VGRD"))
        values2d = _wind_speed_ms(u, v)
        direction2d: NDArray[np.float64] | None = _wind_direction_deg(u, v)
    else:
        t_kelvin = _pick(dataset, ("t2m", "t", "TMP", "unknown"))
        values2d = np.asarray(t_kelvin - _KELVIN_OFFSET_C, dtype=np.float64)
        direction2d = None

    return NativeField(
        lat2d=lat2d,
        lon2d=np.asarray(lon2d, dtype=np.float64),
        values2d=np.asarray(values2d, dtype=np.float64),
        direction2d=None if direction2d is None else np.asarray(direction2d, dtype=np.float64),
        valid_time=cycle,
    )


def regrid_to_mesh(
    native: NativeField,
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    n: int,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Resample `native.values2d` onto a regular `n x n` lat/lon mesh over the box.

    The mesh matches what `map_view._add_grid_image_overlay` assumes: row 0 is the southern
    edge (`origin="lower"`), column 0 the western edge.

    Returns:
        `(lat_mesh, lon_mesh, value_mesh)`, each shape `(n, n)`.

    Raises:
        NWPUnavailableError: No native HRRR points fall within the (haloed) box.
    """
    from scipy.interpolate import griddata

    selected = (
        (native.lat2d >= lat_min - _SELECT_HALO_DEG)
        & (native.lat2d <= lat_max + _SELECT_HALO_DEG)
        & (native.lon2d >= lon_min - _SELECT_HALO_DEG)
        & (native.lon2d <= lon_max + _SELECT_HALO_DEG)
    )
    if not bool(selected.any()):
        raise NWPUnavailableError("The requested area is outside the HRRR domain.")

    lat_pts = native.lat2d[selected]
    lon_pts = native.lon2d[selected]
    val_pts = native.values2d[selected]

    if lat_pts.size > config.HRRR_MAX_NATIVE_CELLS:
        stride = math.ceil(lat_pts.size / config.HRRR_MAX_NATIVE_CELLS)
        lat_pts = lat_pts[::stride]
        lon_pts = lon_pts[::stride]
        val_pts = val_pts[::stride]

    lats_1d = np.linspace(lat_min, lat_max, n)
    lons_1d = np.linspace(lon_min, lon_max, n)
    lon_mesh, lat_mesh = np.meshgrid(lons_1d, lats_1d)

    points = np.column_stack((lat_pts, lon_pts))
    targets = (lat_mesh, lon_mesh)
    linear = griddata(points, val_pts, targets, method="linear")
    nearest = griddata(points, val_pts, targets, method="nearest")
    values = np.where(np.isnan(linear), nearest, linear)

    return (
        np.asarray(lat_mesh, dtype=np.float64),
        np.asarray(lon_mesh, dtype=np.float64),
        np.asarray(values, dtype=np.float64),
    )


def nearest_sample(native: NativeField, lat: float, lon: float) -> tuple[float, float | None]:
    """Return `(value, direction)` at the native cell nearest `(lat, lon)`.

    `direction` is `None` for a temperature field.

    Raises:
        NWPUnavailableError: The nearest native cell is farther than
            `config.HRRR_NEAREST_MAX_KM` (the point is off the HRRR grid).
    """
    dist2 = (native.lat2d - lat) ** 2 + (native.lon2d - lon) ** 2
    flat = int(np.argmin(dist2))
    ncols = native.lat2d.shape[1]
    row, col = flat // ncols, flat % ncols

    cell_lat = float(native.lat2d[row, col])
    cell_lon = float(native.lon2d[row, col])
    if _haversine_km(lat, lon, cell_lat, cell_lon) > config.HRRR_NEAREST_MAX_KM:
        raise NWPUnavailableError(f"({lat:.3f}, {lon:.3f}) is outside the HRRR domain.")

    value = float(native.values2d[row, col])
    direction = None if native.direction2d is None else float(native.direction2d[row, col])
    return value, direction


def _wind_speed_ms(u: NDArray[np.float64], v: NDArray[np.float64]) -> NDArray[np.float64]:
    """Wind speed magnitude from eastward `u` and northward `v` components (m/s)."""
    return np.asarray(np.hypot(u, v), dtype=np.float64)


def _wind_direction_deg(u: NDArray[np.float64], v: NDArray[np.float64]) -> NDArray[np.float64]:
    """Meteorological "from" bearing in `[0, 360)` for eastward `u` / northward `v`."""
    return np.asarray((270.0 - np.degrees(np.arctan2(v, u))) % 360.0, dtype=np.float64)


def _pick(dataset: xr.Dataset, candidates: tuple[str, ...]) -> NDArray[np.float64]:
    """Return the first matching data variable as a float64 array, else the first data var."""
    for name in candidates:
        if name in dataset.data_vars:
            return np.asarray(dataset[name].to_numpy(), dtype=np.float64)
    first = next(iter(dataset.data_vars))
    return np.asarray(dataset[first].to_numpy(), dtype=np.float64)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))
