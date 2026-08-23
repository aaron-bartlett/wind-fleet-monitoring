# IMPLEMENTATION_PLAN.md — Wind Fleet Monitor

Sequential, dependency-ordered build plan. Each phase is self-contained and independently verifiable.

**How to use this document:** hand a single phase to Claude Code with the instruction
*"Read `CLAUDE.md` and `PROJECT_SPEC.md`, then execute Phase N of `IMPLEMENTATION_PLAN.md`."*
Do not begin a phase until the previous phase's **Exit Gate** passes.

**Universal Exit Gate — applies to every phase in addition to its own verification command:**

```bash
ruff format --check . && ruff check . && mypy src app.py && pytest
```

**Phase order and dependencies:**

```
0 ─► 1 ─► 2 ─► 3 ─┬─► 4 ─► 5 ─► 6 ─┬─► 8 ─► 9 ─► 10 ─► 11 ─► 12 ─► 13 ─► 14
                  └─► 7 ────────────┘
```

Phases 0–6 contain no UI and no Streamlit imports. Phases 7–14 build the interface on top.
Stopping after Phase 11 yields a coherent, demonstrable product (`PROJECT_SPEC.md` §14).

---

## Phase 0 — Repository Scaffold & Toolchain

**Objective:** Create the directory tree, dependency files, tool configuration, and the architecture
test that enforces layering. No application logic.

**Target Files (create):**

```
pyproject.toml
requirements.txt
requirements-dev.txt
requirements-optional.txt
.gitignore
src/__init__.py
src/data/__init__.py
src/domain/__init__.py
src/ui/__init__.py
src/ui/dashboards/__init__.py
tests/__init__.py
tests/conftest.py
tests/test_architecture.py
```

**Data Contracts:** None.

**Step-by-Step Instructions:**

1. Create the full directory tree exactly as listed in `CLAUDE.md` §4.2. Create empty `__init__.py`
   files in every package directory (`src`, `src/data`, `src/domain`, `src/ui`,
   `src/ui/dashboards`, `tests`). Create empty directories `data/` and `tests/fixtures/`.
2. Write `requirements.txt`, `requirements-dev.txt`, and `requirements-optional.txt` with the exact
   pins from `CLAUDE.md` §2.2–2.3. `requirements-optional.txt` contains only
   `# herbie-data~=2024.8  # NOT installed in v1 — HRRRProvider is a NotImplementedError skeleton`.
3. Write `pyproject.toml` with the exact `[tool.ruff]`, `[tool.mypy]`, and `[tool.pytest.ini_options]`
   blocks from `CLAUDE.md` §2.5.
4. Write `.gitignore` covering: `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`,
   `.ruff_cache/`, `data/*.duckdb`, `.DS_Store`, `.streamlit/secrets.toml`.
5. Write `tests/test_architecture.py`. It must:
   - Walk every `.py` file under `src/domain/` and `src/data/` using `pathlib.Path.rglob`.
   - Parse each with `ast.parse` and collect all `ast.Import` and `ast.ImportFrom` module names.
   - Assert none of `{"streamlit", "folium", "streamlit_folium", "branca", "plotly"}` appears as a
     top-level module in any of them. The assertion message must name the offending file and import.
   - A second test asserting `src/domain/` and `src/data/` contain no raw SQL outside
     `src/data/queries.py` and `src/data/db.py` — scan for the case-insensitive substrings
     `"select "`, `"insert "`, `"create table"` in string literals.
6. Write a minimal `tests/conftest.py` containing a `pytest` fixture `fixtures_dir` returning
   `Path(__file__).parent / "fixtures"`. More fixtures are added in later phases.
7. Run `pip install -r requirements-dev.txt`.

**Verification Command:**

```bash
pip install -r requirements-dev.txt && ruff check . && mypy src && pytest tests/test_architecture.py -v
```

**Expected result:** 2 passing tests. Lint and type check clean.

---

## Phase 1 — Configuration & Error Hierarchy

**Objective:** Centralize every constant, threshold, color, and setting. Define the exception types.
Nothing in the project may hard-code a number after this phase.

**Target Files (create):** `config.py`, `src/errors.py`, `tests/test_config.py`

**Data Contracts:**

```python
# src/errors.py
class WindFleetError(Exception): ...
class DataLoadError(WindFleetError): ...
class QueryError(WindFleetError): ...
class ConfigError(WindFleetError): ...
class NWPUnavailableError(WindFleetError): ...

# config.py
@dataclass(frozen=True, slots=True)
class Threshold:
    metric: str
    physical_min: float
    physical_max: float
    minor_max: float | None = None      # breach when value > minor_max
    major_max: float | None = None      # breach when value > major_max
    minor_min: float | None = None      # breach when value < minor_min (conditional rules only)

@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    duckdb_path: Path
    sim_now: datetime | None
    stale_after_minutes: int
```

**Step-by-Step Instructions:**

1. Write `src/errors.py` with exactly the hierarchy above. Each class gets a one-line docstring
   stating when it is raised.
2. Write `config.py`. It contains **only** constants and the two frozen dataclasses — no logic beyond
   `load_settings()` and validation. Required contents:

   **Data / runtime settings** — `load_settings() -> Settings` reading environment variables:
   - `DATA_DIR` → `Path`, default `Path("data")`
   - `DUCKDB_PATH` → `Path`, default `<data_dir>/fleet.duckdb`
   - `SIM_NOW` → ISO-8601 string parsed to a tz-aware UTC `datetime`, default `None`.
     A value that fails to parse raises `ConfigError` naming the variable and the received value.
   - `STALE_AFTER_MINUTES` → `int`, default `15`

   **Telemetry metric names** — `METRICS: tuple[str, ...] = ("power_output_kw", "wind_speed_ms",
   "rotor_rpm", "blade_pitch_deg", "gearbox_temp_c")` and `METRIC_LABELS: dict[str, str]` mapping each
   to a display label with units (e.g. `"power_output_kw": "Power Output (kW)"`).

   **Thresholds** — `THRESHOLDS: dict[str, Threshold]` transcribing `PROJECT_SPEC.md` §6.2 exactly:

   | metric | physical_min | physical_max | minor_max | major_max |
   |---|---|---|---|---|
   | `power_output_kw` | -50 | 5000 | *(conditional — see below)* | *(conditional)* |
   | `wind_speed_ms` | 0 | 60 | 25 | None |
   | `rotor_rpm` | 0 | 40 | 18.5 | 22.0 |
   | `blade_pitch_deg` | -5 | 95 | 25 *(conditional)* | 40 |
   | `gearbox_temp_c` | -40 | 200 | 95 | 110 |

   **Conditional-rule constants** (used by `health.py` for rules a simple max cannot express):
   - `CUT_IN_MS = 3.0`, `RATED_MS = 12.0`, `CUT_OUT_MS = 25.0`, `RATED_POWER_KW = 3500.0`
   - `POWER_UNDERPERFORM_FRACTION = 0.40` — minor breach when actual < 40% of curve expectation
   - `POWER_CHECK_WIND_RANGE = (4.0, 15.0)` — window in which the underperformance rule applies
   - `POWER_ZERO_WIND_RANGE = (4.0, 25.0)` — window in which zero power is a major breach
   - `ROTOR_STALL_RPM = 0.5`, `ROTOR_STALL_WIND_RANGE = (4.0, 25.0)`
   - `PITCH_CONDITIONAL_POWER_KW = 100.0` — pitch minor rule applies only above this power

   **Health classification** — `MINOR_TO_CRITICAL = 3` (≥3 minor breaches → Critical; 1–2 → Warning;
   this is the `PROJECT_SPEC.md` §16 spec-gap resolution).

   **Colors** — `HEALTH_COLORS: dict[str, str]` = Healthy `#2E7D32`, Warning `#ED6C02`,
   Critical `#C62828`, Error `#757575`. Plus `FARM_SCORE_COLORMAP_STOPS = ["#C62828", "#ED6C02", "#2E7D32"]`.

   **Farm alerting** — `FARM_ALERT_ON_ANY_CRITICAL = True`, `FARM_ALERT_ERROR_FRACTION = 0.20`.
   Farm health score weights: `FARM_SCORE_WEIGHTS = {"HEALTHY": 1.0, "WARNING": 0.6, "CRITICAL": 0.0}`
   (Error excluded from the denominator).

   **Map defaults** — `BOUNDS_EXPANSION = 1.10`, `SINGLE_POINT_ZOOM = 13`,
   `DASHBOARD_FRACTION = 1 / 3`, `MOBILE_BREAKPOINT_PX = 768`.

   **Performance caps** — `MAX_TIMESERIES_POINTS = 2000`, `MAX_SCATTER_POINTS = 5000`,
   `TELEMETRY_INTERVAL_MINUTES = 5`, and
   `BUCKET_BY_WINDOW = {"24h": "5 minutes", "7d": "1 hour", "all": "6 hours"}`.

   **Time windows** — `TIME_WINDOWS: dict[str, timedelta | None]` = `{"24h": timedelta(hours=24),
   "7d": timedelta(days=7), "all": None}`.

