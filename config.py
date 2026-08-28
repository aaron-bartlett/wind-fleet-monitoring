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
    # SPEC-GAP: NWP (weather) valid-time override, deliberately independent of `sim_now` and
    # the dataset's own "now". Lets HRRR overlays target a cycle the archive actually has
    # while the dashboards keep running against the telemetry clock (see README §16). `None`
    # -> NWP requests use the resolved "now" (`clock.get_nwp_time`).
    nwp_valid_time: datetime | None = None


def load_settings() -> Settings:
    """Read runtime settings from environment variables.

    Returns:
        A populated, immutable `Settings`.

    Raises:
        ConfigError: `SIM_NOW`, `NWP_VALID_TIME`, or `STALE_AFTER_MINUTES` is set but cannot
            be parsed.
    """
    data_dir = Path(os.environ.get("DATA_DIR", "data"))

    duckdb_path_str = os.environ.get("DUCKDB_PATH")
    duckdb_path = Path(duckdb_path_str) if duckdb_path_str else data_dir / "fleet.duckdb"

    sim_now = _parse_utc_datetime(os.environ.get("SIM_NOW"), "SIM_NOW")
    nwp_valid_time = _parse_utc_datetime(os.environ.get("NWP_VALID_TIME"), "NWP_VALID_TIME")
    stale_after_minutes = _parse_stale_after_minutes(os.environ.get("STALE_AFTER_MINUTES"))

    return Settings(
        data_dir=data_dir,
        duckdb_path=duckdb_path,
        sim_now=sim_now,
        stale_after_minutes=stale_after_minutes,
        nwp_valid_time=nwp_valid_time,
    )


def _parse_utc_datetime(raw: str | None, var_name: str) -> datetime | None:
    """Parse an ISO-8601 environment variable to a tz-aware UTC datetime, if set.

    Args:
        raw: The raw environment value, or `None` when unset.
        var_name: The variable's name, used only in error messages.

    Returns:
        The value as a UTC datetime, or `None` when `raw` is `None`.

    Raises:
        ConfigError: `raw` is set but is not a valid ISO-8601 datetime, or lacks a timezone.
    """
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError(f"{var_name}={raw!r} is not a valid ISO-8601 datetime.") from exc
    if parsed.tzinfo is None:
        raise ConfigError(f"{var_name}={raw!r} must include a UTC offset or 'Z' suffix.")
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

# FARM_SCORE_COLORMAP_STOPS = ["#C62828", "#ED6C02", "#2E7D32"]
FARM_SCORE_COLORMAP_STOPS = ["#C62828", "#DDCB25", "#2E7D32"]

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

# The desktop dashboard panel is drag-resizable along its right edge (src/ui/layout.py). The
# chosen width is stored as a fraction of viewport width, clamped to this range and persisted
# in the browser's localStorage so it survives Streamlit reruns.
DASHBOARD_MIN_FRACTION = 0.25
DASHBOARD_MAX_FRACTION = 0.50

# The dashboard's `st.metric` row (four widgets on the Fleet Dashboard, two on the Farm
# Dashboard) reflows as the panel is drag-resized: 4-up above `_2UP_MAX_PX` of panel content
# width, 2-up at/below it, 1-up at/below `_1UP_MAX_PX`. Driven by a CSS `@container` query on
# the panel (src/ui/layout.py) — a viewport media query cannot see the drag handle, which only
# changes the panel's own width. Below the wide layout the metric value also scales down within
# `[_VALUE_MIN_REM, _VALUE_MAX_REM]` (max = Streamlit's 2.25rem default, so the wide view is
# unchanged) and the label wraps instead of truncating to an ellipsis.
DASHBOARD_METRIC_2UP_MAX_PX = 520
DASHBOARD_METRIC_1UP_MAX_PX = 360
DASHBOARD_METRIC_VALUE_MIN_REM = 1.35
DASHBOARD_METRIC_VALUE_MAX_REM = 2.25

# Inner buffer on the dashboard panel so its text and full-width plots do not crowd the panel
# edges (src/ui/layout.py). Slightly wider on the left, where the content starts; a lighter
# right buffer keeps plots clear of the panel scrollbar.
DASHBOARD_PANEL_PAD_LEFT_REM = 0.9
DASHBOARD_PANEL_PAD_RIGHT_REM = 0.5

MOBILE_BREAKPOINT_PX = 768
ZERO_SPAN_PAD_DEG = 0.05  # Bounds.expanded() fallback when all points share a coordinate

# Conservative, fixed panel-size estimates for `fit_bounds` padding (PROJECT_SPEC.md §10.1
# rules out live JS viewport measurement) — src/ui/layout.py's viewport_padding(). Each is
# `DASHBOARD_FRACTION` of the reference viewport `IMPLEMENTATION_PLAN.md` Phase 15's manual
# check verifies against: 1440x900 desktop (1440 / 3 = 480) and 390x844 mobile (844 / 3 = 281.3,
# rounded up). A prior value of 260 here under-padded the 390x844 case by ~21px, letting markers
# render just inside the bottom panel; 282 covers it with the same exact-reference-viewport
# derivation as the desktop constant, plus a few pixels of headroom for slightly taller phones.
DESKTOP_PANEL_PX = 480
MOBILE_PANEL_PX = 282

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

