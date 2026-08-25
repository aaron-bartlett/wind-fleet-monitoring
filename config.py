"""All constants, thresholds, colors, and settings for the Wind Fleet Monitor.

This module sits below every layer (`CLAUDE.md` §2.4): it holds data only — two frozen
dataclasses, module-level constants, and `load_settings()` / `POWER_CURVE_EXPECTED_KW`, which
are lookups rather than business logic. No SQL, no Streamlit, no domain rules live here; they
only ever read values from it.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.errors import ConfigError

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Threshold:
    """One metric's physical bounds and simple (unconditional) breach limits.

    Metrics whose breach rules depend on another metric (e.g. `power_output_kw` on
    `wind_speed_ms`) leave `minor_max`/`major_max` as `None` here; those rules are expressed
    instead as the conditional-rule constants below and applied directly in `health.py`.
    """

    metric: str
    physical_min: float
    physical_max: float
    minor_max: float | None = None  # breach when value > minor_max
    major_max: float | None = None  # breach when value > major_max
    minor_min: float | None = None  # breach when value < minor_min (conditional rules only)


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, environment-derived runtime configuration."""

    data_dir: Path
    duckdb_path: Path
    sim_now: datetime | None
    stale_after_minutes: int


def load_settings() -> Settings:
    """Read runtime settings from environment variables.

    Returns:
        A populated, immutable `Settings`.

    Raises:
        ConfigError: `SIM_NOW` or `STALE_AFTER_MINUTES` is set but cannot be parsed.
    """
    data_dir = Path(os.environ.get("DATA_DIR", "data"))

    duckdb_path_str = os.environ.get("DUCKDB_PATH")
    duckdb_path = Path(duckdb_path_str) if duckdb_path_str else data_dir / "fleet.duckdb"

    sim_now = _parse_sim_now(os.environ.get("SIM_NOW"))
    stale_after_minutes = _parse_stale_after_minutes(os.environ.get("STALE_AFTER_MINUTES"))

    return Settings(
        data_dir=data_dir,
        duckdb_path=duckdb_path,
        sim_now=sim_now,
        stale_after_minutes=stale_after_minutes,
    )


def _parse_sim_now(raw: str | None) -> datetime | None:
    """Parse the `SIM_NOW` environment variable to a tz-aware UTC datetime, if set."""
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError(f"SIM_NOW={raw!r} is not a valid ISO-8601 datetime.") from exc
    if parsed.tzinfo is None:
        raise ConfigError(f"SIM_NOW={raw!r} must include a UTC offset or 'Z' suffix.")
    return parsed.astimezone(UTC)


def _parse_stale_after_minutes(raw: str | None) -> int:
    """Parse the `STALE_AFTER_MINUTES` environment variable, defaulting to 15."""
    if raw is None:
        return 15
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"STALE_AFTER_MINUTES={raw!r} is not a valid integer.") from exc


# --------------------------------------------------------------------------------------
# Telemetry metrics
# --------------------------------------------------------------------------------------

METRICS: tuple[str, ...] = (
    "power_output_kw",
    "wind_speed_ms",
    "rotor_rpm",
    "blade_pitch_deg",
    "gearbox_temp_c",
)

METRIC_LABELS: dict[str, str] = {
    "power_output_kw": "Power Output (kW)",
    "wind_speed_ms": "Wind Speed (m/s)",
    "rotor_rpm": "Rotor Speed (RPM)",
    "blade_pitch_deg": "Blade Pitch (deg)",
    "gearbox_temp_c": "Gearbox Temp (C)",
}

# --------------------------------------------------------------------------------------
# Health thresholds (PROJECT_SPEC.md §6.2)
# --------------------------------------------------------------------------------------

THRESHOLDS: dict[str, Threshold] = {
    # power_output_kw's minor/major breach rules are wind-speed-conditional (see
    # POWER_* constants below); only its physically-possible range is expressed here.
    "power_output_kw": Threshold(
        metric="power_output_kw",
        physical_min=-50.0,
        physical_max=5000.0,
    ),
    "wind_speed_ms": Threshold(
        metric="wind_speed_ms",
        physical_min=0.0,
        physical_max=60.0,
        minor_max=25.0,
    ),
    "rotor_rpm": Threshold(
        metric="rotor_rpm",
        physical_min=0.0,
        physical_max=40.0,
        minor_max=18.5,
        major_max=22.0,
    ),
    "blade_pitch_deg": Threshold(
        metric="blade_pitch_deg",
        physical_min=-5.0,
        physical_max=95.0,
        minor_max=25.0,  # conditional: only a breach when power_output_kw > PITCH_CONDITIONAL_POWER_KW
        major_max=40.0,
    ),
    "gearbox_temp_c": Threshold(
        metric="gearbox_temp_c",
        physical_min=-40.0,
        physical_max=200.0,
        minor_max=95.0,
        major_max=110.0,
    ),
}