3. Add `POWER_CURVE_EXPECTED_KW(wind_speed_ms: float) -> float` to `config.py`: 0 below `CUT_IN_MS`;
   linear ramp from 0 at `CUT_IN_MS` to `RATED_POWER_KW` at `RATED_MS`; flat `RATED_POWER_KW` from
   `RATED_MS` to `CUT_OUT_MS`; 0 above `CUT_OUT_MS`.
4. Write `tests/test_config.py`: `load_settings()` defaults are correct; `SIM_NOW="2026-01-02T23:55:00Z"`
   parses to a tz-aware UTC datetime; `SIM_NOW="garbage"` raises `ConfigError`; every metric in
   `METRICS` has an entry in both `THRESHOLDS` and `METRIC_LABELS`; `POWER_CURVE_EXPECTED_KW` returns
   0 at 2 m/s, ~1750 at 7.5 m/s, 3500 at 12 and at 20 m/s, and 0 at 30 m/s.

**Verification Command:**

```bash
pytest tests/test_config.py -v && mypy config.py src/errors.py
```

---

## Phase 2 — Domain Models

**Objective:** Define every shared type once, before any module needs it. Pure data, no behavior.

**Target Files (create):** `src/domain/models.py`, `tests/test_models.py`

**Data Contracts:**

```python
class HealthStatus(StrEnum):
    HEALTHY = "Healthy"
    WARNING = "Warning"
    CRITICAL = "Critical"
    ERROR = "Error"

class Level(StrEnum):
    FLEET = "fleet"
    FARM = "farm"
    TURBINE = "turbine"

class Severity(StrEnum):
    MINOR = "minor"
    MAJOR = "major"

@dataclass(frozen=True, slots=True)
class Breach:
    metric: str
    value: float
    threshold: float
    severity: Severity
    message: str          # human-readable, e.g. "Gearbox temp 126.5 °C exceeds major limit 110 °C"

@dataclass(frozen=True, slots=True)
class HealthResult:
    status: HealthStatus
    minor: tuple[Breach, ...]
    major: tuple[Breach, ...]
    errors: tuple[str, ...]        # reasons the turbine is ERROR
    @property
    def color(self) -> str: ...    # from config.HEALTH_COLORS

@dataclass(frozen=True, slots=True)
class TelemetryRecord:
    turbine_id: str
    farm_id: str
    timestamp: datetime            # tz-aware UTC
    received_at: datetime          # tz-aware UTC
    power_output_kw: float | None
    wind_speed_ms: float | None
    rotor_rpm: float | None
    blade_pitch_deg: float | None
    gearbox_temp_c: float | None
    @property
    def lag_minutes(self) -> float: ...
    def get(self, metric: str) -> float | None: ...

@dataclass(frozen=True, slots=True)
class Bounds:
    lat_min: float; lat_max: float; lon_min: float; lon_max: float
    def expanded(self, factor: float) -> "Bounds": ...
    def as_folium(self) -> list[list[float]]: ...   # [[lat_min, lon_min], [lat_max, lon_max]]

@dataclass(frozen=True, slots=True)
class Farm:
    farm_id: str; farm_name: str; latitude: float; longitude: float

@dataclass(frozen=True, slots=True)
class Turbine:
    turbine_id: str; farm_id: str; latitude: float; longitude: float

@dataclass(frozen=True, slots=True)
class PointForecast:
    valid_time: datetime
    wind_speed_ms: float
    wind_direction_deg: float      # meteorological — direction wind blows FROM
    air_temp_c: float
    is_simulated: bool

@dataclass(frozen=True, slots=True)
class GridField:
    lats: np.ndarray; lons: np.ndarray; values: np.ndarray
    variable: str; valid_time: datetime; is_simulated: bool
```

**Step-by-Step Instructions:**

1. Create `src/domain/models.py` with exactly the types above. All dataclasses `frozen=True, slots=True`.
   Collections inside frozen dataclasses are `tuple`, never `list`.
2. `TelemetryRecord.lag_minutes` returns `(received_at - timestamp).total_seconds() / 60`.
   `TelemetryRecord.get(metric)` returns the attribute named `metric`, raising `KeyError` for an
   unknown metric name.
3. `Bounds.expanded(factor)` widens each axis so the total span is `factor` × the original, centered on
   the original midpoint. For a zero-span axis (all points identical) fall back to ±0.05°.
4. Add `compass_point(degrees: float) -> str` returning the 16-point compass abbreviation
   (`"N"`, `"NNE"`, … `"NNW"`) for a bearing in [0, 360).
5. Write `tests/test_models.py`: `Bounds.expanded(1.10)` on a known box produces the expected corners;
   zero-span fallback works; `as_folium()` ordering is correct; `lag_minutes` computes correctly;
   `get()` on an unknown metric raises `KeyError`; `compass_point` returns `"N"` at 0 and 359,
   `"NNW"` at 337.5, `"E"` at 90.

**Verification Command:**

```bash
pytest tests/test_models.py -v && mypy src/domain/models.py
```

---

## Phase 3 — DuckDB Ingest Layer

**Objective:** Load the three CSVs into DuckDB with correct types, deduplication, indexes, and the
`latest_telemetry` view. Fail fast and clearly on bad input.

**Target Files (create):** `src/data/db.py`, `tests/fixtures/{farms,turbines,telemetry}.csv`,
`tests/test_ingest.py`. **Modify:** `tests/conftest.py`.

**Data Contracts:**

```python
@dataclass(frozen=True, slots=True)
class IngestSummary:
    farms: int
    turbines: int
    telemetry_rows: int
    duplicates_removed: int
    rows_with_nulls: int
    telemetry_start: datetime
    telemetry_end: datetime
    elapsed_seconds: float

def connect(settings: Settings) -> duckdb.DuckDBPyConnection: ...
def ingest(con: duckdb.DuckDBPyConnection, settings: Settings) -> IngestSummary: ...
def is_ingest_current(con, settings) -> bool: ...
```

**Required schema after ingest:**

| table | columns |
|---|---|
| `farms` | `farm_id VARCHAR PK, farm_name VARCHAR, latitude DOUBLE, longitude DOUBLE` |
| `turbines` | `turbine_id VARCHAR PK, farm_id VARCHAR, latitude DOUBLE, longitude DOUBLE` |
| `telemetry` | `turbine_id VARCHAR, farm_id VARCHAR, timestamp TIMESTAMPTZ, received_at TIMESTAMPTZ, power_output_kw DOUBLE, wind_speed_ms DOUBLE, rotor_rpm DOUBLE, blade_pitch_deg DOUBLE, gearbox_temp_c DOUBLE` |
| `ingest_meta` | `key VARCHAR, value VARCHAR` — stores each source CSV's mtime and size |
| `latest_telemetry` (view) | one row per `turbine_id`, greatest `timestamp` |