# `@st.cache_data` TTL (seconds) for the UI-layer wrappers around the fleet-scale aggregate
# builders (`IMPLEMENTATION_PLAN.md` Phase 15; `app.py`) — long enough that a burst of reruns
# from panning/zooming/toggling a checkbox within one drill-down session doesn't recompute the
# roll-up, short enough that the map still reflects a `SIM_NOW`/dataset change within a session.
CACHE_TTL_SECONDS = 300

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

# "stub" is the only provider wired up in this version. Live HRRR fetching is disabled:
# nwp.get_provider() rejects "hrrr" (and anything else) with a ConfigError. The HRRRProvider
# class and src/data/hrrr.py stay in the tree for a future release (README §16).
NWP_PROVIDER: str = os.environ.get("NWP_PROVIDER", "stub")
NWP_GRID_RESOLUTION = 12  # points per axis for StubNWPProvider.grid()
NWP_STUB_SEED = 20260101

# Shown in place of a wind rose / grid overlay when the provider cannot serve a request
# (out of domain, no archived HRRR run for the resolved time, offline) — CLAUDE.md §5.3.
NWP_UNAVAILABLE_MESSAGE = "Weather data unavailable for this time or area."

# --------------------------------------------------------------------------------------
# HRRR provider (src/data/hrrr.py, src/domain/nwp.py::HRRRProvider)
# --------------------------------------------------------------------------------------
# SPEC-GAP: PROJECT_SPEC.md §9 specifies HRRRProvider as a NotImplementedError skeleton and
# CLAUDE.md §5.8 says not to build it; implemented anyway by explicit request. The prompt asked
# for "100 m" winds/temperature. HRRR's `sfc` product publishes wind (U/V) at 80 m above
# ground — its highest AGL wind level, used as the hub-height proxy — but temperature only at
# 2 m above ground, so the temperature overlay is the standard 2 m screen-level field. Both
# heights are labelled honestly in the UI. See README §16.

HRRR_MODEL = "hrrr"
HRRR_PRODUCT = "sfc"  # 2D surface file; carries UGRD/VGRD at 80 m and TMP at 2 m above ground
HRRR_FXX = 0  # analysis (F00) of the cycle at/before valid_time
HRRR_WIND_LEVEL = "80 m above ground"
HRRR_TEMP_LEVEL = "2 m above ground"

# Regular lat/lon mesh the native HRRR grid is resampled onto for GridField / the ImageOverlay.
# Finer than NWP_GRID_RESOLUTION because real data over a single farm is otherwise flat.
HRRR_GRID_RESOLUTION = 48

# Above this many native points inside the view box, stride before scipy.griddata — the fleet
# view box is nearly CONUS-scale (~15 deg lat x 31 deg lon).
HRRR_MAX_NATIVE_CELLS = 200_000

# Herbie's download cache (GRIB subsets + .idx). Git-ignored; created on first HRRR fetch.
HRRR_CACHE_DIR = Path(os.environ.get("HRRR_CACHE_DIR", "data/hrrr-cache"))

# Fast reject before any network call: (lat_min, lat_max, lon_min, lon_max) roughly bounding
# the HRRR CONUS domain. A plain tuple, not a Bounds — config must not import src.domain.models.
HRRR_DOMAIN_LATLON_BBOX = (21.0, 53.0, -134.0, -60.0)

# A point farther than this from its nearest native HRRR cell is treated as out of domain.
HRRR_NEAREST_MAX_KM = 5.0

# Caption stem shown beneath a real (non-simulated) overlay / weather block, plus the
# per-variable AGL level so wind (80 m) and temperature (2 m) are never conflated.
HRRR_SOURCE_LABEL = "HRRR"
HRRR_WIND_LEVEL_LABEL = "80 m AGL"
HRRR_TEMP_LEVEL_LABEL = "2 m AGL"

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

# The same note for the farm/turbine dashboard weather blocks (src/ui/dashboards/*.py); the
# leading glyph matches the other dashboard cautions.
WEATHER_SIMULATED_CAPTION = "⚠ Simulated data — NWP provider not connected"

# Shown under the Farm/Turbine wind rose: this version has no live NWP provider, so the rose's
# petal lengths are real telemetry wind speed but its angles (and the air-temp line) are a
# deterministic synthetic stand-in — telemetry carries no wind-direction channel.
WIND_ROSE_TELEMETRY_CAPTION = (
    "Petal length = measured wind speed (telemetry); direction and air temperature are "
    "theoretical — no direction or ambient-temperature sensor."
)

MAP_CONTROLS_LABELS: dict[str, str] = {
    "wind": "Wind streams",
    "temperature": "Temperature",
    "forecast": "Forecasted power output",
}

# Quoted verbatim from PROJECT_SPEC.md §8.4 so the checkbox's placeholder text can't drift.
FORECAST_TODO_MESSAGE = "Power output forecasting is not yet implemented. See PROJECT_SPEC.md §8.4."