# Conditional-rule constants — used by health.py for rules a simple max cannot express.
CUT_IN_MS = 3.0
RATED_MS = 12.0
CUT_OUT_MS = 25.0
RATED_POWER_KW = 3500.0
POWER_UNDERPERFORM_FRACTION = 0.40  # minor breach when actual < 40% of curve expectation
POWER_CHECK_WIND_RANGE = (4.0, 15.0)  # window in which the underperformance rule applies
POWER_ZERO_WIND_RANGE = (4.0, 25.0)  # window in which zero power is a major breach
ROTOR_STALL_RPM = 0.5
ROTOR_STALL_WIND_RANGE = (4.0, 25.0)
PITCH_CONDITIONAL_POWER_KW = 100.0  # pitch minor rule applies only above this power

# SPEC-GAP: "two minor breaches" is unspecified between Warning and Critical; resolved as
# Warning = 1-2 minor, Critical = >=3 minor or >=1 major (see PROJECT_SPEC.md §16).
MINOR_TO_CRITICAL = 3


def POWER_CURVE_EXPECTED_KW(wind_speed_ms: float) -> float:
    """Return the reference power-curve expectation for a wind speed (not a model, PROJECT_SPEC.md §6.2).

    Zero below cut-in, a linear ramp from 0 to rated power between cut-in and rated wind
    speed, flat at rated power up to cut-out, and zero above cut-out.

    Args:
        wind_speed_ms: Wind speed in meters per second.

    Returns:
        Expected power output in kW.
    """
    if wind_speed_ms < CUT_IN_MS or wind_speed_ms > CUT_OUT_MS:
        return 0.0
    if wind_speed_ms >= RATED_MS:
        return RATED_POWER_KW
    ramp_fraction = (wind_speed_ms - CUT_IN_MS) / (RATED_MS - CUT_IN_MS)
    return ramp_fraction * RATED_POWER_KW


# --------------------------------------------------------------------------------------
# Health colors & farm scoring (PROJECT_SPEC.md §6.2-6.3)
# --------------------------------------------------------------------------------------

HEALTH_COLORS: dict[str, str] = {
    "Healthy": "#2E7D32",
    "Warning": "#ED6C02",
    "Critical": "#C62828",
    "Error": "#757575",
}

FARM_SCORE_COLORMAP_STOPS = ["#C62828", "#ED6C02", "#2E7D32"]

FARM_ALERT_ON_ANY_CRITICAL = True
FARM_ALERT_ERROR_FRACTION = 0.20

# Keyed by HealthStatus member name (e.g. "HEALTHY"), not its display value.
FARM_SCORE_WEIGHTS: dict[str, float] = {"HEALTHY": 1.0, "WARNING": 0.6, "CRITICAL": 0.0}

# --------------------------------------------------------------------------------------
# Map defaults (PROJECT_SPEC.md §8.1)
# --------------------------------------------------------------------------------------

BOUNDS_EXPANSION = 1.10
SINGLE_POINT_ZOOM = 13
DASHBOARD_FRACTION = 1 / 3
MOBILE_BREAKPOINT_PX = 768
ZERO_SPAN_PAD_DEG = 0.05  # Bounds.expanded() fallback when all points share a coordinate

# Conservative, fixed panel-size estimates for `fit_bounds` padding (PROJECT_SPEC.md §10.1
# rules out live JS viewport measurement) — src/ui/layout.py's viewport_padding().
DESKTOP_PANEL_PX = 480
MOBILE_PANEL_PX = 260

# SPEC-GAP: `st_folium`'s `height` parameter is a fixed pixel int (streamlit-folium has no
# `vh`/percentage support), so "the map fills the viewport" (PROJECT_SPEC.md §8.1) is
# approximated with the same fixed-conservative-estimate approach as DESKTOP_PANEL_PX /
# MOBILE_PANEL_PX above, rather than JS measurement. See PROJECT_SPEC.md §8.1; src/ui/map_view.py.
MAP_HEIGHT_PX = 900

# Turbine layer markers (PROJECT_SPEC.md §8.3) — discrete health-colored dots, shown at farm/
# turbine level. The selected turbine renders larger and thicker so it stands out among siblings.
TURBINE_MARKER_RADIUS_PX = 8
TURBINE_MARKER_WEIGHT_PX = 1
TURBINE_MARKER_FILL_OPACITY = 0.9
TURBINE_SELECTED_RADIUS_PX = 11
TURBINE_SELECTED_WEIGHT_PX = 3