**Step-by-Step Instructions:**

1. Create `tests/fixtures/` CSVs — small and hand-built, covering every edge case the tests need:
   - `farms.csv`: 3 farms. One (`FARM03`) has no turbines.
   - `turbines.csv`: 4 turbines — 2 on `FARM01`, 1 on `FARM02`, 1 (`TURB999`) on `FARM02` with **no**
     telemetry rows at all.
   - `telemetry.csv`: ~40 rows covering — a clean healthy record; a 10-minute gap (one missing
     interval); a duplicate `(turbine_id, timestamp)` with two different `received_at` values; a row
     with a NULL metric; a row with `gearbox_temp_c = 126.5` (major breach); a row with
     `blade_pitch_deg = 44` (major breach); a row with `gearbox_temp_c = 250` (physically impossible);
     a row with 20-minute ingest lag.
   - Deliberately give `turbines.csv` the extra denormalized `farm_name` column so the ingest's
     column-dropping is exercised.
2. Write `src/data/db.py`:
   - `connect()` opens `settings.duckdb_path` (creating parent dirs), or `:memory:` if the path is
     the literal string `":memory:"`.
   - `ingest()` runs inside a transaction:
     a. Verify all three CSVs exist; raise `DataLoadError` naming the missing path.
     b. `read_csv_auto` each into a staging relation. Verify required columns are present; raise
        `DataLoadError` naming the file and the missing column(s).
     c. Build `farms` and `turbines` with explicit column selection — **drop** `turbines.farm_name`.
     d. Build `telemetry` casting `timestamp` and `received_at` to `TIMESTAMPTZ` via
        `strptime(..., '%Y-%m-%dT%H:%M:%SZ')` with UTC. Raise `DataLoadError` on parse failure.
     e. Deduplicate `(turbine_id, timestamp)` keeping the greatest `received_at`
        (`ROW_NUMBER() OVER (PARTITION BY turbine_id, timestamp ORDER BY received_at DESC) = 1`).
        Count removals for the summary.
     f. Create indexes `idx_tel_turbine_ts` on `(turbine_id, timestamp)` and `idx_tel_farm_ts` on
        `(farm_id, timestamp)`.
     g. Create the `latest_telemetry` view per `PROJECT_SPEC.md` §5.2.
     h. Write source-file mtime and size into `ingest_meta`.
     i. Return a populated `IngestSummary`.
   - `is_ingest_current()` returns `True` when all tables exist and every source CSV's current mtime
     and size match the stored `ingest_meta` values. Used to skip re-ingest on restart.
   - Log the summary at INFO level.
3. Write `tests/test_ingest.py`:
   - Ingest of the fixture CSVs produces the expected row counts.
   - `duplicates_removed == 1` and the surviving row carries the **later** `received_at`.
   - `turbines` table has no `farm_name` column.
   - `timestamp` and `received_at` come back as tz-aware UTC datetimes.
   - `latest_telemetry` returns exactly one row per turbine that has telemetry, and `TURB999` is absent.
   - A CSV missing a required column raises `DataLoadError` whose message contains both the filename
     and the column name.
   - A missing file raises `DataLoadError` naming the path.
   - `is_ingest_current()` is `True` immediately after ingest and `False` after a fixture file's mtime
     is touched.
4. Add a `db_con` fixture to `tests/conftest.py`: an in-memory connection ingested from
   `tests/fixtures/`, function-scoped.

**Verification Command:**

```bash
pytest tests/test_ingest.py -v && mypy src/data/db.py
```

---

## Phase 4 — Query Layer

**Objective:** Every SQL statement the application will ever run, as named, parameterized, typed
functions. No aggregation happens outside this file.

**Target Files (create):** `src/data/queries.py`, `tests/test_queries.py`

**Data Contracts:** Every function's first parameter is `con: duckdb.DuckDBPyConnection`.

```python
def get_farms(con) -> list[Farm]: ...
def get_turbines(con, farm_id: str | None = None) -> list[Turbine]: ...
def get_turbine_counts_by_farm(con) -> dict[str, int]: ...
def get_max_timestamp(con) -> datetime | None: ...
def get_latest_records(con, farm_id: str | None = None) -> list[TelemetryRecord]: ...
def get_latest_record_for_turbine(con, turbine_id: str) -> TelemetryRecord | None: ...

def get_power_timeseries(
    con, *, level: Level, entity_id: str | None, start: datetime | None,
    end: datetime, bucket: str, max_points: int,
) -> pd.DataFrame: ...            # columns: bucket_start (tz-aware UTC), power_kw (float)

def get_total_energy_mwh(con, *, level: Level, entity_id: str | None) -> float: ...
def get_current_power_kw(con, *, level: Level, entity_id: str | None, now: datetime,
                         stale_after_minutes: int) -> float: ...
def get_scatter_data(con, *, turbine_id: str, x_metric: str, y_metric: str,
                     start: datetime | None, end: datetime, max_points: int) -> pd.DataFrame: ...
                                   # columns: x (float), y (float)
def get_fleet_bounds(con) -> Bounds | None: ...
def get_farm_turbine_bounds(con, farm_id: str) -> Bounds | None: ...
```

**Step-by-Step Instructions:**

1. Create `src/data/queries.py`. Module docstring states: *the sole owner of SQL in this project.*
2. Implement every function above. Rules:
   - Bind all parameters with `?`. The only value ever interpolated into a SQL string is `bucket`
     and `x_metric`/`y_metric`, which **must** first be validated against `config.BUCKET_BY_WINDOW`
     values and `config.METRICS` respectively; an invalid value raises `QueryError`. Never accept a
     free-form string into SQL.
   - `get_power_timeseries` buckets with `time_bucket(INTERVAL <bucket>, timestamp)` and
     `SUM(power_output_kw)` grouped by bucket, filtered by level (fleet = no filter, farm =
     `farm_id = ?`, turbine = `turbine_id = ?`) and by `timestamp >= ? AND timestamp <= ?`.
     Order ascending. If the result exceeds `max_points`, raise `QueryError` — the caller is
     responsible for choosing a coarse enough bucket, and a silent truncation would mislead.
   - `get_power_timeseries` must **also return gap rows**: left-join the aggregation onto a generated
     time spine (`generate_series`) at the bucket interval so missing intervals appear as rows with
     `power_kw = NULL`. This is what makes `PROJECT_SPEC.md` §11's "genuine gap" rendering possible.
   - `get_total_energy_mwh` = `SUM(power_output_kw) * (TELEMETRY_INTERVAL_MINUTES / 60.0) / 1000.0`.
   - `get_current_power_kw` sums `power_output_kw` from `latest_telemetry`, excluding rows where
     `timestamp < now - stale_after_minutes` and rows where `power_output_kw IS NULL`.
   - `get_scatter_data` selects the two metrics with both `IS NOT NULL`, within the time window, and
     down-samples above `max_points` using a deterministic modulo stride
     (`WHERE rn % CAST(? AS BIGINT) = 0` over a `ROW_NUMBER()`), never `ORDER BY random()`.
   - `get_fleet_bounds` / `get_farm_turbine_bounds` compute `MIN`/`MAX` of lat and lon.
     `get_farm_turbine_bounds` returns `None` when the farm has no turbines.
   - Wrap every `con.execute` in `try/except duckdb.Error` and re-raise as `QueryError` with the
     function name and the original message.
