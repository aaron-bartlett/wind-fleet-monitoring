"""Turbine Dashboard (`PROJECT_SPEC.md` §10.4) — the operator's diagnostic view.

UI layer (`CLAUDE.md` §4.1): composes `src.domain.aggregates`/`clock`/`nwp` and
`src.data.queries` into the status chip, itemized breach list, raw telemetry readout, NWP
block, and the historical scatter with its two dropdowns. No computation happens here beyond
formatting, picking the right time window, and the two pure data-prep helpers
(`_scatter_y_metric`, `_breach_severity_by_metric`) — every figure is either already computed
by `aggregates.build_turbine_summary` or produced in SQL/`nwp` by their respective modules.
"""

import logging
from datetime import datetime
from typing import cast

import duckdb
import streamlit as st

import config
from config import Settings
from src.data import queries
from src.domain import aggregates, clock
from src.domain.models import (
    Farm,
    HealthResult,
    HealthStatus,
    PointForecast,
    Severity,
    TelemetryRecord,
    compass_point,
)
from src.domain.nwp import get_provider
from src.errors import NWPUnavailableError
from src.ui import charts, state

logger = logging.getLogger(__name__)


def render(
    con: duckdb.DuckDBPyConnection, settings: Settings, now: datetime, turbine_id: str
) -> None:
    """Render the Turbine Dashboard into the current Streamlit container.

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings (staleness threshold).
        now: The resolved "now" (`clock.get_now`).
        turbine_id: The turbine to render.
    """
    summary = aggregates.build_turbine_summary(con, settings, now, turbine_id)

    if st.button("◀ Back to Farm"):
        state.select_farm(summary.farm.farm_id)
        st.rerun()

    st.markdown(f"#### {summary.turbine.turbine_id} ({summary.farm.farm_name})")
    st.caption(f"{summary.turbine.latitude:.4f}, {summary.turbine.longitude:.4f}")
    st.caption(f"{summary.local_time:%H:%M} {summary.tz_label} ({now:%H:%M} UTC)")

    st.markdown("**Health Status**")
    _render_status_chip(summary.health)
    _render_breach_list(summary.health)

    st.markdown("**Telemetry Data**")
    if summary.record is None:
        st.info("No telemetry received.")
    else:
        _render_telemetry(summary.record, summary.health)

    st.markdown("**NWP Forecast Data**")
    _render_weather(now, summary.farm)

    st.markdown("**Historical Data**")
    if summary.record is None:
        st.info("No historical telemetry available for this turbine.")
    else:
        _render_historical(con, now, turbine_id)


# --------------------------------------------------------------------------------------
# Health status & breach list
# --------------------------------------------------------------------------------------


def _render_status_chip(health: HealthResult) -> None:
    """Render a large colored status pill (`PROJECT_SPEC.md` §10.4)."""
    st.markdown(
        f"<span style='background:{health.color};color:white;padding:4px 14px;"
        f"border-radius:6px;font-size:1.1rem;font-weight:bold'>{health.status.value}</span>",
        unsafe_allow_html=True,
    )


def _render_breach_list(health: HealthResult) -> None:
    """Render one line per breach/error reason — the operator's actionable content.

    Never collapsed to a single word (`PROJECT_SPEC.md` §10.4): an `ERROR` turbine lists its
    `errors` reasons verbatim; any other status lists every `major` breach, then every `minor`
    breach, each as its full `Breach.message`.
    """
    if health.status is HealthStatus.ERROR:
        for reason in health.errors:
            st.markdown(f"⚠️ {reason}")
        return
    if not health.major and not health.minor:
        st.caption("No breaches — all metrics nominal.")
        return
    for breach in health.major:
        st.markdown(f"🔴 **Major** — {breach.message}")
    for breach in health.minor:
        st.markdown(f"🟠 **Minor** — {breach.message}")


