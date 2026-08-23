# Wind Fleet Monitor — Project Specification

**Version:** 1.0
**Status:** Ready for implementation
**Audience:** An engineer (human or AI) implementing the app end-to-end, and a reviewer reading it as a design document.

---

## 1. Purpose

Build a Python web application that lets a Renewable Operations team monitor a fleet of wind farms from a single map-centric screen. The map is the primary interface; dashboards appear alongside it and change content as the user drills from **Fleet → Farm → Turbine**.

The app must:

1. Ingest three CSVs (`farms`, `turbines`, `telemetry`) into DuckDB.
2. Render an interactive Folium map of farm and turbine locations, color-coded by health.
3. Show a context-appropriate dashboard at each drill level.
4. Classify turbine health and surface alerts.
5. Handle missing, late-arriving, and anomalous telemetry gracefully.

### Non-goals for this version

- No data generation, synthetic-data tooling, or external API ingestion. `turbines.csv` and `telemetry.csv` will be expanded with additional rows and test cases **outside** this task; the app must simply scale to them.
- No authentication, multi-user state, or persistent database beyond the process lifetime.
- No production deployment concerns (containers, CI/CD, observability).
- No real NWP download. The NWP layer is specified as an **interface with a stub implementation** (§9).
- No power-output forecasting model. Specified as a **ToDo placeholder** (§8.4).

---

## 2. Technology Stack

| Concern | Choice | Notes |
|---|---|---|
| Web framework | **Streamlit** | Single-file-per-page reruns; state via `st.session_state`. |
| Map rendering | **Folium** via **`streamlit-folium`** | Use `st_folium(...)` to capture click events back into Python. |
| Storage / query | **DuckDB** (in-process, in-memory or local file) | All aggregation done in SQL, not pandas loops. |
| Dataframes | **pandas** | Only as the transport between DuckDB and plotting libs. |
| Charts | **Plotly** (`plotly.graph_objects` / `plotly.express`) | Time series, scatter + regression, wind rose (`barpolar`). |
| Regression | **numpy** `polyfit` or **scipy** `linregress` | Simple OLS; no ML dependency. |
| Timezones | **`timezonefinder`** + **`zoneinfo`** | Lat/lon → IANA tz → local time. |
| NWP (stubbed) | **`herbie-data`** interface only | See §9. Not called in v1. |

**Python:** 3.11+.
**Dependency file:** `requirements.txt` pinned to minor versions.

---

## 3. Input Data

### 3.1 `farms.csv`

```
farm_id,farm_name,latitude,longitude
FARM01,Prairie Ridge,41.25,-96.53
```

10 rows in the seed set. `farm_id` is the primary key.

### 3.2 `turbines.csv`

```
turbine_id,farm_id,farm_name,latitude,longitude
TURB001,FARM01,Prairie Ridge,41.263,-96.518
```

2 rows in the seed set; expect 100–500 after expansion. `turbine_id` is the primary key; `farm_id` is a foreign key to `farms`. `farm_name` is denormalized and **must be ignored** — always join to `farms` for the authoritative name.

### 3.3 `telemetry.csv`

```
turbine_id,farm_id,timestamp,received_at,power_output_kw,wind_speed_ms,rotor_rpm,blade_pitch_deg,gearbox_temp_c
TURB001,FARM01,2026-01-01T00:00:00Z,2026-01-01T00:02:00Z,2331.2,8.0,14.0,3.6,81.6
```

- 1,122 rows in the seed set; expect 100k–10M after expansion.
- Nominal interval: **5 minutes**. `timestamp` = measurement time (UTC). `received_at` = ingest time (UTC).
- Observed seed characteristics the app must tolerate: ingest lag of 1–25 minutes; ~30 missing 5-minute intervals (appearing as 10-minute gaps); anomalous values (`blade_pitch_deg` up to 44°, `gearbox_temp_c` up to 126.5 °C).
- No `wind_direction` or ambient-temperature column exists. **Wind direction and air temperature are sourced exclusively from the NWP layer** (§9), never from telemetry.

### 3.4 Configurable paths

Data directory is configurable via `DATA_DIR` environment variable, defaulting to `./data`. The app must fail with a clear, human-readable error if a file is missing or a required column is absent.

