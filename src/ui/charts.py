"""Plotly figure builders — the project's only source of chart construction.

UI layer (`CLAUDE.md` §4.1): every function here is a pure `build_*(...) -> go.Figure` taking
already-shaped data (a `pandas.DataFrame` or domain dataclasses) and returning a figure.
Dashboard modules (`src/ui/dashboards/`) call these; they never construct a `go.Trace`
themselves. This keeps every figure testable without a Streamlit runtime and keeps chart
styling centralized in one place.
"""

from collections.abc import Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from scipy.stats import linregress

import config
from src.domain.models import HealthStatus, PointForecast, compass_point

# --------------------------------------------------------------------------------------
# Power time series
# --------------------------------------------------------------------------------------


def build_power_timeseries(df: pd.DataFrame, title: str) -> go.Figure:
    """Build a fleet/farm/turbine power time series line chart.

    Args:
        df: Columns `bucket_start` (tz-aware UTC datetime) and `power_kw` (float, `NaN` for
            buckets with no telemetry — see `src/data/queries.py::get_power_timeseries`).
        title: Chart title.

    Returns:
        A `go.Figure` with a single line trace. `connectgaps=False` so missing buckets render
        as a genuine visual gap rather than an interpolated line (`PROJECT_SPEC.md` §11).
    """
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["bucket_start"],
            y=df["power_kw"],
            mode="lines",
            connectgaps=False,
            name="Power (kW)",
        )
    )
    fig.update_layout(
        title=title,
        xaxis_title="Time (UTC)",
        yaxis_title="Power (kW)",
        template=config.PLOTLY_TEMPLATE,
        height=config.CHART_HEIGHT_PX,
        margin=config.CHART_MARGIN,
    )
    return fig


# --------------------------------------------------------------------------------------
# Wind rose
# --------------------------------------------------------------------------------------


def _direction_bin(degrees: float) -> int:
    """Map a bearing to its 16-point compass sector index, matching `compass_point`."""
    return round((degrees % 360) / 22.5) % len(config.COMPASS_POINTS)


def _binned_mean_speed(history: Sequence[PointForecast]) -> list[float]:
    """Average wind speed per 16-point compass sector; `0.0` for sectors with no history."""
    sums = [0.0] * len(config.COMPASS_POINTS)
    counts = [0] * len(config.COMPASS_POINTS)
    for forecast in history:
        sector = _direction_bin(forecast.wind_direction_deg)
        sums[sector] += forecast.wind_speed_ms
        counts[sector] += 1
    return [sums[i] / counts[i] if counts[i] else 0.0 for i in range(len(sums))]


def build_wind_rose(current: PointForecast, history: Sequence[PointForecast]) -> go.Figure:
    """Build a 16-point wind rose: previous 24h in gray behind the current hour's petal.

    Args:
        current: The current hour's forecast — drawn as a single colored petal.
        history: The previous 24 hours' forecasts — averaged per compass sector and drawn
            in gray, behind the current petal (`PROJECT_SPEC.md` §10.3).

    Returns:
        A `go.Figure` with exactly two `go.Barpolar` traces, each with 16 angular bins.
    """
    history_r = _binned_mean_speed(history)
    current_r = [0.0] * len(config.COMPASS_POINTS)
    current_r[_direction_bin(current.wind_direction_deg)] = current.wind_speed_ms

    fig = go.Figure()
    fig.add_trace(
        go.Barpolar(
            r=history_r,
            theta=list(config.COMPASS_POINTS),
            name="Previous 24h",
            marker_color=config.WIND_ROSE_HISTORY_COLOR,
        )
    )
    fig.add_trace(
        go.Barpolar(
            r=current_r,
            theta=list(config.COMPASS_POINTS),
            name="Current hour",
            marker_color=config.WIND_ROSE_CURRENT_COLOR,
        )
    )
    readout = f"{current.wind_speed_ms:.1f} m/s {compass_point(current.wind_direction_deg)}"
    fig.update_layout(
        title=readout,
        polar={"angularaxis": {"direction": "clockwise", "rotation": 90}},
        template=config.PLOTLY_TEMPLATE,
        height=config.CHART_HEIGHT_PX,
        margin=config.CHART_MARGIN,
    )
    return fig


