"""Streamlit entrypoint (`CLAUDE.md` §4.1). Wiring and page config only — no business logic.

Connects to DuckDB, resolves "now", injects CSS, initializes session state, and renders the
map/dashboard shell: the fleet map, farm drill-down and turbine layer (`src/ui/map_view.py`),
and the Fleet/Farm/Turbine Dashboards (`src/ui/dashboards/`).
"""

import logging
from datetime import datetime
from typing import Literal

import duckdb
import streamlit as st
from streamlit_folium import st_folium

import config
from src.data import db
from src.domain import aggregates, clock, geo, nwp
from src.domain.models import Bounds, GridField, Level
from src.errors import WindFleetError
from src.ui import layout, map_view, state
from src.ui.dashboards import farm as farm_dashboard
from src.ui.dashboards import fleet as fleet_dashboard
from src.ui.dashboards import turbine as turbine_dashboard

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

st.set_page_config(
    page_title="Wind Fleet Monitor",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Set by `get_connection()` only on the one process-lifetime call where ingest actually runs;
# `None` otherwise (including "already current" on every rerun after the first). This is
# process-wide data, not per-session Streamlit state, and belongs alongside the
# `st.cache_resource` connection it describes rather than in `src/ui/state.py`'s contract.
_last_ingest_summary: db.IngestSummary | None = None


@st.cache_resource
def get_connection() -> duckdb.DuckDBPyConnection:
    """Open (or reuse) the cached DuckDB connection, ingesting source CSVs if needed.

    Cached process-wide via `st.cache_resource` (`CLAUDE.md` §5.4) so ingest runs at most once
    per process, not once per script rerun.

    Returns:
        An open, ingested DuckDB connection.

    Raises:
        DataLoadError: A source CSV is missing or malformed (propagates from `db.ingest`).
    """
    global _last_ingest_summary
    settings = config.load_settings()
    con = db.connect(settings)
    if not db.is_ingest_current(con, settings):
        _last_ingest_summary = db.ingest(con, settings)
    return con


def _render_ingest_summary() -> None:
    """Render the collapsed ingest-summary expander, degrading gracefully if nothing ran."""
    with st.expander("Ingest summary"):
        if _last_ingest_summary is None:
            st.write("Data already ingested for the current source files; nothing to report.")
            return
        summary = _last_ingest_summary
        st.write(f"Farms: {summary.farms}")
        st.write(f"Turbines: {summary.turbines}")
        st.write(f"Telemetry rows: {summary.telemetry_rows}")
        st.write(f"Duplicates removed: {summary.duplicates_removed}")
        st.write(f"Rows with nulls: {summary.rows_with_nulls}")
        st.write(f"Telemetry range: {summary.telemetry_start} — {summary.telemetry_end}")
        st.write(f"Elapsed: {summary.elapsed_seconds:.3f}s")


def _render_map_controls(now: datetime, bounds: Bounds) -> dict[str, GridField]:
    """Render the top-right Wind / Temperature / Forecast layer checkboxes (`PROJECT_SPEC.md` §8.4).

    Checking Wind or Temperature lazily fetches a synthetic grid from the NWP provider and
    caches it via `state.set_nwp_cached`; unchecking hides the overlay without discarding that
    cache, so re-checking is instant, and the cache dies with the rest of `nwp_cache` on a page
    refresh. Forecasted power output is the documented v1 ToDo (`PROJECT_SPEC.md` §8.4) — its
    checkbox state is recorded here but never produces a map overlay; `_render_dashboard`
    reads it back to show the placeholder message instead.

    Args:
        now: The resolved "now" (`clock.get_now`); part of the overlay cache key.
        bounds: The current view's bounds — overlays are fetched over exactly this box.

    Returns:
        The `GridField` overlays to render this rerun, keyed by variable name; populated only
        for currently-checked layers.
    """
    overlays: dict[str, GridField] = {}
    with st.container(key="map-controls"):
        wind_checked = st.checkbox(
            config.MAP_CONTROLS_LABELS["wind"], value=state.get_layer("wind"), key="layer-wind"
        )
        if wind_checked != state.get_layer("wind"):
            state.set_layer("wind", wind_checked)
        if wind_checked:
            overlays["wind"] = _get_cached_grid(now, bounds, "wind")
            st.caption(config.MAP_LAYER_SIMULATED_CAPTION)

        temperature_checked = st.checkbox(
            config.MAP_CONTROLS_LABELS["temperature"],
            value=state.get_layer("temperature"),
            key="layer-temperature",
        )
        if temperature_checked != state.get_layer("temperature"):
            state.set_layer("temperature", temperature_checked)
        if temperature_checked:
            overlays["temperature"] = _get_cached_grid(now, bounds, "temperature")
            st.caption(config.MAP_LAYER_SIMULATED_CAPTION)

        forecast_checked = st.checkbox(
            config.MAP_CONTROLS_LABELS["forecast"],
            value=state.get_layer("forecast"),
            key="layer-forecast",
        )
        if forecast_checked != state.get_layer("forecast"):
            state.set_layer("forecast", forecast_checked)
    return overlays


def _get_cached_grid(
    now: datetime, bounds: Bounds, variable: Literal["wind", "temperature"]
) -> GridField:
    """Fetch one variable's `GridField` over `bounds`, or return the refresh-scoped cached copy.

    Args:
        now: The resolved "now" (`clock.get_now`); part of the cache key, so the grid refreshes
            whenever the dataset's own "now" moves rather than on real wall-clock time.
        bounds: The current view's bounds; part of the cache key, so drilling from the fleet
            into a farm fetches a new, farm-scoped grid instead of reusing the fleet's.
        variable: `"wind"` or `"temperature"`.

    Returns:
        The `GridField`, from `state.nwp_cache` if a prior rerun already fetched this exact
        `(variable, bounds, now)` combination, or freshly fetched (and cached) otherwise.
    """
    cache_key = (
        f"grid:{variable}:{bounds.lat_min:.4f},{bounds.lat_max:.4f},"
        f"{bounds.lon_min:.4f},{bounds.lon_max:.4f}:{now.isoformat()}"
    )
    cached = state.get_nwp_cached(cache_key)
    if isinstance(cached, GridField):
        return cached
    grid = nwp.get_provider().grid(bounds, variable, now)
    state.set_nwp_cached(cache_key, grid)
    return grid


def _render_map(con: duckdb.DuckDBPyConnection, settings: config.Settings, now: datetime) -> None:
    """Render the fleet map and its Reset View button (`PROJECT_SPEC.md` §8, §8.5).

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings (staleness threshold).
        now: The resolved "now" (`clock.get_now`).
    """
    farm_rows = aggregates.build_farm_map_rows(con, settings, now)
    level = state.get_level()
    selected_farm_id = state.get_selected_farm_id()
    selected_farm_row = next(
        (row for row in farm_rows if row.farm.farm_id == selected_farm_id), None
    )

    if level == Level.FLEET or selected_farm_row is None:
        bounds = geo.fleet_bounds(con)
        turbine_rows = None
    else:
        bounds = geo.farm_view_bounds(con, selected_farm_row.farm.farm_id, selected_farm_row.farm)
        turbine_rows = aggregates.build_turbine_map_rows(
            con, settings, now, selected_farm_row.farm.farm_id
        )

    if bounds is None:
        # Not one of PROJECT_SPEC.md §11's enumerated bad-data cases, but the same principle:
        # degrade gracefully at render time rather than crash (CLAUDE.md §5.3).
        st.info("No farms to display.")
        return

    overlays = _render_map_controls(now, bounds)

    padding = layout.viewport_padding(state.get_is_mobile())
    fleet_map = map_view.build_map(
        farm_rows,
        turbine_rows,
        bounds,
        level,
        selected_farm_id,
        state.get_selected_turbine_id(),
        padding,
        overlays,
    )

    # Restricting `returned_objects` to just the click-related keys is required: st_folium's
    # default payload also includes `bounds`/`zoom`/`center`, which change on every pan and
    # zoom and would trigger a Streamlit rerun on every one of those interactions.
    map_return = st_folium(
        fleet_map,
        use_container_width=True,
        height=config.MAP_HEIGHT_PX,
        returned_objects=["last_object_clicked_popup", "last_object_clicked_tooltip"],
    )
    clicked = map_view.extract_clicked_id(map_return)
    if clicked is not None:
        kind, clicked_id = clicked
        # Guarded rerun (CLAUDE.md §5.1): only mutate state and rerun when the click actually
        # changes the selection, or re-clicking the same marker would loop forever.
        if kind == "farm" and clicked_id != state.get_selected_farm_id():
            state.select_farm(clicked_id)
            st.rerun()
        elif kind == "turbine" and clicked_id != state.get_selected_turbine_id():
            state.select_turbine(clicked_id)
            st.rerun()

    with st.container(key="reset-view"):
        # Guarded: a Reset click while already at fleet level mutates nothing and reruns nothing.
        if st.button("Reset View") and state.get_level() != Level.FLEET:
            state.reset_view()
            st.rerun()


def _render_dashboard(
    con: duckdb.DuckDBPyConnection, settings: config.Settings, now: datetime
) -> None:
    """Render the level-appropriate dashboard (`PROJECT_SPEC.md` §10).

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings (staleness threshold).
        now: The resolved "now" (`clock.get_now`).
    """
    if state.get_layer("forecast"):
        # ToDo placeholder only (PROJECT_SPEC.md §8.4): no download, model, or spinner — just
        # this message, and nothing added to the map.
        st.info(config.FORECAST_TODO_MESSAGE)

    level = state.get_level()
    selected_farm_id = state.get_selected_farm_id()
    if level == Level.FLEET or selected_farm_id is None:
        fleet_dashboard.render(con, settings, now)
        return
    selected_turbine_id = state.get_selected_turbine_id()
    if level == Level.TURBINE and selected_turbine_id is not None:
        turbine_dashboard.render(con, settings, now, selected_turbine_id)
        return
    farm_dashboard.render(con, settings, now, selected_farm_id)


def main() -> None:
    """Render the app shell: header, ingest summary, the map, and the level-appropriate dashboard."""
    try:
        settings = config.load_settings()
        con = get_connection()
        now = clock.get_now(con, settings)

        layout.inject_css()
        state.init_state()
        map_container, dashboard_container = layout.render_shell()

        st.markdown(f"### Wind Fleet Monitor — Data as of: {now:%Y-%m-%d %H:%M} UTC")
        _render_ingest_summary()

        with map_container:
            _render_map(con, settings, now)
        with dashboard_container:
            _render_dashboard(con, settings, now)
    except WindFleetError as exc:
        logger.exception("Failed to start Wind Fleet Monitor.")
        st.error(str(exc))
        st.stop()


main()