---

## 4. Architecture

```
┌─────────────────────────────────────────────────────────┐
│  app.py                    Streamlit entrypoint          │
│    ├── layout & responsive shell                         │
│    ├── navigation state machine (fleet/farm/turbine)     │
│    └── renders: map component + dashboard component      │
└───────────────┬─────────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────────┐
│  ui/                     Presentation layer              │
│    map_view.py           builds the Folium object        │
│    dashboards/fleet.py   farm.py   turbine.py            │
│    charts.py             timeseries, scatter, wind rose  │
└───────────────┬─────────────────────────────────────────┘
                │  (only ever receives typed dicts/DataFrames)
┌───────────────▼─────────────────────────────────────────┐
│  domain/                 Business logic — no Streamlit   │
│    health.py             threshold rules → HealthStatus  │
│    aggregates.py         fleet/farm/turbine roll-ups     │
│    clock.py              "now" resolution                │
│    nwp.py                NWPProvider interface + stub    │
└───────────────┬─────────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────────┐
│  data/                   Persistence layer               │
│    db.py                 DuckDB connection + ingest      │
│    queries.py            parameterized SQL, all reads    │
└─────────────────────────────────────────────────────────┘
```

**Hard rule:** `domain/` and `data/` must not import `streamlit`. This keeps business logic unit-testable without a Streamlit runtime and is a stated evaluation point.

### 4.1 File layout

```
wind-fleet-monitor/
├── app.py
├── requirements.txt
├── README.md
├── config.py                 # thresholds, colors, map defaults
├── data/                     # CSVs live here (gitignored if large)
│   ├── farms.csv
│   ├── turbines.csv
│   └── telemetry.csv
├── src/
│   ├── data/
│   │   ├── db.py
│   │   └── queries.py
│   ├── domain/
│   │   ├── health.py
│   │   ├── aggregates.py
│   │   ├── clock.py
│   │   └── nwp.py
│   └── ui/
│       ├── map_view.py
│       ├── charts.py
│       ├── layout.py
│       └── dashboards/
│           ├── fleet.py
│           ├── farm.py
│           └── turbine.py
└── tests/
    ├── test_health.py
    ├── test_aggregates.py
    └── test_ingest.py
```

---

## 5. Data Layer (DuckDB)

### 5.1 Ingest

On first run (cached with `@st.cache_resource` so it happens once per process):

```sql
CREATE TABLE farms AS SELECT * FROM read_csv_auto('farms.csv', header=true);
CREATE TABLE turbines AS SELECT turbine_id, farm_id, latitude, longitude
                        FROM read_csv_auto('turbines.csv', header=true);
CREATE TABLE telemetry AS SELECT * FROM read_csv_auto('telemetry.csv',
                        header=true, timestampformat='%Y-%m-%dT%H:%M:%SZ');
```

Then:

- Cast `timestamp` and `received_at` to `TIMESTAMP WITH TIME ZONE` (UTC).
- Create indexes: `CREATE INDEX idx_tel_turbine_ts ON telemetry(turbine_id, timestamp);` and `CREATE INDEX idx_tel_farm_ts ON telemetry(farm_id, timestamp);`
- **Deduplicate** on `(turbine_id, timestamp)`, keeping the row with the greatest `received_at` (a late-arriving record supersedes an earlier one for the same measurement time). The seed set has no duplicates; expanded sets may.
- Log a one-line ingest summary to the Streamlit sidebar/expander: row counts per table, telemetry time range, dedup count, count of rows with any NULL metric.

### 5.2 Materialized helper view

Create a `latest_telemetry` view — the most recent row per turbine by `timestamp` — since nearly every "current" figure derives from it:

```sql
CREATE VIEW latest_telemetry AS
SELECT * FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY turbine_id ORDER BY timestamp DESC) rn
  FROM telemetry
) WHERE rn = 1;
```

### 5.3 Query rules

- **All** aggregation, filtering, and time bucketing happens in SQL. Do not pull the full telemetry table into pandas.
- Every query lives in `src/data/queries.py` as a named function with typed parameters. No SQL strings in UI code.
- Use parameter binding (`?`) — never f-string interpolation of user-influenced values.
- Cache query results with `@st.cache_data(ttl=...)` keyed on the parameters.

