"""Tests for src/ui/charts.py: figure shape, NaN-gap preservation, and color fidelity."""

import math
from datetime import UTC, datetime

import pandas as pd
import plotly.graph_objects as go
import pytest

import config
from src.domain.models import HealthStatus, PointForecast
from src.ui import charts

# --------------------------------------------------------------------------------------
# build_power_timeseries
# --------------------------------------------------------------------------------------


def _timeseries_df_with_gap() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "bucket_start": [datetime(2026, 1, 1, hour, tzinfo=UTC) for hour in range(4)],
            "power_kw": [100.0, float("nan"), 300.0, 400.0],
        }
    )


def test_build_power_timeseries_returns_figure() -> None:
    fig = charts.build_power_timeseries(_timeseries_df_with_gap(), "Fleet Power")

    assert isinstance(fig, go.Figure)


def test_build_power_timeseries_preserves_nan_gap_and_disables_connectgaps() -> None:
    fig = charts.build_power_timeseries(_timeseries_df_with_gap(), "Fleet Power")

    assert fig.data[0].connectgaps is False
    y_values = list(fig.data[0].y)
    assert math.isnan(y_values[1])
    assert y_values[0] == 100.0
    assert y_values[3] == 400.0


# --------------------------------------------------------------------------------------
# build_wind_rose
# --------------------------------------------------------------------------------------


def _current_and_history() -> tuple[PointForecast, list[PointForecast]]:
    current = PointForecast(
        valid_time=datetime(2026, 1, 2, 14, tzinfo=UTC),
        wind_speed_ms=7.6,
        wind_direction_deg=337.5,  # NNW
        air_temp_c=10.0,
        is_simulated=True,
    )
    history = [
        PointForecast(
            valid_time=datetime(2026, 1, 2, hour, tzinfo=UTC),
            wind_speed_ms=5.0 + (hour % 3),
            wind_direction_deg=float((hour * 15) % 360),
            air_temp_c=8.0,
            is_simulated=True,
        )
        for hour in range(24)
    ]
    return current, history


def test_build_wind_rose_returns_figure() -> None:
    current, history = _current_and_history()

    fig = charts.build_wind_rose(current, history)

    assert isinstance(fig, go.Figure)


def test_build_wind_rose_has_two_traces_of_sixteen_bins() -> None:
    current, history = _current_and_history()

    fig = charts.build_wind_rose(current, history)

    assert len(fig.data) == 2
    for trace in fig.data:
        assert isinstance(trace, go.Barpolar)
        assert len(trace.theta) == 16
        assert len(trace.r) == 16


def test_build_wind_rose_current_petal_carries_current_speed() -> None:
    current, history = _current_and_history()

    fig = charts.build_wind_rose(current, history)

    current_trace = fig.data[1]
    assert max(current_trace.r) == pytest.approx(current.wind_speed_ms)


def test_build_wind_rose_title_includes_speed_and_compass_point() -> None:
    current, history = _current_and_history()

    fig = charts.build_wind_rose(current, history)

    assert "7.6 m/s" in fig.layout.title.text
    assert "NNW" in fig.layout.title.text


# --------------------------------------------------------------------------------------
# build_scatter_with_regression
# --------------------------------------------------------------------------------------


def _perfectly_linear_df(n: int = 10) -> pd.DataFrame:
    x = [float(i) for i in range(n)]
    y = [2.0 * v + 1.0 for v in x]
    return pd.DataFrame({"x": x, "y": y})


def test_build_scatter_with_regression_returns_figure() -> None:
    fig = charts.build_scatter_with_regression(_perfectly_linear_df(), "X", "Y", None)

    assert isinstance(fig, go.Figure)


def test_build_scatter_with_regression_r_squared_near_one_on_linear_fixture() -> None:
    fig = charts.build_scatter_with_regression(_perfectly_linear_df(), "X", "Y", None)

    annotation_texts = [a.text for a in fig.layout.annotations]
    r2_text = next(t for t in annotation_texts if "R²" in t)
    r2_value = float(r2_text.split("=")[1].strip())
    assert r2_value == pytest.approx(1.0, abs=1e-3)


def test_build_scatter_with_regression_states_sample_size_when_downsampled() -> None:
    fig = charts.build_scatter_with_regression(_perfectly_linear_df(), "X", "Y", sampled_from=500)

    annotation_texts = [a.text for a in fig.layout.annotations]
    assert any("Showing" in t and "10" in t and "500" in t for t in annotation_texts)


def test_build_scatter_with_regression_omits_sample_annotation_when_not_downsampled() -> None:
    fig = charts.build_scatter_with_regression(_perfectly_linear_df(), "X", "Y", None)

    annotation_texts = [a.text for a in fig.layout.annotations]
    assert not any("Showing" in t for t in annotation_texts)


def test_build_scatter_with_regression_insufficient_data_below_minimum_points() -> None:
    df = pd.DataFrame({"x": [1.0, 2.0], "y": [1.0, 2.0]})

    fig = charts.build_scatter_with_regression(df, "X", "Y", None)

    assert len(fig.data) == 0
    annotation_texts = [a.text for a in fig.layout.annotations]
    assert annotation_texts == ["Insufficient data"]


def test_build_scatter_with_regression_empty_df_does_not_raise() -> None:
    df = pd.DataFrame({"x": [], "y": []})

    fig = charts.build_scatter_with_regression(df, "X", "Y", None)

    assert isinstance(fig, go.Figure)
    assert len(fig.data) == 0


# --------------------------------------------------------------------------------------
# build_status_bar
# --------------------------------------------------------------------------------------


def test_build_status_bar_returns_figure() -> None:
    counts = {
        HealthStatus.HEALTHY: 5,
        HealthStatus.WARNING: 2,
        HealthStatus.CRITICAL: 1,
        HealthStatus.ERROR: 0,
    }

    fig = charts.build_status_bar(counts)

    assert isinstance(fig, go.Figure)


def test_build_status_bar_colors_match_config() -> None:
    counts = {
        HealthStatus.HEALTHY: 5,
        HealthStatus.WARNING: 2,
        HealthStatus.CRITICAL: 1,
        HealthStatus.ERROR: 0,
    }

    fig = charts.build_status_bar(counts)

    colors_by_status = {trace.name: trace.marker.color for trace in fig.data}
    for status in HealthStatus:
        assert colors_by_status[status.value] == config.HEALTH_COLORS[status.value]


def test_build_status_bar_counts_appear_as_text() -> None:
    counts = {
        HealthStatus.HEALTHY: 5,
        HealthStatus.WARNING: 2,
        HealthStatus.CRITICAL: 1,
        HealthStatus.ERROR: 0,
    }

    fig = charts.build_status_bar(counts)

    text_by_status = {trace.name: trace.text[0] for trace in fig.data}
    assert text_by_status[HealthStatus.HEALTHY.value] == "5"
    assert text_by_status[HealthStatus.ERROR.value] == "0"
