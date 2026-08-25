"""Farm Dashboard (`PROJECT_SPEC.md` §10.3) — a farm's turbines, weather, and power figures.

UI layer (`CLAUDE.md` §4.1): composes `src.domain.aggregates`/`clock`/`nwp` and
`src.data.queries` into the farm-scoped headline metrics, health counts, weather block, and
power time series. No computation happens here beyond formatting and picking the right time
window — every figure is either already computed by `aggregates.build_farm_summary` or
produced in SQL/`nwp` by their respective modules.
"""

from datetime import datetime
from typing import cast

import duckdb
import streamlit as st

import config
from config import Settings
from src.data import queries
from src.domain import aggregates, clock
from src.domain.models import Farm, HealthStatus, Level, PointForecast, compass_point
from src.domain.nwp import get_provider
from src.ui import charts, state


def render(con: duckdb.DuckDBPyConnection, settings: Settings, now: datetime, farm_id: str) -> None:
    """Render the Farm Dashboard into the current Streamlit container.

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings (staleness threshold).
        now: The resolved "now" (`clock.get_now`).
        farm_id: The farm to render.
    """
    summary = aggregates.build_farm_summary(con, settings, now, farm_id)

    if st.button("◀ Back to Fleet"):
        state.reset_view()
        st.rerun()

    st.markdown(f"#### {summary.farm.farm_name} ({summary.farm.farm_id})")
    st.caption(f"{summary.farm.latitude:.4f}, {summary.farm.longitude:.4f}")
    st.caption(f"{summary.local_time:%H:%M} {summary.tz_label} ({now:%H:%M} UTC)")

    metric_cols = st.columns(2)
    metric_cols[0].metric("Current Power Output", _format_power_kw(summary.current_power_kw))
    metric_cols[1].metric("Total Energy (MWh)", f"{summary.total_energy_mwh:,.1f}")

    _render_weather(now, summary.farm)

    st.markdown(f"**Turbines:** {summary.turbine_count}")
    if summary.turbine_count == 0:
        st.info("No turbines registered at this farm.")
    else:
        _render_status_counts(summary.status_counts)

    window = state.get_history_window()
    start = clock.window_start(now, window)
    bucket = config.BUCKET_BY_WINDOW[window]
    timeseries_df = queries.get_power_timeseries(
        con,
        level=Level.FARM,
        entity_id=farm_id,
        start=start,
        end=now,
        bucket=bucket,
        max_points=config.MAX_TIMESERIES_POINTS,
    )
    st.plotly_chart(
        charts.build_power_timeseries(timeseries_df, f"{summary.farm.farm_name} Power Output"),
        use_container_width=True,
        config={"displayModeBar": False},
    )


def _render_weather(now: datetime, farm: Farm) -> None:
    """Render the wind/temperature weather block (`PROJECT_SPEC.md` §10.3)."""
    current, history = _get_weather(now, farm)

    st.markdown("**Current Weather**")
    readout = f"{current.wind_speed_ms:.1f} m/s {compass_point(current.wind_direction_deg)}"
    st.write(readout)
    st.plotly_chart(
        charts.build_wind_rose(current, history),
        use_container_width=True,
        config={"displayModeBar": False},
    )
    st.write(f"Air Temperature: {_format_temp_c(current.air_temp_c)}")
    if current.is_simulated:
        st.caption("⚠ Simulated data — NWP provider not connected")


def _get_weather(now: datetime, farm: Farm) -> tuple[PointForecast, list[PointForecast]]:
    """Fetch (and cache for this rerun cycle) the farm's current + previous-24h forecasts.

    Cached under `state.nwp_cache` keyed by farm and resolved time so repeated Streamlit
    reruns for the same "now" do not recompute or re-request the forecast
    (`IMPLEMENTATION_PLAN.md` Phase 12; `PROJECT_SPEC.md` §8.4's cache lifecycle).
    """
    cache_key = f"farm:{farm.farm_id}:{now.isoformat()}"
    cached = state.get_nwp_cached(cache_key)
    if cached is not None:
        return cast("tuple[PointForecast, list[PointForecast]]", cached)

    provider = get_provider()
    history_start = clock.window_start(now, "24h")
    assert history_start is not None  # "24h" always has a concrete window (config.TIME_WINDOWS)
    current = provider.point_forecast(farm.latitude, farm.longitude, now)
    history = provider.point_history(farm.latitude, farm.longitude, history_start, now)
    state.set_nwp_cached(cache_key, (current, history))
    return current, history


def _render_status_counts(status_counts: dict[HealthStatus, int]) -> None:
    """Render the four health-status counts, each colored per `config.HEALTH_COLORS`."""
    status_cols = st.columns(len(HealthStatus))
    for col, status in zip(status_cols, HealthStatus, strict=True):
        color = config.HEALTH_COLORS[status.value]
        count = status_counts[status]
        col.markdown(
            f"<span style='color:{color};font-weight:bold'>{status.value}: {count}</span>",
            unsafe_allow_html=True,
        )


def _format_power_kw(power_kw: float) -> str:
    """Format a power figure as `"X,XXX kW"`, switching to MW above `config.MW_DISPLAY_THRESHOLD_KW`.

    Args:
        power_kw: Power in kilowatts.

    Returns:
        A display-ready string with the appropriate unit (`PROJECT_SPEC.md` §10.2, reused for
        the farm-scoped figure per §10.3).
    """
    if power_kw > config.MW_DISPLAY_THRESHOLD_KW:
        return f"{power_kw / config.KW_PER_MW:,.1f} MW"
    return f"{power_kw:,.0f} kW"


def _format_temp_c(temp_c: float) -> str:
    """Format an air temperature in °C with °F alongside (`PROJECT_SPEC.md` §10.3)."""
    temp_f = temp_c * config.FAHRENHEIT_SCALE + config.FAHRENHEIT_OFFSET
    return f"{temp_c:.1f}°C ({temp_f:.1f}°F)"