---

## 6. Domain Logic

### 6.1 The clock — defining "now"

The dataset is historical (Jan 2026 in the seed set), so wall-clock time would make every turbine permanently stale. `domain/clock.py` exposes:

```python
def get_now(con) -> datetime:  # tz-aware UTC
```

Resolution order:

1. If env var `SIM_NOW` is set (ISO 8601), use it.
2. Otherwise use `MAX(timestamp)` across the telemetry table — i.e. the dataset's own latest measurement.

Display the resolved "now" in the app header as `Data as of: 2026-01-02 23:55 UTC` so the operator is never misled about freshness. Every "current" and staleness calculation in the app uses this single function.

### 6.2 Health classification

Health is computed **per turbine**, from that turbine's latest telemetry record, by counting threshold breaches.

**Statuses:**

| Status | Color | Rule |
|---|---|---|
| Healthy | Green `#2E7D32` | Zero breaches |
| Warning | Orange `#ED6C02` | Exactly one *minor* breach |
| Critical | Red `#C62828` | One or more *major* breaches, **or** three or more *minor* breaches |
| Error | Gray `#757575` | Telemetry missing, stale, or structurally invalid |

**Two minor breaches** falls between Warning and Critical as literally written. Resolve this by defining Warning as **one or two** minor breaches and Critical as **three or more** minor breaches (or any major). Document this in the README as a spec-gap decision.

**Error takes precedence over everything.** A turbine is Error if any of:

- No telemetry row exists at all.
- Latest `timestamp` is older than `now - STALE_AFTER_MINUTES` (default **15 min** = three missed intervals).
- Any of the five metrics is NULL, non-numeric, or outside its physically-possible range (below).

**Thresholds** — all values live in `config.py` as a single dict so they can be tuned without touching logic:

| Metric | Physically impossible (→ Error) | Minor breach | Major breach |
|---|---|---|---|
| `power_output_kw` | `< -50` or `> 5000` | `< 40%` of power-curve expectation while `4 ≤ wind_speed ≤ 15` | `≤ 0` while `4 ≤ wind_speed ≤ 25` |
| `wind_speed_ms` | `< 0` or `> 60` | `> 25` (cut-out exceeded) | — |
| `rotor_rpm` | `< 0` or `> 40` | `> 18.5` | `> 22.0`; **or** `< 0.5` while `4 ≤ wind_speed ≤ 25` |
| `blade_pitch_deg` | `< -5` or `> 95` | `> 25` while `power_output_kw > 100` | `> 40` |
| `gearbox_temp_c` | `< -40` or `> 200` | `> 95` | `> 110` |

**Power-curve expectation** (a simple reference curve, not a model): linear ramp from 0 kW at 3 m/s (cut-in) to rated 3,500 kW at 12 m/s, flat at rated from 12–25 m/s, 0 above 25 m/s. Put it in `config.py` as `POWER_CURVE`. Its only use is the power-underperformance breach.

`health.py` exposes:

```python
@dataclass
class HealthResult:
    status: HealthStatus          # enum: HEALTHY/WARNING/CRITICAL/ERROR
    minor: list[Breach]           # each: metric, value, threshold, message
    major: list[Breach]
    errors: list[str]             # human-readable reasons for ERROR

def classify(record: dict | None, now: datetime) -> HealthResult
```

Pure function, no I/O — this is the most heavily unit-tested module.

### 6.3 Farm health

- **Farm health score** = fraction of its turbines that are Healthy, weighted: Healthy 1.0, Warning 0.6, Critical 0.0, Error excluded from the denominator (but see alert rule). Score ∈ [0, 1] drives the green→red dot color via a continuous colormap (`branca.colormap.LinearColormap` from red through amber to green).
- **Farm alert** (the exclamation icon): raised when **any** turbine at the farm is Critical, **or** when Error turbines exceed 20% of the farm's turbine count. Both conditions are configurable. The icon tooltip states which condition fired and how many turbines are involved.
- If a farm has zero turbines in `turbines.csv` (true for 8 of 10 seed farms), render its dot in Error gray with count `0` and a tooltip reading "No turbines registered." Do **not** crash or omit the farm.