def _breach_severity_by_metric(health: HealthResult) -> dict[str, Severity]:
    """Map each breached metric to its severity, for the telemetry block's per-metric dot.

    Args:
        health: A turbine's classified `HealthResult`.

    Returns:
        `{metric: severity}` for every breached metric. Empty for `HEALTHY` or `ERROR` results
        (the latter never reaches breach collection — `health.py`'s `classify` short-circuits
        before it). A metric breaches at most once (`health.py`'s invariant); if both were
        somehow present, major wins.
    """
    severities: dict[str, Severity] = {breach.metric: breach.severity for breach in health.minor}
    severities.update({breach.metric: breach.severity for breach in health.major})
    return severities


def _dot_color(severity: Severity | None) -> str:
    """The telemetry block's per-metric breach-status dot color."""
    if severity is Severity.MAJOR:
        return config.HEALTH_COLORS["Critical"]
    if severity is Severity.MINOR:
        return config.HEALTH_COLORS["Warning"]
    return config.HEALTH_COLORS["Healthy"]


# --------------------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------------------


def _render_telemetry(record: TelemetryRecord, health: HealthResult) -> None:
    """Render the five telemetry metrics, each with a colored breach-status dot.

    Also shows the record's `timestamp` and ingest lag so the operator can judge freshness
    (`PROJECT_SPEC.md` §10.4).
    """
    severity_by_metric = _breach_severity_by_metric(health)
    for metric in config.METRICS:
        value = record.get(metric)
        # A metric can still be None here on a record that exists but has one invalid field
        # (health.status == ERROR from an out-of-range/NULL metric, as opposed to no record at
        # all) — degrade at render time rather than crash on f"{None:.1f}" (CLAUDE.md §5.3).
        value_str = f"{value:.1f}" if value is not None else "—"
        dot_color = _dot_color(severity_by_metric.get(metric))
        st.markdown(
            f"<span style='color:{dot_color}'>●</span> **{config.METRIC_LABELS[metric]}**: "
            f"{value_str}",
            unsafe_allow_html=True,
        )
    st.caption(
        f"Recorded {record.timestamp:%Y-%m-%d %H:%M} UTC · ingest lag {record.lag_minutes:.0f} min"
    )


# --------------------------------------------------------------------------------------
# NWP weather block — construction mirrors dashboards/farm.py, using the farm coordinate
# (PROJECT_SPEC.md §10.4: "wind speed & direction ... using the farm coordinate").
# --------------------------------------------------------------------------------------


def _render_weather(now: datetime, farm: Farm) -> None:
    """Render the wind/temperature weather block, identical construction to the Farm Dashboard.

    Shows `config.NWP_UNAVAILABLE_MESSAGE` in place of the block on `NWPUnavailableError`,
    never raising (`CLAUDE.md` §5.3).
    """
    weather = _get_weather(now, farm)
    if weather is None:
        st.info(config.NWP_UNAVAILABLE_MESSAGE)
        return

    current, history = weather
    readout = f"{current.wind_speed_ms:.1f} m/s {compass_point(current.wind_direction_deg)}"
    st.write(readout)
    st.plotly_chart(
        charts.build_wind_rose(current, history),
        use_container_width=True,
        config={"displayModeBar": False},
    )
    st.write(f"Air Temperature: {_format_temp_c(current.air_temp_c)}")
    st.caption(_weather_caption(current))


def _weather_caption(current: PointForecast) -> str:
    """The "Simulated" note for stub data, else the source, AGL levels, and valid time of HRRR."""
    if current.is_simulated:
        return config.WEATHER_SIMULATED_CAPTION
    return (
        f"{config.HRRR_SOURCE_LABEL} {config.HRRR_WIND_LEVEL_LABEL} wind / "
        f"{config.HRRR_TEMP_LEVEL_LABEL} temp · valid {current.valid_time:%Y-%m-%d %H:%MZ}"
    )