# --------------------------------------------------------------------------------------
# Scatter + regression
# --------------------------------------------------------------------------------------


def _insufficient_data_figure() -> go.Figure:
    """A figure carrying only an "Insufficient data" annotation, no traces."""
    fig = go.Figure()
    fig.add_annotation(
        text="Insufficient data",
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
        showarrow=False,
    )
    fig.update_layout(
        template=config.PLOTLY_TEMPLATE,
        height=config.CHART_HEIGHT_PX,
        margin=config.CHART_MARGIN,
    )
    return fig


def build_scatter_with_regression(
    df: pd.DataFrame, x_label: str, y_label: str, sampled_from: int | None
) -> go.Figure:
    """Build a scatter plot with an OLS regression line and R² annotation.

    Args:
        df: Columns `x` and `y` (float), e.g. from
            `src/data/queries.py::get_scatter_data`.
        x_label: X-axis title.
        y_label: Y-axis title.
        sampled_from: When the caller down-sampled the data, the original point count before
            sampling; renders a "Showing N of M points" subtitle so truncation is never
            silent. `None` when no down-sampling occurred.

    Returns:
        A `go.Figure`. Fewer than `config.SCATTER_MIN_REGRESSION_POINTS` rows returns a figure
        containing only an "Insufficient data" annotation, with no traces.
    """
    if len(df) < config.SCATTER_MIN_REGRESSION_POINTS:
        return _insufficient_data_figure()

    x = df["x"].to_numpy(dtype=float)
    y = df["y"].to_numpy(dtype=float)
    regression = linregress(x, y)
    line_x = np.array([x.min(), x.max()])
    line_y = regression.slope * line_x + regression.intercept

    fig = go.Figure()
    fig.add_trace(go.Scattergl(x=x, y=y, mode="markers", name="Observations"))
    # Hidden until the operator clicks its legend entry — keeps the raw scatter uncluttered
    # while the R² annotation still reports the fit quality up front.
    fig.add_trace(
        go.Scatter(x=line_x, y=line_y, mode="lines", name="OLS fit", visible="legendonly")
    )
    fig.add_annotation(
        text=f"R² = {regression.rvalue**2:.3f}",
        xref="paper",
        yref="paper",
        x=0.02,
        y=0.98,
        showarrow=False,
    )
    if sampled_from is not None:
        fig.add_annotation(
            text=f"Showing {len(df):,} of {sampled_from:,} points",
            xref="paper",
            yref="paper",
            x=0.02,
            y=0.90,
            showarrow=False,
        )
    fig.update_layout(
        xaxis_title=x_label,
        yaxis_title=y_label,
        template=config.PLOTLY_TEMPLATE,
        height=config.CHART_HEIGHT_PX,
        margin=config.CHART_MARGIN,
    )
    return fig


# --------------------------------------------------------------------------------------
# Status bar
# --------------------------------------------------------------------------------------


def build_status_bar(counts: dict[HealthStatus, int]) -> go.Figure:
    """Build a single horizontal stacked bar of health status counts.

    Args:
        counts: Turbine count per `HealthStatus`, as from `health.status_counts`.

    Returns:
        A `go.Figure` with one segment per `HealthStatus`, colored from `config.HEALTH_COLORS`.
    """
    fig = go.Figure()
    for status in HealthStatus:
        count = counts.get(status, 0)
        fig.add_trace(
            go.Bar(
                x=[count],
                y=["Fleet"],
                orientation="h",
                name=status.value,
                marker_color=config.HEALTH_COLORS[status.value],
                text=[str(count)],
                textposition="inside",
            )
        )
    fig.update_layout(
        barmode="stack",
        showlegend=True,
        xaxis_title="Turbines",
        yaxis={"showticklabels": False},
        template=config.PLOTLY_TEMPLATE,
        height=config.CHART_HEIGHT_PX,
        margin=config.CHART_MARGIN,
    )
    return fig