---

## 7. Navigation & State

### 7.1 State machine

`st.session_state` holds:

```python
{
  "level": "fleet" | "farm" | "turbine",
  "selected_farm_id": str | None,
  "selected_turbine_id": str | None,
  "map_center": (lat, lon),
  "map_zoom": int,
  "layers": {"wind": bool, "temperature": bool, "forecast": bool},
  "nwp_cache": dict,          # cleared only on page refresh
  "history_window": "24h" | "7d" | "all",
  "history_x_metric": str,
}
```

### 7.2 Transitions

| From | Action | To |
|---|---|---|
| fleet | click a farm dot | farm — set `selected_farm_id`, refit bounds to that farm's turbines, render turbine layer |
| farm | click a turbine dot | turbine — set `selected_turbine_id`, keep the turbine layer visible |
| turbine | click a different turbine dot | turbine — swap `selected_turbine_id` |
| any | click "Reset View" button | fleet — clear both selections, refit to fleet bounds, **preserve** layer checkbox states and `nwp_cache` |
| any | click empty map area | no change (do not deselect) |

Clicks are captured from the `st_folium` return value. Read `last_object_clicked_tooltip` or, preferably, embed the entity ID in each marker's popup/tooltip and parse it from the returned dict. **Guard against Streamlit rerun loops:** only mutate state and call `st.rerun()` when the clicked ID differs from the currently selected one.

---

## 8. The Map

### 8.1 Sizing and bounds

- The map fills the browser viewport (`height` computed from viewport, `width="100%"`). Use a small CSS injection to remove Streamlit's default page padding and max-width so the map is truly edge-to-edge.
- **Fleet default bounds:** compute `lat_min/lat_max/lon_min/lon_max` across all farms, expand each range by **110%** (i.e. pad by 5% of the range on each side), and `fit_bounds()` to that box.
- **Dashboard occlusion:** the dashboard covers the left third (desktop) or bottom third (mobile) of the viewport. The map canvas extends *underneath* it, but no farm or turbine marker may sit in the occluded region. Achieve this by fitting bounds to the **visible** sub-rectangle: pass `fit_bounds` a padding offset — `padding_top_left=(viewport_width/3, 0)` on desktop, `padding_bottom_right=(0, viewport_height/3)` on mobile. Folium's `fit_bounds` accepts `padding_top_left` / `padding_bottom_right` in pixels; use them rather than manipulating the coordinate box.
- **Farm-level bounds:** same 110% expansion and same occlusion padding, computed over the selected farm's turbine coordinates. If the farm has one turbine (or none), fall back to centering on the farm coordinate at zoom 13.

### 8.2 Fleet (Wind Farms) layer

- One marker per farm, rendered as a `folium.Marker` with a `DivIcon` — a filled circle whose fill color comes from the health colormap, containing the **turbine count** as centered white text.
- **Hover tooltip:** `{farm_name} ({farm_id})`.
- **Alert:** when the farm alert condition fires, overlay a small exclamation badge on the upper-right of the dot. Tooltip extends to include the alert reason.
- Markers are added to a named `FeatureGroup` so the layer can be swapped wholesale on drill-down.

### 8.3 Turbine layer

- Shown only when `level` is `farm` or `turbine`. One `CircleMarker` per turbine belonging to `selected_farm_id`.
- Fill color = the turbine's discrete health color (§6.2), not a continuous scale.
- **Hover tooltip:** `{turbine_id} — {status}`.
- The currently selected turbine gets a thicker stroke / larger radius so it is visually distinguishable.
- The parent farm dot remains visible but dimmed (reduced opacity) for context.

### 8.4 Map controls (top-right corner)

Rendered as Streamlit checkboxes in a floating container overlaid on the map, or via `folium.LayerControl` where feasible. Streamlit checkboxes are preferred because their state must drive Python-side data loading.