3. Write `tests/test_queries.py` against the `db_con` fixture. At minimum:
   - `get_turbine_counts_by_farm` includes `FARM03` with count 0.
   - `get_latest_records(farm_id="FARM01")` returns exactly the `FARM01` turbines.
   - `get_latest_record_for_turbine("TURB999")` returns `None`.
   - `get_power_timeseries` at fleet level returns a row for the known missing interval with a NULL
     `power_kw`.
   - `get_total_energy_mwh` equals the hand-computed value for the fixture.
   - `get_current_power_kw` excludes a record older than the stale threshold.
   - `get_scatter_data` with `max_points=5` on a larger window returns ≤5 rows.
   - `get_scatter_data(x_metric="; DROP TABLE telemetry")` raises `QueryError`.
   - `get_farm_turbine_bounds("FARM03")` returns `None`.

**Verification Command:**

```bash
pytest tests/test_queries.py -v && mypy src/data/queries.py
```

---

## Phase 5 — Clock & Geo Utilities

**Objective:** Resolve "now" from the dataset rather than the wall clock, and provide timezone and
bounds helpers.

**Target Files (create):** `src/domain/clock.py`, `src/domain/geo.py`, `tests/test_clock.py`,
`tests/test_geo.py`

**Data Contracts:**

```python
# clock.py
def get_now(con, settings: Settings) -> datetime: ...     # tz-aware UTC
def is_stale(record_timestamp: datetime, now: datetime, stale_after_minutes: int) -> bool: ...
def window_start(now: datetime, window_key: str) -> datetime | None: ...   # None for "all"

# geo.py
def local_time(utc_dt: datetime, latitude: float, longitude: float) -> tuple[datetime, str]: ...
def fleet_bounds(con) -> Bounds | None: ...
def farm_view_bounds(con, farm_id: str, farm: Farm) -> Bounds: ...
```

**Step-by-Step Instructions:**

1. `clock.get_now`: return `settings.sim_now` if set; otherwise `queries.get_max_timestamp(con)`;
   if that is `None` (empty telemetry), return `datetime.now(UTC)` and log a warning.
   Result is always tz-aware UTC. `# SPEC-GAP:` comment referencing `PROJECT_SPEC.md` §6.1.
2. `clock.is_stale`: `record_timestamp < now - timedelta(minutes=stale_after_minutes)`. Raise
   `ValueError` if either datetime is naive.
3. `clock.window_start`: look up `config.TIME_WINDOWS[window_key]`; return `now - delta`, or `None`
   for `"all"`. Unknown key raises `ValueError`.
4. `geo.local_time`: use a **module-level singleton** `TimezoneFinder()` (construction is expensive);
   resolve the IANA name via `timezone_at(lat=..., lng=...)`; convert with `ZoneInfo`. If the lookup
   returns `None` (ocean coordinate), fall back to UTC and return `"UTC"` as the label. Return
   `(local_datetime, tz_abbreviation)` where the abbreviation comes from `local_dt.strftime("%Z")`.
5. `geo.fleet_bounds`: delegate to `queries.get_fleet_bounds`, then `.expanded(config.BOUNDS_EXPANSION)`.
6. `geo.farm_view_bounds`: use `queries.get_farm_turbine_bounds`; if `None` or a single point, build a
   box of ±0.05° around the farm coordinate; otherwise expand by `config.BOUNDS_EXPANSION`.
7. Tests: `get_now` honors `SIM_NOW`; falls back to `MAX(timestamp)`; `is_stale` is exact at the
   boundary minute; naive datetime raises; `window_start("all")` is `None`; `local_time` on
   `FARM01` (41.25, -96.53) yields an `America/Chicago` offset and a non-empty abbreviation;
   `local_time` at (0, 0) falls back to UTC without raising; `farm_view_bounds` for the
   single-turbine and zero-turbine fixture farms returns a valid non-degenerate box.

**Verification Command:**

```bash
pytest tests/test_clock.py tests/test_geo.py -v && mypy src/domain/clock.py src/domain/geo.py
```

---

## Phase 6 — Health Classification

**Objective:** The project's most important module. Convert a telemetry record into a `HealthResult`.
Pure function, exhaustively tested at every threshold boundary.

**Target Files (create):** `src/domain/health.py`, `tests/test_health.py`

**Data Contracts:**

```python
def classify(record: TelemetryRecord | None, now: datetime,
             stale_after_minutes: int) -> HealthResult: ...

def classify_many(records: Sequence[TelemetryRecord], turbine_ids: Sequence[str],
                  now: datetime, stale_after_minutes: int) -> dict[str, HealthResult]: ...

def farm_health_score(results: Sequence[HealthResult]) -> float | None: ...   # 0..1, None if no turbines
def farm_alert(results: Sequence[HealthResult]) -> str | None: ...            # reason text, or None
def status_counts(results: Sequence[HealthResult]) -> dict[HealthStatus, int]: ...
```

**Step-by-Step Instructions:**

1. Implement `classify` with this exact precedence:
   - **Step 1 — ERROR checks (short-circuit, return immediately with all reasons collected):**
     a. `record is None` → `errors=("No telemetry received",)`.
     b. `clock.is_stale(record.timestamp, now, stale_after_minutes)` → error reason naming the record
        age in minutes and the threshold.
     c. Any metric in `config.METRICS` is `None`, `NaN`, or outside `[physical_min, physical_max]` →
        one error reason per offending metric, naming the metric, value, and the bound crossed.
   - **Step 2 — collect breaches** (record is valid; evaluate all rules, do not short-circuit):
     - `gearbox_temp_c`: `> major_max (110)` → major; else `> minor_max (95)` → minor.
     - `rotor_rpm`: `> major_max (22.0)` → major; else `> minor_max (18.5)` → minor.
       Additionally `< ROTOR_STALL_RPM (0.5)` while `wind_speed_ms` is inside
       `ROTOR_STALL_WIND_RANGE` → **major** (stalled rotor in operating wind).
     - `blade_pitch_deg`: `> major_max (40)` → major; else `> minor_max (25)` **and**
       `power_output_kw > PITCH_CONDITIONAL_POWER_KW (100)` → minor.
     - `wind_speed_ms`: `> minor_max (25)` → minor (cut-out exceeded). No major.
     - `power_output_kw`: `<= 0` while `wind_speed_ms` is inside `POWER_ZERO_WIND_RANGE` → **major**;
       else `< POWER_UNDERPERFORM_FRACTION × POWER_CURVE_EXPECTED_KW(wind_speed_ms)` while
       `wind_speed_ms` is inside `POWER_CHECK_WIND_RANGE` → **minor**.
     - A metric contributes **at most one** breach; major supersedes minor for the same metric.
   - **Step 3 — classify:** any major, or `len(minor) >= config.MINOR_TO_CRITICAL` → `CRITICAL`;
     else `len(minor) >= 1` → `WARNING`; else `HEALTHY`.
     Add `# SPEC-GAP: 2 minor breaches → WARNING (see PROJECT_SPEC.md §16)`.
2. Every `Breach.message` is a complete sentence naming metric, observed value with unit, and the
   threshold crossed — this text renders directly in the turbine dashboard.
3. `farm_health_score`: weighted mean over non-ERROR results using `config.FARM_SCORE_WEIGHTS`.
   Returns `None` if the farm has zero turbines or every turbine is ERROR.
4. `farm_alert`: returns a reason string when any result is `CRITICAL`
   (`"2 turbines in Critical state"`) or when the ERROR fraction exceeds
   `config.FARM_ALERT_ERROR_FRACTION` (`"3 of 10 turbines reporting Error (30%)"`); otherwise `None`.
   If both fire, return both, joined by `"; "`.