def _get_weather(now: datetime, farm: Farm) -> tuple[PointForecast, list[PointForecast]] | None:
    """Fetch (and cache for this rerun cycle) the farm's current + previous-24h forecasts.

    Deliberately reuses `dashboards/farm.py`'s exact cache key (`f"farm:{farm_id}:{now...}"`)
    rather than a turbine-scoped one: wind/temperature are farm-scoped, shared by every turbine
    on that farm (`PROJECT_SPEC.md` §9), so switching between the Farm Dashboard and any of its
    turbines' dashboards hits the same `nwp_cache` entry instead of recomputing per turbine.

    Returns:
        `(current, history)`, or `None` when the provider raised `NWPUnavailableError`.
    """
    cache_key = f"farm:{farm.farm_id}:{now.isoformat()}"
    cached = state.get_nwp_cached(cache_key)
    if cached is not None:
        return cast("tuple[PointForecast, list[PointForecast]]", cached)

    provider = get_provider()
    history_start = clock.window_start(now, "24h")
    assert history_start is not None  # "24h" always has a concrete window (config.TIME_WINDOWS)
    try:
        current = provider.point_forecast(farm.latitude, farm.longitude, now)
        history = provider.point_history(farm.latitude, farm.longitude, history_start, now)
    except NWPUnavailableError:
        logger.warning("NWP forecast unavailable for farm %s", farm.farm_id)
        return None
    state.set_nwp_cached(cache_key, (current, history))
    return current, history


def _format_temp_c(temp_c: float) -> str:
    """Format an air temperature in °C with °F alongside (`PROJECT_SPEC.md` §10.3/§10.4)."""
    temp_f = temp_c * config.FAHRENHEIT_SCALE + config.FAHRENHEIT_OFFSET
    return f"{temp_c:.1f}°C ({temp_f:.1f}°F)"


# --------------------------------------------------------------------------------------
# Historical scatter — two dropdowns bound to state.py, no st.rerun() needed: a native
# st.selectbox already triggers Streamlit's own rerun on change, so this module only has to
# keep state.py as the single source of truth for the value across reruns (CLAUDE.md §5.1).
# --------------------------------------------------------------------------------------


def _scatter_y_metric(x_metric: str) -> str:
    """The historical scatter's y-axis metric (`PROJECT_SPEC.md` §10.4).

    Args:
        x_metric: The currently selected x-axis metric.

    Returns:
        `"power_output_kw"`, unless `x_metric` IS `"power_output_kw"` — in which case the
        y-axis switches to `"wind_speed_ms"` so the chart never plots a metric against itself.
    """
    return "wind_speed_ms" if x_metric == "power_output_kw" else "power_output_kw"


def _render_historical(con: duckdb.DuckDBPyConnection, now: datetime, turbine_id: str) -> None:
    """Render the x-axis/time-window dropdowns and the resulting scatter + regression chart."""
    metrics = list(config.METRICS)
    current_x = state.get_history_x_metric()
    selected_x = st.selectbox(
        "X-axis metric",
        options=metrics,
        index=metrics.index(current_x) if current_x in metrics else 0,
        format_func=lambda m: config.METRIC_LABELS[m],
    )
    if selected_x != current_x:
        state.set_history_x_metric(selected_x)

    windows = list(config.HISTORY_WINDOW_LABELS)
    current_window = state.get_history_window()
    selected_window = st.selectbox(
        "Time window",
        options=windows,
        index=windows.index(current_window) if current_window in windows else 0,
        format_func=lambda w: config.HISTORY_WINDOW_LABELS[w],
    )
    if selected_window != current_window:
        state.set_history_window(selected_window)

    y_metric = _scatter_y_metric(selected_x)
    start = clock.window_start(now, selected_window)
    df = queries.get_scatter_data(
        con,
        turbine_id=turbine_id,
        x_metric=selected_x,
        y_metric=y_metric,
        start=start,
        end=now,
        max_points=config.MAX_SCATTER_POINTS,
    )
    total = queries.get_scatter_sample_size(
        con, turbine_id=turbine_id, x_metric=selected_x, y_metric=y_metric, start=start, end=now
    )
    sampled_from = total if total > len(df) else None
    st.plotly_chart(
        charts.build_scatter_with_regression(
            df, config.METRIC_LABELS[selected_x], config.METRIC_LABELS[y_metric], sampled_from
        ),
        use_container_width=True,
        config={"displayModeBar": False},
    )
