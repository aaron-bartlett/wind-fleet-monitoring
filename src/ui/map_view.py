"""Builds the Folium fleet/farm/turbine map object (`CLAUDE.md` §4.1 — UI layer).

Deliberately imports only `folium`/`branca`, never `streamlit` — `build_map` is a pure
function of already-shaped data (`FarmMapRow`/`Turbine`+`HealthResult` pairs, a `Bounds`, the
current drill-down state) to a `folium.Map`, and `extract_clicked_id` is a pure parser of the
dict `st_folium` hands back. Both are testable without a Streamlit runtime
(`tests/test_smoke.py`). `app.py` is the only caller, and the only place `st_folium` itself is
invoked.
"""

import branca.colormap as bcm
import folium
from folium.raster_layers import ImageOverlay

import config
from src.domain.aggregates import FarmMapRow
from src.domain.models import Bounds, GridField, HealthResult, Level, Turbine

# Machine-readable ids are carried in each marker's popup, never its tooltip — the tooltip
# stays free-form human text (PROJECT_SPEC.md §8.2/§8.3) while `extract_clicked_id` only ever
# has to parse one fixed, unambiguous format. `st_folium` surfaces a clicked marker's popup
# content verbatim in `last_object_clicked_popup`.
_FARM_POPUP_PREFIX = "__farm__"
_TURBINE_POPUP_PREFIX = "__turbine__"

_FARM_ICON_PX = 32
_FARM_BADGE_PX = 14


def build_map(
    farm_rows: list[FarmMapRow],
    turbine_rows: list[tuple[Turbine, HealthResult]] | None,
    bounds: Bounds,
    level: Level,
    selected_farm_id: str | None,
    selected_turbine_id: str | None,
    padding: tuple[tuple[int, int], tuple[int, int]],
    overlays: dict[str, GridField],
) -> folium.Map:
    """Build the Folium map for the current drill-down level.

    Renders the fleet (farm-dot) layer always, plus the turbine layer when `level` is `FARM`
    or `TURBINE` (`PROJECT_SPEC.md` §8.2-8.3), plus any checked wind/temperature grid overlays
    (`PROJECT_SPEC.md` §8.4).

    Args:
        farm_rows: One marker's worth of data per farm, from `aggregates.build_farm_map_rows`.
        turbine_rows: One `(Turbine, HealthResult)` pair per turbine of the selected farm, from
            `aggregates.build_turbine_map_rows`; `None` (or ignored) at the fleet level.
        bounds: The lat/lon box to fit the map to (already padded/expanded by the caller).
        level: The current drill-down level; only `FARM`/`TURBINE` draw the turbine layer.
        selected_farm_id: The selected farm, dimmed rather than hidden for context once its
            turbine layer is showing.
        selected_turbine_id: The selected turbine, if any, drawn larger/thicker to stand out.
        padding: `(padding_top_left, padding_bottom_right)` pixel pairs for `fit_bounds`, so
            no marker is solved into the region the dashboard panel covers.
        overlays: The currently-checked wind/temperature `GridField`s, keyed by variable name
            (`app.py`'s layer checkboxes); absent keys simply render nothing.

    Returns:
        A `folium.Map` with the fleet layer (and turbine layer, when applicable) added and
        bounds fit.
    """
    # Not read at this drill level (see docstring); referenced so intent is explicit rather
    # than a silently-dropped parameter.
    del selected_farm_id

    fleet_map = folium.Map(tiles="CartoDB positron", zoom_control=True)
    padding_top_left, padding_bottom_right = padding
    fleet_map.fit_bounds(
        bounds.as_folium(),
        padding_top_left=padding_top_left,
        padding_bottom_right=padding_bottom_right,
    )

    show_turbines = level in (Level.FARM, Level.TURBINE)
    farm_opacity = config.FARM_MARKER_DIMMED_OPACITY if show_turbines else 1.0

    farms_layer = folium.FeatureGroup(name="farms")
    colormap = bcm.LinearColormap(config.FARM_SCORE_COLORMAP_STOPS, vmin=0.0, vmax=1.0)
    for row in farm_rows:
        _add_farm_marker(farms_layer, row, colormap, opacity=farm_opacity)
    farms_layer.add_to(fleet_map)

    if show_turbines and turbine_rows is not None:
        turbines_layer = folium.FeatureGroup(name="turbines")
        for turbine, result in turbine_rows:
            selected = turbine.turbine_id == selected_turbine_id
            _add_turbine_marker(turbines_layer, turbine, result, selected=selected)
        turbines_layer.add_to(fleet_map)

    if "wind" in overlays:
        _add_wind_overlay(fleet_map, overlays["wind"])
    if "temperature" in overlays:
        _add_temperature_overlay(fleet_map, overlays["temperature"])

    return fleet_map


