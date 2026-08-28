"""Tests for src/data/hrrr.py — the pure cycle / geometry / wind-math helpers.

No network: `fetch_field` (the only function that talks to Herbie) is not exercised here; the
end-to-end HRRR fetch is a manual verification step.
"""

from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pytest

from src.data import hrrr
from src.data.hrrr import NativeField
from src.errors import NWPUnavailableError


def _native(
    *,
    variable: str = "wind",
    lat_range: tuple[float, float] = (40.0, 42.0),
    lon_range: tuple[float, float] = (-98.0, -96.0),
    n: int = 40,
) -> NativeField:
    lats = np.linspace(lat_range[0], lat_range[1], n)
    lons = np.linspace(lon_range[0], lon_range[1], n)
    lon2d, lat2d = np.meshgrid(lons, lats)
    # value == latitude, so a resampled/nearest value is trivially checkable.
    values = lat2d.copy()
    direction = np.full((n, n), 200.0) if variable == "wind" else None
    return NativeField(
        lat2d=lat2d.astype(np.float64),
        lon2d=lon2d.astype(np.float64),
        values2d=values.astype(np.float64),
        direction2d=None if direction is None else direction.astype(np.float64),
        valid_time=datetime(2026, 1, 2, 14, 0, tzinfo=UTC),
    )


# --------------------------------------------------------------------------------------
# cycle_for
# --------------------------------------------------------------------------------------


def test_cycle_for_truncates_to_the_hour() -> None:
    assert hrrr.cycle_for(datetime(2026, 1, 2, 14, 37, 41, tzinfo=UTC)) == datetime(
        2026, 1, 2, 14, 0, tzinfo=UTC
    )


def test_cycle_for_converts_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert hrrr.cycle_for(datetime(2026, 1, 2, 15, 30, tzinfo=plus_two)) == datetime(
        2026, 1, 2, 13, 0, tzinfo=UTC
    )


def test_cycle_for_rejects_naive() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        hrrr.cycle_for(datetime(2026, 1, 2, 14, 0))  # deliberately naive


# --------------------------------------------------------------------------------------
# bbox_intersects_domain
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("box", "expected"),
    [
        ((41.0, 42.0, -97.0, -96.0), True),  # Nebraska — well inside
        ((36.0, 37.0, -122.5, -121.5), True),  # US west coast — inside
        ((51.0, 52.0, 0.0, 1.0), False),  # England
        ((-34.0, -33.0, 151.0, 152.0), False),  # Sydney
        ((20.0, 25.0, -80.0, -60.0), True),  # straddles the SE domain edge
    ],
)
def test_bbox_intersects_domain(box: tuple[float, float, float, float], expected: bool) -> None:
    assert hrrr.bbox_intersects_domain(*box) is expected


# --------------------------------------------------------------------------------------
# wind math
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("u", "v", "expected_from_deg"),
    [
        (1.0, 0.0, 270.0),  # blowing toward the east -> from the west
        (0.0, 1.0, 180.0),  # toward the north -> from the south
        (-1.0, 0.0, 90.0),  # toward the west -> from the east
        (0.0, -1.0, 0.0),  # toward the south -> from the north
    ],
)
def test_wind_direction_deg_met_convention(u: float, v: float, expected_from_deg: float) -> None:
    got = hrrr._wind_direction_deg(np.array([u]), np.array([v]))
    assert float(got[0]) == pytest.approx(expected_from_deg)


def test_wind_speed_ms_is_magnitude() -> None:
    got = hrrr._wind_speed_ms(np.array([3.0, 5.0]), np.array([4.0, 12.0]))
    assert list(np.round(got, 3)) == [5.0, 13.0]


# --------------------------------------------------------------------------------------
# regrid_to_mesh
# --------------------------------------------------------------------------------------


def test_regrid_to_mesh_shape_and_ascending_axes() -> None:
    lat_mesh, lon_mesh, values = hrrr.regrid_to_mesh(_native(), 40.5, 41.5, -97.5, -96.5, 16)

    assert lat_mesh.shape == lon_mesh.shape == values.shape == (16, 16)
    assert lat_mesh[0, 0] < lat_mesh[-1, 0]  # row 0 is the southern edge
    assert lon_mesh[0, 0] < lon_mesh[0, -1]  # column 0 is the western edge
    assert not np.isnan(values).any()
    # value == latitude in the fixture, so the mesh values track the mesh latitudes.
    assert np.allclose(values, lat_mesh, atol=1e-4)


def test_regrid_to_mesh_out_of_domain_raises() -> None:
    with pytest.raises(NWPUnavailableError):
        hrrr.regrid_to_mesh(
            _native(lat_range=(0.0, 1.0), lon_range=(10.0, 11.0)), 40.0, 41.0, -97.0, -96.0, 8
        )


# --------------------------------------------------------------------------------------
# nearest_sample
# --------------------------------------------------------------------------------------


def test_nearest_sample_returns_nearest_cell_value() -> None:
    value, direction = hrrr.nearest_sample(_native(), 41.0, -97.0)
    assert value == pytest.approx(41.0, abs=0.05)  # value == latitude in the fixture
    assert direction == pytest.approx(200.0)


def test_nearest_sample_temperature_has_no_direction() -> None:
    _, direction = hrrr.nearest_sample(_native(variable="temperature"), 41.0, -97.0)
    assert direction is None


def test_nearest_sample_off_grid_raises() -> None:
    with pytest.raises(NWPUnavailableError):
        hrrr.nearest_sample(_native(lat_range=(0.0, 1.0), lon_range=(10.0, 11.0)), 41.0, -97.0)


def test_haversine_km_one_degree_latitude() -> None:
    assert hrrr._haversine_km(40.0, -97.0, 41.0, -97.0) == pytest.approx(111.2, abs=1.0)