5. Write `tests/test_health.py` as a **table-driven** suite. Required cases — for each threshold,
   assert at the boundary, just below, and just above:
   - Clean record → `HEALTHY`, no breaches.
   - `gearbox_temp_c` = 94.9 → healthy; 95.0 → healthy (rule is strictly `>`); 95.1 → 1 minor →
     `WARNING`; 110.1 → major → `CRITICAL`.
   - `blade_pitch_deg` = 44 with power 2000 → major → `CRITICAL`.
   - `blade_pitch_deg` = 30 with power 50 → **no breach** (conditional power gate).
   - `rotor_rpm` = 0.2 with wind 10 → major → `CRITICAL`.
   - `power_output_kw` = 0 with wind 10 → major → `CRITICAL`.
   - `power_output_kw` = 400 with wind 10 (expectation ≈ 2722, 40% ≈ 1089) → minor → `WARNING`.
   - Exactly 2 minor breaches → `WARNING`. Exactly 3 minor breaches → `CRITICAL`.
   - Any metric `None` → `ERROR` with a reason naming that metric.
   - `gearbox_temp_c` = 250 → `ERROR` (physically impossible), **not** Critical.
   - `record.timestamp` 16 minutes before `now` → `ERROR`; 14 minutes → not stale.
   - `record is None` → `ERROR`.
   - `farm_health_score` on `[HEALTHY, WARNING, CRITICAL]` → `(1.0 + 0.6 + 0.0)/3`.
   - `farm_health_score` on all-ERROR and on empty → `None`.
   - `farm_alert` fires on one CRITICAL; fires at 21% ERROR; does not fire at 19% ERROR with no
     CRITICAL.

**Verification Command:**

```bash
pytest tests/test_health.py -v --cov=src/domain/health --cov-report=term-missing && mypy src/domain/health.py
```

**Expected result:** all tests pass and `health.py` coverage is ≥95%.

---

## Phase 7 — NWP Provider (Interface + Stub)

**Objective:** Supply wind direction and air temperature — which have no telemetry source — behind a
swappable interface. The stub is what v1 actually uses; the HRRR class is a documented skeleton.

**Target Files (create):** `src/domain/nwp.py`, `tests/test_nwp.py`. **Modify:** `config.py`.

**Data Contracts:**

```python
class NWPProvider(Protocol):
    name: str
    def point_forecast(self, lat: float, lon: float, valid_time: datetime) -> PointForecast: ...
    def point_history(self, lat: float, lon: float, start: datetime,
                      end: datetime, step_hours: int = 1) -> list[PointForecast]: ...
    def grid(self, bounds: Bounds, variable: Literal["wind", "temperature"],
             valid_time: datetime) -> GridField: ...

class StubNWPProvider:   # the v1 implementation
class HRRRProvider:      # skeleton — every method raises NotImplementedError

def get_provider() -> NWPProvider: ...   # reads config.NWP_PROVIDER
```

**Step-by-Step Instructions:**

1. Add to `config.py`: `NWP_PROVIDER: str = os.environ.get("NWP_PROVIDER", "stub")`,
   `NWP_GRID_RESOLUTION = 12` (points per axis for the stub grid), and
   `NWP_STUB_SEED = 20260101`.
2. Implement `StubNWPProvider`. **Determinism is mandatory** — Streamlit reruns constantly and a
   flickering wind rose is a bug. Seed a `numpy.random.default_rng` from a hash of
   `(round(lat, 3), round(lon, 3), valid_time.timestamp() // 3600, NWP_STUB_SEED)`. Produce:
   - `wind_speed_ms` — a smooth diurnal function of the hour plus a small seeded perturbation,
     bounded to `[0.5, 22.0]`.
   - `wind_direction_deg` — a slowly-rotating bearing derived from latitude and hour, in `[0, 360)`.
   - `air_temp_c` — a latitude- and hour-dependent value bounded to `[-25, 45]`.
   - `is_simulated=True` on every returned object.
   `point_history` returns one `PointForecast` per `step_hours` step across `[start, end]`.
   `grid` returns an `NWP_GRID_RESOLUTION × NWP_GRID_RESOLUTION` `GridField` over `bounds` built from
   the same deterministic function, `is_simulated=True`.
3. Implement `HRRRProvider` with correct signatures and **no** module-level `import herbie`.
   Every method body is `raise NotImplementedError(...)` with a message explaining what is missing.
   Each docstring states the intended implementation: fetch HRRR via `herbie-data`, select the grid
   point nearest the **farm** coordinate (all turbines in a farm share farm conditions per
   `PROJECT_SPEC.md` §9), derive speed and direction from the 80 m U/V wind components, take the 2 m
   temperature field, and note that HRRR covers CONUS only so out-of-domain farms must raise
   `NWPUnavailableError`.
4. `get_provider()` returns `StubNWPProvider()` for `"stub"` and `HRRRProvider()` for `"hrrr"`;
   any other value raises `ConfigError`.
5. Tests: two calls to `point_forecast` with identical arguments return **identical** values;
   different hours return different values; all outputs are within their documented bounds and
   `is_simulated is True`; `point_history` returns the expected count; `grid` returns arrays of shape
   `(12, 12)`; every `HRRRProvider` method raises `NotImplementedError`; `get_provider()` with an
   unknown name raises `ConfigError`.

**Verification Command:**

```bash
pytest tests/test_nwp.py -v && mypy src/domain/nwp.py
```

---

## Phase 8 — Aggregates

**Objective:** Assemble the exact view-models each dashboard renders, so UI modules contain no
computation.

**Target Files (create):** `src/domain/aggregates.py`, `tests/test_aggregates.py`

**Data Contracts:**

```python
@dataclass(frozen=True, slots=True)
class FleetSummary:
    current_power_kw: float
    total_energy_mwh: float
    farm_count: int
    turbine_count: int
    status_counts: dict[HealthStatus, int]
    now_utc: datetime

@dataclass(frozen=True, slots=True)
class FarmSummary:
    farm: Farm
    local_time: datetime
    tz_label: str
    current_power_kw: float
    total_energy_mwh: float
    turbine_count: int
    status_counts: dict[HealthStatus, int]
    health_score: float | None
    alert_reason: str | None

@dataclass(frozen=True, slots=True)
class TurbineSummary:
    turbine: Turbine
    farm: Farm
    local_time: datetime
    tz_label: str
    record: TelemetryRecord | None
    health: HealthResult

@dataclass(frozen=True, slots=True)
class FarmMapRow:
    farm: Farm
    turbine_count: int
    health_score: float | None
    alert_reason: str | None

def build_fleet_summary(con, settings, now) -> FleetSummary: ...
def build_farm_summary(con, settings, now, farm_id: str) -> FarmSummary: ...
def build_turbine_summary(con, settings, now, turbine_id: str) -> TurbineSummary: ...
def build_farm_map_rows(con, settings, now) -> list[FarmMapRow]: ...
def build_turbine_map_rows(con, settings, now, farm_id: str) -> list[tuple[Turbine, HealthResult]]: ...
```

**Step-by-Step Instructions:**

1. Implement each builder by composing `queries`, `clock`, `geo`, and `health`. No SQL, no
   arithmetic that belongs in SQL.
2. `build_farm_map_rows` must run **one** `get_latest_records(con)` call and **one**
   `get_turbine_counts_by_farm(con)` call for the entire fleet, then group in Python — never a query
   per farm. This is the `PROJECT_SPEC.md` §12 requirement and a test asserts the call count.
3. Turbines that exist in `turbines` but have no row in `latest_telemetry` must still appear, with
   `classify(None, ...)` → `ERROR`. Farms with zero turbines produce `turbine_count=0`,
   `health_score=None`, `alert_reason=None`.
4. `build_turbine_summary` raises `DataLoadError` if `turbine_id` is unknown.
5. Tests against the fixture DB: fleet counts match; `FARM03` (no turbines) produces a valid
   `FarmSummary` with zeros and `health_score is None`; `TURB999` (no telemetry) classifies as
   `ERROR` and appears in `build_turbine_map_rows`; `current_power_kw` excludes stale records;
   `total_energy_mwh` matches the hand-computed fixture value; a `monkeypatch`-based call counter
   proves `build_farm_map_rows` issues ≤3 queries regardless of farm count.