| Control | Behavior |
|---|---|
| ☐ Wind streams | On first check, request gridded wind from the NWP provider (§9), cache in `session_state["nwp_cache"]`, render as an overlay. Unchecking hides but **does not** discard the cache. Cache lives until page refresh. |
| ☐ Temperature | Same lifecycle, temperature grid. |
| ☐ Forecasted power output | **ToDo placeholder.** Checking it renders an `st.info("Power output forecasting is not yet implemented.")` panel in the dashboard and nothing on the map. Do not build the download, the model, or the spinner — those are explicitly out of scope for v1. |

Because the NWP provider is stubbed in v1 (§9), the wind and temperature checkboxes render the stub's synthetic grid and display a clearly visible "Simulated data — NWP provider not connected" caption.

### 8.5 Reset button

Fixed to the **bottom-right** of the map area. Label: "Reset View". Returns to the fleet layer per §7.2.

---

## 9. NWP Layer — Interface + Stub

Wind direction and air temperature have no source in the telemetry schema, so they come exclusively from here.

Define in `domain/nwp.py`:

```python
class NWPProvider(Protocol):
    def point_forecast(self, lat: float, lon: float, valid_time: datetime
                       ) -> PointForecast: ...
    def point_history(self, lat: float, lon: float,
                      start: datetime, end: datetime) -> list[PointForecast]: ...
    def grid(self, bounds: Bounds, variable: Literal["wind","temperature"],
             valid_time: datetime) -> GridField: ...

@dataclass
class PointForecast:
    valid_time: datetime
    wind_speed_ms: float
    wind_direction_deg: float     # meteorological convention: direction wind is FROM
    air_temp_c: float
```

Two implementations:

