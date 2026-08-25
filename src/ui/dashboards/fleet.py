"""Fleet Dashboard (`PROJECT_SPEC.md` §10.2) — the default view on load.

UI layer (`CLAUDE.md` §4.1): composes `src.domain.aggregates`/`clock` and `src.data.queries`
into the four headline metrics, the fleet health-status bar, and the fleet power time series.
No computation happens here beyond picking the right time window and formatting a number for
display — every figure is either already computed by `aggregates.build_fleet_summary` or
produced in SQL by `queries.get_power_timeseries`.
"""

from datetime import datetime

import duckdb
import streamlit as st

import config
from config import Settings
from src.data import queries
from src.domain import aggregates, clock
from src.domain.models import Level
from src.ui import charts, state


def render(con: duckdb.DuckDBPyConnection, settings: Settings, now: datetime) -> None:
    """Render the Fleet Dashboard into the current Streamlit container.

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings (staleness threshold).
        now: The resolved "now" (`clock.get_now`).
    """
    summary = aggregates.build_fleet_summary(con, settings, now)

    st.markdown("#### Fleet")
    metric_cols = st.columns(4)
    metric_cols[0].metric("Current Power Output", _format_power_kw(summary.current_power_kw))
    metric_cols[1].metric("Total Energy (MWh)", f"{summary.total_energy_mwh:,.1f}")
    metric_cols[2].metric("Total Farms", summary.farm_count)
    metric_cols[3].metric("Total Turbines", summary.turbine_count)

    st.plotly_chart(
        charts.build_status_bar(summary.status_counts),
        use_container_width=True,
        config={"displayModeBar": False},
    )

    window = state.get_history_window()
    start = clock.window_start(now, window)
    bucket = config.BUCKET_BY_WINDOW[window]
    timeseries_df = queries.get_power_timeseries(
        con,
        level=Level.FLEET,
        entity_id=None,
        start=start,
        end=now,
        bucket=bucket,
        max_points=config.MAX_TIMESERIES_POINTS,
    )
    st.plotly_chart(
        charts.build_power_timeseries(timeseries_df, "Fleet Power Output"),
        use_container_width=True,
        config={"displayModeBar": False},
    )


def _format_power_kw(power_kw: float) -> str:
    """Format a power figure as `"X,XXX kW"`, switching to MW above `config.MW_DISPLAY_THRESHOLD_KW`.

    Args:
        power_kw: Power in kilowatts.

    Returns:
        A display-ready string with the appropriate unit (`PROJECT_SPEC.md` §10.2).
    """
    if power_kw > config.MW_DISPLAY_THRESHOLD_KW:
        return f"{power_kw / config.KW_PER_MW:,.1f} MW"
    return f"{power_kw:,.0f} kW"