**Verification Command:**

```bash
pytest tests/test_aggregates.py -v && mypy src/domain/aggregates.py
```

---

## Phase 9 — UI Foundation: State, Layout, App Shell

**Objective:** A running Streamlit app with correct state management and the responsive shell —
before any map or chart exists. First phase that may import `streamlit`.

**Target Files (create):** `src/ui/state.py`, `src/ui/layout.py`, `app.py`, `tests/test_state.py`

**Data Contracts:**

```python
class AppState(TypedDict):
    level: Level
    selected_farm_id: str | None
    selected_turbine_id: str | None
    layers: dict[str, bool]          # {"wind": False, "temperature": False, "forecast": False}
    nwp_cache: dict[str, object]
    history_window: str              # "24h" | "7d" | "all"
    history_x_metric: str
    is_mobile: bool

def init_state() -> None: ...
def get_level() -> Level: ...
def get_selected_farm_id() -> str | None: ...
def get_selected_turbine_id() -> str | None: ...
def select_farm(farm_id: str) -> None: ...
def select_turbine(turbine_id: str) -> None: ...
def reset_view() -> None: ...
def get_layer(name: str) -> bool: ...
def set_layer(name: str, value: bool) -> None: ...
def get_nwp_cached(key: str) -> object | None: ...
def set_nwp_cached(key: str, value: object) -> None: ...
def get_history_window() -> str: ...
def set_history_window(value: str) -> None: ...
def get_history_x_metric() -> str: ...
def set_history_x_metric(value: str) -> None: ...
```

**Step-by-Step Instructions:**

1. `src/ui/state.py` is the **only** file in the repository permitted to reference `st.session_state`.
   Add a test in `tests/test_architecture.py` asserting this by scanning `src/` and `app.py`.
2. `init_state()` sets every `AppState` key if absent, in one place. Defaults: `level=Level.FLEET`,
   both selections `None`, all layers `False`, empty `nwp_cache`, `history_window="24h"`,
   `history_x_metric="wind_speed_ms"`, `is_mobile=False`.
3. `select_farm(farm_id)` sets `level=FARM`, `selected_farm_id=farm_id`, `selected_turbine_id=None`.
   `select_turbine(turbine_id)` sets `level=TURBINE` and the turbine id, leaving the farm selection.
   `reset_view()` sets `level=FLEET` and clears both selections but **must not** touch `layers` or
   `nwp_cache` (`PROJECT_SPEC.md` §7.2).
4. `src/ui/layout.py`:
   - `inject_css()` — a single `st.markdown(..., unsafe_allow_html=True)` block that removes
     Streamlit's default page padding and `max-width` so the map is edge-to-edge; defines the
     `.dashboard-panel` class with an opaque background, drop shadow, `overflow-y: auto`, and a
     slide-in `@keyframes` transition; and defines the responsive rule — at
     `min-width: {MOBILE_BREAKPOINT_PX}px` and landscape orientation the panel is fixed to the left
     at `width: 33.33vw; height: 100vh`, otherwise it is fixed to the bottom at
     `width: 100vw; height: 33.33vh`. Use a single `@media` query on
     `(max-width: 767px), (orientation: portrait)` for the mobile branch. No JS measurement.
   - `render_shell() -> tuple[DeltaGenerator, DeltaGenerator]` — returns `(map_container,
     dashboard_container)` built from `st.container()`, with the dashboard container wrapped in the
     `.dashboard-panel` class.
   - `viewport_padding() -> tuple[tuple[int,int], tuple[int,int]]` — returns the
     `(padding_top_left, padding_bottom_right)` pixel pairs for `fit_bounds`: on desktop
     `((viewport_third_px, 0), (0, 0))`, on mobile `((0, 0), (0, viewport_third_px))`. Use the
     conservative constants `DESKTOP_PANEL_PX = 480` and `MOBILE_PANEL_PX = 260` added to `config.py`
     rather than attempting live measurement.
5. `app.py`:
   - `st.set_page_config(page_title="Wind Fleet Monitor", layout="wide", initial_sidebar_state="collapsed")`
     as the first Streamlit call.
   - `@st.cache_resource def get_connection()` → `db.connect(settings)`, running `db.ingest()` when
     `not db.is_ingest_current()`.
   - Wrap the whole body in `try/except WindFleetError as e: st.error(str(e)); st.stop()`.
   - Call `layout.inject_css()`, `state.init_state()`, then `layout.render_shell()`.
   - Render a header line: `Wind Fleet Monitor — Data as of: {now:%Y-%m-%d %H:%M} UTC`.
   - Render a collapsed `st.expander("Ingest summary")` with the `IngestSummary` fields.
   - Placeholder text in both containers; the map and dashboards arrive in later phases.
6. `tests/test_state.py`: import `src.ui.state` with a stubbed `st.session_state` (a plain dict
   monkeypatched onto the module) and assert the transition table from `PROJECT_SPEC.md` §7.2 —
   in particular that `reset_view()` preserves `layers` and `nwp_cache`.

**Verification Command:**

```bash
pytest tests/test_state.py tests/test_architecture.py -v && streamlit run app.py --server.headless true --server.port 8501 & sleep 12 && curl -sf http://localhost:8501/_stcore/health && kill %1
```

**Expected result:** tests pass; the health endpoint returns `ok`; no exception in the Streamlit log.

---

## Phase 10 — Charts

**Objective:** Every Plotly figure builder, as pure functions taking DataFrames and returning
`go.Figure`. Testable without rendering.

**Target Files (create):** `src/ui/charts.py`, `tests/test_charts.py`. **Modify:** `config.py`.

**Data Contracts:**

```python
def build_power_timeseries(df: pd.DataFrame, title: str) -> go.Figure: ...
def build_wind_rose(current: PointForecast, history: Sequence[PointForecast]) -> go.Figure: ...
def build_scatter_with_regression(df: pd.DataFrame, x_label: str, y_label: str,
                                  sampled_from: int | None) -> go.Figure: ...
def build_status_bar(counts: dict[HealthStatus, int]) -> go.Figure: ...
```

**Step-by-Step Instructions:**

1. Add a `CHART_HEIGHT_PX = 260` constant and a `PLOTLY_TEMPLATE = "plotly_white"` constant to
   `config.py`. Every figure uses them plus `margin=dict(l=40, r=20, t=40, b=40)` so panels stay tight.
2. `build_power_timeseries`: `go.Scatter` with `mode="lines"`, `connectgaps=False` so NULL buckets
   render as real gaps (`PROJECT_SPEC.md` §11). X axis labelled `Time (UTC)`, Y axis `Power (kW)`.
3. `build_wind_rose`: `go.Barpolar` with 16 direction bins. Two traces — history (previous 24 h)
   in gray `#BDBDBD` drawn first, current hour in the accent color drawn on top
   (`PROJECT_SPEC.md` §10.3). `angularaxis` uses `direction="clockwise"`, `rotation=90` so N is up.
   Radial axis is wind speed in m/s. Title includes the formatted readout
   `f"{speed:.1f} m/s {compass_point(direction)}"`.
4. `build_scatter_with_regression`: `go.Scattergl` markers plus a straight regression line from
   `scipy.stats.linregress`; annotate R² in the top-left. If `sampled_from` is not `None`, add a
   subtitle annotation `f"Showing {len(df):,} of {sampled_from:,} points"` — never truncate silently.
   Fewer than 3 points → return a figure containing only an "Insufficient data" annotation.
5. `build_status_bar`: a single horizontal stacked bar, one segment per status, colored from
   `config.HEALTH_COLORS`, with counts as text.
6. Tests: each builder returns a `go.Figure`; `build_power_timeseries` preserves NaN gaps and sets
   `connectgaps=False`; `build_wind_rose` produces exactly 2 traces and 16 angular bins; regression
   on a perfectly linear fixture yields R² ≈ 1.0; a 2-point input returns the "Insufficient data"
   figure without raising; `build_status_bar` colors match `config.HEALTH_COLORS`.