1. **`StubNWPProvider`** — **the one wired up in v1.** Returns deterministic synthetic values seeded from `(lat, lon, valid_time)` so results are stable across reruns. Every value it returns is flagged `is_simulated=True`, and the UI must render a visible "Simulated" badge wherever stub data appears.
2. **`HRRRProvider`** — **ToDo stub only.** Class exists with full docstrings and correct signatures; every method raises `NotImplementedError`. Docstrings state the intended implementation: use `herbie-data` to fetch HRRR, select the nearest grid point to the **farm** coordinate (all turbines in a farm share the farm's conditions), derive wind speed/direction from the 80 m U/V components, and take 2 m temperature. Note that HRRR covers CONUS only, so farms outside that domain must degrade gracefully.

Provider selection is a single line in `config.py` (`NWP_PROVIDER = StubNWPProvider`), so swapping in the real one later is a one-line change.

---

## 10. Dashboards

### 10.1 Shell and responsive behavior

- **Desktop / landscape** (viewport width ≥ 768px **and** width > height): dashboard occupies the **left third** of the screen, full height, scrollable, with a slide-in transition.
- **Mobile / portrait** (width < 768px **or** height ≥ width): dashboard occupies the **bottom third**, full width, scrollable, sliding up from the bottom.
- Detect orientation via a small CSS media-query-driven class on the container plus a `st.session_state` hint; do not attempt pixel-perfect JS measurement. Only normal PC and mobile aspect ratios need to be handled — no ultrawide, no square, no split-screen edge cases.
- The dashboard has a subtle drop shadow and an opaque background so the map beneath it is not distracting.
- Every dashboard has a header showing the current level and, for farm/turbine levels, a "◀ Back" affordance that steps up one level.

### 10.2 Fleet Dashboard (default on load)

| Element | Definition |
|---|---|
| Current Power Output | Sum of `power_output_kw` across all turbines' latest records that are **not** stale. Format `X,XXX kW` (or MW above 10,000). |
| Total Power Output | Energy over the full dataset: `SUM(power_output_kw) × (5/60)` → MWh. Label explicitly as **"Total Energy (MWh)"** — "Total Power Output" is a units error and the label must be corrected in the UI. |
| Total Number of Farms | `COUNT(*)` from `farms`. |
| Total Number of Turbines | `COUNT(*)` from `turbines`. |
| Health breakdown | Small stacked bar or four counters: Healthy / Warning / Critical / Error across the fleet. *(Addition — cheap, and the fleet view is otherwise blind to health.)* |
| Time Series Plot | Fleet-wide `power_output_kw` summed per 5-minute bucket over the selected window. Line chart, x = UTC time, y = kW. |

### 10.3 Farm Dashboard

| Element | Definition |
|---|---|
| Farm Name and Coordinates | From `farms`. Coordinates to 4 decimal places. |
| Current Local Time at Farm | `now` (§6.1) converted via `timezonefinder` lookup on the farm coordinate. Show both: `14:55 MST (21:55 UTC)`. |
| Current Power Output | Sum across the farm's non-stale latest records, kW. |
| Total Energy | Farm-scoped MWh, same formula and same label correction as §10.2. |
| Current Weather — Wind | From the NWP provider at the farm coordinate. Text readout `7.6 m/s NNW`, plus a **wind rose**: current hour's direction as a colored petal, previous 24 hours as gray petals behind it. 16-point compass binning. |
| Current Weather — Air Temperature | NWP provider, °C, with °F in parentheses. |
| Total Number of Turbines | Count for this farm. |
| Health counts | Four numbers: Healthy / Warning / Critical / Error, each with its status color. |
| Time Series Plot | Farm-summed `power_output_kw` per 5-minute bucket over the selected window. |

Whenever NWP data is stub-sourced, the weather block carries a "Simulated" badge.

### 10.4 Turbine Dashboard

| Element | Definition |
|---|---|
| Turbine ID and Coordinates | From `turbines`. |
| Current Local Time at Farm | Same as §10.3 — uses the parent **farm's** timezone. |
| Health Status | Large colored status chip. Below it, an itemized list of every breach: metric, observed value, threshold crossed, severity. If Error, list the specific reasons. This is the operator's actionable content — do not reduce it to a single word. |
| Telemetry Data | The five metrics from the latest record, each with unit, value, and a small colored dot indicating whether that specific metric is in breach. Show the record's `timestamp` and the ingest lag (`received_at - timestamp`) so the operator can see data freshness. |
| NWP Forecast Data | Wind speed & direction (wind rose, same construction as §10.3, using the **farm** coordinate) and air temperature. Simulated badge in v1. |
| Historical Data | Scatter plot, default x = `wind_speed_ms`, y = `power_output_kw`, with an OLS regression line and R² annotation. Points colored by whether they were flagged as breaches. |
| — x-axis dropdown | Selects any telemetry metric for the x-axis: power output, wind speed, rotor RPM, blade pitch, gearbox temp. y-axis stays `power_output_kw` unless x is power output, in which case y switches to `wind_speed_ms`. |
| — time-window dropdown | `24h` / `7 days` / `Full history`, relative to `now`. Drives both the scatter and any turbine time series. Down-sample above ~5,000 points to keep rendering responsive. |

---

## 11. Handling Bad Data

| Situation | Behavior |
|---|---|
| Missing 5-min interval | Time series shows a genuine **gap** (insert NULLs at expected intervals via a generated time spine, `connectgaps=False`), not an interpolated line. |
| Late-arriving record | Ordered by `timestamp` for display; `received_at` surfaced as ingest lag. Dedup keeps the latest `received_at` per `(turbine_id, timestamp)`. |
| Anomalous value within physical range | Counted as a threshold breach → drives Warning/Critical. Still plotted. |
| Value outside physical range | Turbine → Error. Value excluded from aggregates and plots; the exclusion is noted in the UI. |
| Turbine with no telemetry at all | Error status, gray dot, dashboard shows "No telemetry received." |
| Farm with no turbines | Gray dot, count 0, tooltip "No turbines registered." Farm dashboard renders with zeros and an explanatory note. |
| Empty or malformed CSV | App fails fast at startup with a specific, readable error naming the file and the problem. |

---

## 12. Scalability Requirements

The seed set is trivial; the expanded set will not be. Build for it from the start:

- **Never** `SELECT *` from telemetry into pandas. All roll-ups are SQL aggregations.
- Persist DuckDB to a local file (`fleet.duckdb`) and skip re-ingest if the file exists and the CSV mtimes are unchanged. Show ingest time in the sidebar.
- Cache query results with `@st.cache_data`; key on level, entity ID, and window. Cache the DuckDB connection with `@st.cache_resource`.
- Time series must be **bucketed in SQL** (`time_bucket` / `date_trunc`) to a resolution appropriate to the window: 5 min for 24h, 1 hour for 7 days, 6 hours for full history. Never return more than ~2,000 points to the browser.
- Scatter plots down-sample above 5,000 points using deterministic reservoir sampling, with the sample size stated on the chart.
- Health classification for the map runs as a **single query** producing one row per turbine, then a vectorized classification pass — not a per-turbine query loop.
- Document in the README the path beyond this: partitioned Parquet on object storage, a columnar warehouse, pre-aggregated 5-min/hourly/daily rollup tables, and a streaming ingest path replacing CSV.

---

## 13. Testing

`pytest`, no Streamlit runtime required.

- `test_health.py` — table-driven cases covering: clean record → Healthy; one minor → Warning; two minors → Warning; three minors → Critical; one major → Critical; NULL metric → Error; stale timestamp → Error; out-of-physical-range → Error; each threshold at, just below, and just above its boundary.
- `test_aggregates.py` — fleet and farm roll-ups against a fixture with known sums; verify stale records are excluded from "current" figures; verify energy conversion.
- `test_ingest.py` — dedup on `(turbine_id, timestamp)` keeps the greatest `received_at`; missing column raises a clear error; timestamps parse to tz-aware UTC.
- A smoke check that the app imports and builds a Folium map object for each of the three levels without raising.

---

## 14. Implementation Phases

Build in this order; each phase leaves the app runnable.

1. **Data layer** — DuckDB ingest, schema, dedup, `latest_telemetry` view, queries module. Verify with `test_ingest.py`.
2. **Domain layer** — `clock.py`, `health.py`, `aggregates.py`. Verify with unit tests. *No UI yet.*
3. **Map + fleet level** — Folium fleet layer with health colors, counts, tooltips, alerts, correct 110% bounds with occlusion padding; Fleet Dashboard; Reset button.
4. **Farm level** — click-to-drill, turbine layer, Farm Dashboard including local time and health counts.
5. **Turbine level** — Turbine Dashboard, telemetry readout, breach itemization, historical scatter with both dropdowns.
6. **NWP stub** — provider interface, `StubNWPProvider`, wind rose, air temperature, simulated badges, `HRRRProvider` skeleton.
7. **Map layer controls** — wind/temperature overlays from the stub with lifecycle caching; forecast checkbox as ToDo placeholder.
8. **Responsive shell** — left/bottom dashboard positioning, slide transitions, CSS full-bleed map.
9. **Polish** — ingest summary panel, README, docstrings.

If time runs short, phases 1–5 constitute a coherent, demonstrable product. Phases 6–8 are the enhancement tier.

---

## 15. Deliverables

1. Runnable app: `streamlit run app.py` works from a clean clone after `pip install -r requirements.txt`, with the three CSVs in `./data`.
2. `README.md` covering: how to run, architecture summary, key design decisions, assumptions, tradeoffs, known gaps, and the scaling path.
3. Passing `pytest` suite.

---

## 16. Open Decisions Recorded

These are spec gaps resolved by judgment; each is called out in the README so a reviewer can challenge them.

| Gap | Resolution |
|---|---|
| Two minor breaches is unspecified (Warning is "one minor", Critical is "three minor") | Warning = 1–2 minor; Critical = ≥3 minor or ≥1 major. |
| "Total Power Output" mixes power and energy | Rendered as "Total Energy (MWh)" = `Σ kW × 5/60`. |
| No `wind_direction` or air-temp column in telemetry | Both sourced exclusively from the NWP provider, which is stubbed in v1 and clearly badged as simulated. |
| Dataset is historical, so wall-clock "now" makes everything stale | `now` = `MAX(telemetry.timestamp)`, overridable via `SIM_NOW`. Surfaced in the header. |
| Numeric thresholds were not specified | Defaults set in `config.py` per §6.2, derived from the seed data's distributions and typical utility-scale turbine specs. Explicitly tunable. |
| Farm alert trigger was left incomplete in the source spec | Any Critical turbine, or >20% Error turbines. Configurable. |
| 8 of 10 seed farms have no turbines | Rendered in Error gray with count 0 and an explanatory tooltip, not hidden. |