def _add_farm_marker(
    farms_layer: folium.FeatureGroup,
    row: FarmMapRow,
    colormap: bcm.LinearColormap,
    *,
    opacity: float,
) -> None:
    """Add one farm's dot marker to `farms_layer` (`PROJECT_SPEC.md` §8.2)."""
    color = (
        colormap(row.health_score)
        if row.health_score is not None
        else config.HEALTH_COLORS["Error"]
    )

    tooltip = f"{row.farm.farm_name} ({row.farm.farm_id})"
    if row.alert_reason is not None:
        tooltip = f"{tooltip} — {row.alert_reason}"

    folium.Marker(
        location=[row.farm.latitude, row.farm.longitude],
        icon=folium.DivIcon(
            html=_farm_dot_html(
                color, row.turbine_count, alert=row.alert_reason is not None, opacity=opacity
            ),
            icon_size=(_FARM_ICON_PX, _FARM_ICON_PX),
            icon_anchor=(_FARM_ICON_PX // 2, _FARM_ICON_PX // 2),
        ),
        tooltip=tooltip,
        popup=folium.Popup(f"{_FARM_POPUP_PREFIX}{row.farm.farm_id}", parse_html=False),
    ).add_to(farms_layer)


def _farm_dot_html(color: str, turbine_count: int, *, alert: bool, opacity: float) -> str:
    """Return the `DivIcon` HTML for one farm dot: a colored circle with a centered count."""
    badge = ""
    if alert:
        badge = (
            f'<div style="position:absolute;top:-4px;right:-4px;width:{_FARM_BADGE_PX}px;'
            f"height:{_FARM_BADGE_PX}px;border-radius:50%;background:{config.HEALTH_COLORS['Critical']};"
            "color:white;font-size:10px;font-weight:bold;display:flex;align-items:center;"
            'justify-content:center;border:1px solid white;">!</div>'
        )
    return (
        f'<div style="position:relative;width:{_FARM_ICON_PX}px;height:{_FARM_ICON_PX}px;'
        f'opacity:{opacity};">'
        f'<div style="width:{_FARM_ICON_PX}px;height:{_FARM_ICON_PX}px;border-radius:50%;'
        f"background:{color};display:flex;align-items:center;justify-content:center;"
        "color:white;font-weight:bold;font-size:13px;border:2px solid white;"
        f'box-shadow:0 1px 3px rgba(0,0,0,0.4);">{turbine_count}</div>'
        f"{badge}</div>"
    )


def _add_turbine_marker(
    turbines_layer: folium.FeatureGroup,
    turbine: Turbine,
    result: HealthResult,
    *,
    selected: bool,
) -> None:
    """Add one turbine's discrete-health dot to `turbines_layer` (`PROJECT_SPEC.md` §8.3).

    The selected turbine (if any) renders larger and with a thicker stroke so it is visually
    distinguishable among its siblings.
    """
    radius = config.TURBINE_SELECTED_RADIUS_PX if selected else config.TURBINE_MARKER_RADIUS_PX
    weight = config.TURBINE_SELECTED_WEIGHT_PX if selected else config.TURBINE_MARKER_WEIGHT_PX
    folium.CircleMarker(
        location=[turbine.latitude, turbine.longitude],
        radius=radius,
        weight=weight,
        color=result.color,
        fill=True,
        fill_color=result.color,
        fill_opacity=config.TURBINE_MARKER_FILL_OPACITY,
        tooltip=f"{turbine.turbine_id} — {result.status.value}",
        popup=folium.Popup(f"{_TURBINE_POPUP_PREFIX}{turbine.turbine_id}", parse_html=False),
    ).add_to(turbines_layer)


def _add_wind_overlay(fleet_map: folium.Map, grid: GridField) -> None:
    """Render a wind-speed `GridField` (`PROJECT_SPEC.md` §8.4).

    SPEC-GAP: `IMPLEMENTATION_PLAN.md` Phase 14 names `folium.plugins.HeatMap` for this
    overlay, but that class's constructor ships with no type annotations in the pinned
    `folium~=0.18` release. `CLAUDE.md` §2.5's required mypy config makes `src/domain`/
    `src/data` strict via a per-module `strict = true` override — which, per a documented
    mypy limitation, is not actually module-scoped and instead enables `disallow_untyped_calls`
    for the whole project, flagging that constructor call everywhere. Rendered instead as a
    second `ImageOverlay` (`_add_grid_image_overlay`, shared with the temperature overlay
    below): still a smoothed, colored intensity surface — a "HeatMap-style representation" as
    the plan describes it — while keeping every call in this module fully typed, without
    weakening the project's required strict config or adding a `# type: ignore`.
    """
    _add_grid_image_overlay(fleet_map, grid)


def _add_temperature_overlay(fleet_map: folium.Map, grid: GridField) -> None:
    """Render a temperature `GridField` as a `folium.raster_layers.ImageOverlay` (`PROJECT_SPEC.md` §8.4)."""
    _add_grid_image_overlay(fleet_map, grid)


def _add_grid_image_overlay(fleet_map: folium.Map, grid: GridField) -> None:
    """Paint one `GridField` (wind speed or temperature) onto the map as a colored image.

    One pixel per grid cell, colored by `config.GRID_OVERLAY_COLORMAP_STOPS` scaled to this
    grid's own min/max — `branca`/`folium` apply the colormap callable to each raw cell value
    directly rather than a pre-normalized one, so `vmin`/`vmax` must be the data's actual range
    for the ramp to span it correctly.
    """
    value_min = float(grid.values.min())
    value_max = float(grid.values.max())
    if value_min == value_max:
        # A perfectly uniform grid would make LinearColormap divide by zero; widen the range
        # by an arbitrary epsilon rather than special-casing the render path.
        value_max = value_min + 1e-6
    colormap = bcm.LinearColormap(
        config.GRID_OVERLAY_COLORMAP_STOPS, vmin=value_min, vmax=value_max
    )
    image_bounds = [
        [float(grid.lats.min()), float(grid.lons.min())],
        [float(grid.lats.max()), float(grid.lons.max())],
    ]
    ImageOverlay(
        image=grid.values,
        bounds=image_bounds,
        origin="lower",
        colormap=colormap,
        opacity=config.MAP_LAYER_OVERLAY_OPACITY,
    ).add_to(fleet_map)


def extract_clicked_id(map_return: dict[str, object] | None) -> tuple[str, str] | None:
    """Parse the `(kind, entity_id)` embedded in a clicked marker's popup.

    Prefers `last_object_clicked_popup`, falling back to `last_object_clicked_tooltip`
    (`IMPLEMENTATION_PLAN.md` Phase 11) — in practice only the popup carries a parseable id
    (see module docstring), so the tooltip branch degrades to `None` rather than matching, but
    both keys are checked to honor the full contract. Never raises: a non-dict `map_return`, a
    missing key, or a non-string value at either key all fall through to `None`.

    Args:
        map_return: The dict `st_folium(...)` returns, or `None`.

    Returns:
        `("farm", farm_id)`, `("turbine", turbine_id)`, or `None` for a click on empty map
        area, an unparseable payload, or malformed/missing input.
    """
    if not isinstance(map_return, dict):
        return None
    for key in ("last_object_clicked_popup", "last_object_clicked_tooltip"):
        value = map_return.get(key)
        if not isinstance(value, str):
            continue
        if value.startswith(_FARM_POPUP_PREFIX):
            return "farm", value.removeprefix(_FARM_POPUP_PREFIX)
        if value.startswith(_TURBINE_POPUP_PREFIX):
            return "turbine", value.removeprefix(_TURBINE_POPUP_PREFIX)
    return None