**Verification Command:**

```bash
pytest tests/test_charts.py -v && mypy src/ui/charts.py
```

---

## Phase 11 — Map: Fleet Layer & Fleet Dashboard

**Objective:** The default view — all farms on the map, colored by health, with the Fleet Dashboard
and a working Reset button.

**Target Files (create):** `src/ui/map_view.py`, `src/ui/dashboards/fleet.py`.
**Modify:** `app.py`, `tests/test_smoke.py`.

**Data Contracts:**

```python
def build_map(farm_rows: list[FarmMapRow], turbine_rows: list[tuple[Turbine, HealthResult]] | None,
              bounds: Bounds, level: Level, selected_farm_id: str | None,
              selected_turbine_id: str | None, padding: tuple[tuple[int,int], tuple[int,int]],
              overlays: dict[str, GridField]) -> folium.Map: ...

def extract_clicked_id(map_return: dict | None) -> tuple[str, str] | None: ...
    # returns ("farm", farm_id) | ("turbine", turbine_id) | None

def render(con, settings, now) -> None:   # dashboards/fleet.py
```

**Step-by-Step Instructions:**

1. `map_view.build_map`:
   - `folium.Map(tiles="CartoDB positron", zoom_control=True)` with no initial center; call
     `fit_bounds(bounds.as_folium(), padding_top_left=..., padding_bottom_right=...)` using the
     supplied padding so no marker lands under the dashboard (`PROJECT_SPEC.md` §8.1).
   - Farm markers: `folium.Marker` with a `folium.DivIcon` rendering a filled circle whose color
     comes from a `branca.colormap.LinearColormap` over `config.FARM_SCORE_COLORMAP_STOPS` evaluated
     at `health_score` (gray `HEALTH_COLORS["Error"]` when the score is `None`), containing the
     turbine count as centered white text. Tooltip: `f"{farm_name} ({farm_id})"`, extended with the
     alert reason when present. When `alert_reason` is not `None`, overlay a `⚠` badge in the icon
     HTML at the upper-right.
   - Embed the entity id in each marker so clicks are identifiable: set the tooltip to a string
     containing a machine-readable suffix, or attach `folium.Popup(f"__farm__{farm_id}")`. Choose one
     mechanism and use it consistently; `extract_clicked_id` parses it.
   - Farms are added to a `folium.FeatureGroup(name="farms")` so the layer can be swapped wholesale.
2. `extract_clicked_id` reads the `st_folium` return dict, preferring
   `last_object_clicked_popup`, falling back to `last_object_clicked_tooltip`, and returns `None` for
   a click on empty map area. It must never raise on a malformed or missing dict.
3. `dashboards/fleet.py::render` displays, per `PROJECT_SPEC.md` §10.2:
   - Four `st.metric` tiles: **Current Power Output** (kW, or MW above 10,000), **Total Energy (MWh)**
     — note the corrected label — **Total Farms**, **Total Turbines**.
   - The fleet status bar from `charts.build_status_bar`.
   - The fleet power time series from `charts.build_power_timeseries`, using the bucket for
     `state.get_history_window()` from `config.BUCKET_BY_WINDOW`.
4. In `app.py`: render the map into `map_container` with
   `st_folium(m, use_container_width=True, height=<viewport height>, returned_objects=["last_object_clicked_popup", "last_object_clicked_tooltip"])`.
   Restricting `returned_objects` is required — the default return payload triggers a rerun on every
   pan and zoom.
5. Handle the click with the guarded pattern from `CLAUDE.md` §5.1: only call `state.select_farm` and
   `st.rerun()` when the parsed id differs from the current selection.
6. Add the **Reset View** button fixed to the bottom-right of the map area (a `st.button` inside a
   CSS-positioned container). It calls `state.reset_view()` then `st.rerun()`.
7. Extend `tests/test_smoke.py`: `build_map` returns a `folium.Map` for fleet-level fixture data;
   the rendered HTML contains every farm id; a farm with `health_score is None` renders the Error
   color; `extract_clicked_id` correctly parses a farm click, returns `None` for `None`, `{}`, and a
   dict with all-`None` values.

**Verification Command:**

```bash
pytest tests/test_smoke.py -v && streamlit run app.py --server.headless true --server.port 8501 & sleep 12 && curl -sf http://localhost:8501/_stcore/health && kill %1
```

**Manual check (record the result):** the fleet view fits all 10 farms, no marker sits behind the
left panel, and Reset View returns to the fleet after drilling in.

---

## Phase 12 — Farm Level: Turbine Layer & Farm Dashboard

**Objective:** Click a farm → zoom to its turbines and render the Farm Dashboard.

**Target Files (create):** `src/ui/dashboards/farm.py`. **Modify:** `src/ui/map_view.py`, `app.py`,
`tests/test_smoke.py`.

**Data Contracts:** `def render(con, settings, now, farm_id: str) -> None`

**Step-by-Step Instructions:**

1. Extend `build_map` to draw the turbine `FeatureGroup` when `level` is `FARM` or `TURBINE`:
   `folium.CircleMarker` per turbine, `fill_color` from `HealthResult.color` (discrete, not the
   continuous farm scale), `radius=8`, `weight=1`. Tooltip `f"{turbine_id} — {status}"`. The parent
   farm marker stays visible at `opacity=0.4`.
2. In `app.py`, when `level != FLEET`, compute bounds with `geo.farm_view_bounds` and pass the same
   occlusion padding.
3. `dashboards/farm.py::render` displays, per `PROJECT_SPEC.md` §10.3:
   - Farm name and coordinates to 4 decimal places.
   - Local time: `f"{local:%H:%M} {tz_label} ({now:%H:%M} UTC)"`.
   - **Current Power Output** (kW) and **Total Energy (MWh)** tiles.
   - Weather block from `nwp.get_provider()`: text readout `f"{speed:.1f} m/s {compass_point(dir)}"`,
     the wind rose from `charts.build_wind_rose` using `point_forecast(now)` for the current petal and
     `point_history(now - 24h, now)` for the gray petals, and air temperature in °C with °F in
     parentheses. When `is_simulated`, render a visible `st.caption("⚠ Simulated data — NWP provider not connected")`.
   - Cache the NWP result in `state.set_nwp_cached(f"farm:{farm_id}:{now.isoformat()}")` so reruns
     do not recompute it.
   - Turbine count and the four status counts, each colored per `config.HEALTH_COLORS`.
   - Farm power time series.
   - A "◀ Back to Fleet" button calling `state.reset_view()`.
4. Handle the turbine click in `app.py` with the same guarded pattern.
5. Zero-turbine farms (`FARM03` in fixtures, 8 of 10 in the seed data) must render the dashboard with
   zeros and an explanatory `st.info("No turbines registered at this farm.")` — never an exception.

**Verification Command:**

```bash
pytest tests/test_smoke.py -v && mypy src/ui && streamlit run app.py --server.headless true --server.port 8501 & sleep 12 && curl -sf http://localhost:8501/_stcore/health && kill %1
```

**Manual check:** clicking `FARM01` reveals its turbines, the panel shows farm data and a wind rose,
and clicking a zero-turbine farm shows the empty-state message.

---

## Phase 13 — Turbine Level: Turbine Dashboard

**Objective:** The operator's diagnostic view — health breakdown, raw telemetry, NWP, and the
historical scatter with both dropdowns.

**Target Files (create):** `src/ui/dashboards/turbine.py`. **Modify:** `app.py`, `tests/test_smoke.py`.

**Data Contracts:** `def render(con, settings, now, turbine_id: str) -> None`

**Step-by-Step Instructions:**