# The parent farm dot stays visible but dimmed once a farm's turbine layer is showing
# (PROJECT_SPEC.md §8.3), so it reads as context rather than a competing focal point.
FARM_MARKER_DIMMED_OPACITY = 0.4

# 16-point compass abbreviations, indexed by 22.5°-wide sector (0 = N, 4 = E, ...).
COMPASS_POINTS: tuple[str, ...] = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)

# --------------------------------------------------------------------------------------
# Performance caps (PROJECT_SPEC.md §12)
# --------------------------------------------------------------------------------------

MAX_TIMESERIES_POINTS = 2000
MAX_SCATTER_POINTS = 5000
TELEMETRY_INTERVAL_MINUTES = 5
BUCKET_BY_WINDOW: dict[str, str] = {"24h": "5 minutes", "7d": "1 hour", "all": "6 hours"}

# --------------------------------------------------------------------------------------
# Time windows
# --------------------------------------------------------------------------------------

TIME_WINDOWS: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "all": None,
}

# Display labels for the Turbine Dashboard's time-window dropdown (src/ui/dashboards/turbine.py,
# PROJECT_SPEC.md §10.4). Keys match TIME_WINDOWS/state.history_window exactly.
HISTORY_WINDOW_LABELS: dict[str, str] = {
    "24h": "24 Hours",
    "7d": "7 Days",
    "all": "Full History",
}

# --------------------------------------------------------------------------------------
# NWP provider (PROJECT_SPEC.md §9)
# --------------------------------------------------------------------------------------

# "stub" (default, v1) or "hrrr" (ToDo skeleton, raises NotImplementedError) — see src/domain/nwp.py.
NWP_PROVIDER: str = os.environ.get("NWP_PROVIDER", "stub")
NWP_GRID_RESOLUTION = 12  # points per axis for StubNWPProvider.grid()
NWP_STUB_SEED = 20260101

# --------------------------------------------------------------------------------------
# Charts (src/ui/charts.py)
# --------------------------------------------------------------------------------------

CHART_HEIGHT_PX = 260
PLOTLY_TEMPLATE = "plotly_white"
CHART_MARGIN: dict[str, int] = {"l": 40, "r": 20, "t": 40, "b": 40}

# Wind rose petal colors (PROJECT_SPEC.md §10.3): previous 24h drawn first in gray, the
# current hour drawn on top in the accent color.
WIND_ROSE_HISTORY_COLOR = "#BDBDBD"
WIND_ROSE_CURRENT_COLOR = "#1565C0"

# Below this many (x, y) pairs an OLS fit is not meaningful; render "Insufficient data" instead.
SCATTER_MIN_REGRESSION_POINTS = 3

# --------------------------------------------------------------------------------------
# Fleet Dashboard (src/ui/dashboards/fleet.py, PROJECT_SPEC.md §10.2)
# --------------------------------------------------------------------------------------

# Current Power Output switches from "X,XXX kW" to megawatts above this many kW.
MW_DISPLAY_THRESHOLD_KW = 10_000.0
KW_PER_MW = 1000.0

# --------------------------------------------------------------------------------------
# Farm Dashboard (src/ui/dashboards/farm.py, PROJECT_SPEC.md §10.3)
# --------------------------------------------------------------------------------------

# Air temperature is displayed in °C with °F alongside it: F = C * FAHRENHEIT_SCALE + FAHRENHEIT_OFFSET.
FAHRENHEIT_SCALE = 9 / 5
FAHRENHEIT_OFFSET = 32.0

# --------------------------------------------------------------------------------------
# Map layer overlays (src/ui/map_view.py, app.py, PROJECT_SPEC.md §8.4)
# --------------------------------------------------------------------------------------

MAP_LAYER_OVERLAY_OPACITY = 0.5

# Sequential single-hue "blue" ramp (light -> dark; the dataviz skill's validated reference
# palette for continuous magnitude). Reused as-is for both wind and temperature so the two
# grid overlays read as one consistent visual language instead of each inventing its own
# gradient, and so neither ever becomes a rainbow.
GRID_OVERLAY_COLORMAP_STOPS = ["#cde2fb", "#6da7ec", "#256abf", "#0d366b"]

MAP_LAYER_SIMULATED_CAPTION = "Simulated data — NWP provider not connected"

MAP_CONTROLS_LABELS: dict[str, str] = {
    "wind": "Wind streams",
    "temperature": "Temperature",
    "forecast": "Forecasted power output",
}

# Quoted verbatim from PROJECT_SPEC.md §8.4 so the checkbox's placeholder text can't drift.
FORECAST_TODO_MESSAGE = "Power output forecasting is not yet implemented. See PROJECT_SPEC.md §8.4."