1. Render, per `PROJECT_SPEC.md` §10.4:
   - Turbine ID and coordinates; parent farm name.
   - Local time using the **farm's** timezone.
   - A large status chip colored from `HealthResult.color`, followed by an **itemized breach list** —
     one line per `Breach` showing its `message` and severity, and for `ERROR` one line per reason
     string. This is the actionable content; never collapse it to a single word.
   - Telemetry block: all five metrics with `config.METRIC_LABELS`, each prefixed by a colored dot
     indicating whether that specific metric is in breach. Below them, the record `timestamp` and the
     ingest lag `f"{record.lag_minutes:.0f} min"`.
   - NWP block: identical construction to the farm dashboard, using the **farm** coordinate, with the
     simulated badge.
   - Historical block:
     - `st.selectbox` for the x-axis over `config.METRICS` (labels from `METRIC_LABELS`), bound to
       `state.get/set_history_x_metric`. Y is `power_output_kw`, switching to `wind_speed_ms` when x
       is `power_output_kw`.
     - `st.selectbox` for the window over `24h` / `7 days` / `Full history`, bound to
       `state.get/set_history_window`.
     - `queries.get_scatter_data` with `max_points=config.MAX_SCATTER_POINTS`, rendered by
       `charts.build_scatter_with_regression`. Pass the pre-sample row count as `sampled_from` when
       down-sampling occurred.
   - A "◀ Back to Farm" button setting `level=FARM` and clearing `selected_turbine_id`.
2. A turbine with no telemetry renders the ERROR chip, the "No telemetry received" reason, and
   `st.info` in place of the telemetry and historical blocks.
3. Extend `tests/test_smoke.py` with a render-free test: for `TURB999`, `build_turbine_summary`
   returns `health.status is ERROR` and `record is None`, and the dashboard's data-prep helpers
   handle that input without raising.

**Verification Command:**

```bash
pytest -v && mypy src app.py && streamlit run app.py --server.headless true --server.port 8501 & sleep 12 && curl -sf http://localhost:8501/_stcore/health && kill %1
```

**Manual check:** clicking a turbine shows its breaches itemized; both dropdowns change the scatter;
the regression line and R² update.

> **Milestone.** Phases 0–13 constitute a complete, demonstrable product. If time is short, stop here
> and write the README (Phase 16). Phases 14–15 are the enhancement tier.

---

## Phase 14 — Map Layer Controls

**Objective:** Wind and temperature overlays with lazy loading and refresh-scoped caching, plus the
forecast ToDo placeholder.

**Target Files:** **Modify** `src/ui/map_view.py`, `app.py`, `src/ui/layout.py`.

**Step-by-Step Instructions:**

1. Render three `st.checkbox` widgets in a CSS-positioned container at the **top-right** of the map
   area, bound to `state.get_layer` / `state.set_layer`: `Wind streams`, `Temperature`,
   `Forecasted power output`.
2. **Lazy load with refresh-scoped cache** (`PROJECT_SPEC.md` §8.4): on first check of `wind` or
   `temperature`, call `nwp.get_provider().grid(bounds, variable, now)` and store the `GridField` via
   `state.set_nwp_cached(f"grid:{variable}:{bounds_key}:{now.isoformat()}")`. Unchecking hides the
   overlay but **must not** clear the cache. Re-checking reads from cache with no recomputation.
   Because `nwp_cache` lives in `st.session_state`, it dies on page refresh — exactly as specified.
3. Render each cached `GridField` as a `folium.raster_layers.ImageOverlay` (temperature) or a
   `folium.plugins.HeatMap`-style representation (wind), with `opacity=0.5` and a caption
   `"Simulated data — NWP provider not connected"` displayed adjacent to the checkbox.
4. **Forecasted power output is a ToDo.** Checking it renders exactly
   `st.info("Power output forecasting is not yet implemented. See PROJECT_SPEC.md §8.4.")` in the
   dashboard panel and adds **nothing** to the map. Do not build a download path, a model, or a
   spinner — these are explicitly out of scope.
5. Confirm `state.reset_view()` still preserves layer states and the cache.

**Verification Command:**

```bash
pytest -v && mypy src app.py && streamlit run app.py --server.headless true --server.port 8501 & sleep 12 && curl -sf http://localhost:8501/_stcore/health && kill %1
```

**Manual check:** checking Wind draws an overlay once; unchecking and re-checking is instant (no
recompute); a browser refresh clears it; the forecast checkbox shows only the ToDo message.

---

## Phase 15 — Responsive Shell & Performance Pass

**Objective:** Verify the panel repositions correctly on mobile aspect ratios and that the app stays
responsive at scale.

**Target Files:** **Modify** `src/ui/layout.py`, `src/data/queries.py`, `app.py`.
**Create:** `tests/test_performance.py`.

**Step-by-Step Instructions:**

1. Verify in a browser at 1440×900 (panel left, one-third width) and 390×844 (panel bottom,
   one-third height). Fix the CSS if the panel overlaps the map controls or the Reset button.
   Only normal PC and mobile aspect ratios need to work — no ultrawide, square, or split-screen cases.
2. Confirm markers never render beneath the panel at either aspect ratio; adjust
   `DESKTOP_PANEL_PX` / `MOBILE_PANEL_PX` in `config.py` if they do.
3. Write `tests/test_performance.py`: generate a synthetic in-memory DuckDB with **50 turbines across
   10 farms and 30 days of 5-minute telemetry** (~430k rows) *inside the test* — do not write it to
   `data/`. Assert:
   - `build_farm_map_rows` completes in < 2.0 s.
   - `get_power_timeseries` at fleet level with the `"all"` bucket returns ≤ `MAX_TIMESERIES_POINTS`.
   - `get_scatter_data` with `max_points=5000` returns ≤ 5000 rows.
   - No query function returns a DataFrame with more than `MAX_SCATTER_POINTS` rows.
4. Add `@st.cache_data(ttl=300)` wrappers in the UI layer for the aggregate builders, keyed on level,
   entity id, window, and `now`. Keep the decorators out of `src/domain/` and `src/data/`.

**Verification Command:**

```bash
pytest tests/test_performance.py -v --durations=10
```

---

## Phase 16 — README & Final Verification

**Objective:** The written deliverable and a clean end-to-end gate.

**Target Files (create):** `README.md`.

**Step-by-Step Instructions:**

1. Write `README.md` containing:
   - **Quickstart** — the exact venv, install, and `streamlit run app.py` commands, and where to put
     the three CSVs.
   - **Architecture** — the layer diagram from `CLAUDE.md` §4.1 and a one-paragraph rationale for the
     Streamlit / Folium / DuckDB choice.
   - **Key design decisions** — SQL-side aggregation, the pure domain layer enforced by
     `test_architecture.py`, dataset-relative `now`, config-driven thresholds.
   - **Assumptions** — reproduce the `PROJECT_SPEC.md` §16 table verbatim, plus every `# SPEC-GAP:`
     comment added during the build. Grep for them: `grep -rn "SPEC-GAP" src/ config.py app.py`.
   - **Tradeoffs** — what was cut and why (real HRRR ingest, power forecasting, streaming ingest,
     auth, persistent multi-user state).
   - **Scaling path** — partitioned Parquet on object storage, a columnar warehouse, pre-aggregated
     5-min/hourly/daily rollup tables, streaming ingest replacing CSV, and moving health
     classification into a scheduled job writing a `turbine_health` table rather than computing it
     per request.
   - **Known gaps / what I would do next.**
2. Confirm every `# SPEC-GAP:` comment in the source has a matching row in the README's assumptions
   table.
3. Run the full gate from a clean checkout.

**Verification Command:**

```bash
ruff format --check . && ruff check . && mypy src app.py && pytest --cov=src --cov-report=term-missing && grep -rn "SPEC-GAP" src/ config.py app.py
```

**Expected result:** all checks exit 0; `src/domain/` coverage ≥ 90%; every SPEC-GAP comment is
accounted for in `README.md`.
