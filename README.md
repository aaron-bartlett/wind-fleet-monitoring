# Wind Fleet Monitor

A Streamlit application for monitoring a fleet of wind farms. A full-viewport Folium map drills
**Fleet → Farm → Turbine**, with a context-sensitive dashboard alongside it. Three CSVs are
ingested into DuckDB; all aggregation happens in SQL.

- **Specification:** [`PROJECT_SPEC.md`](PROJECT_SPEC.md) — what to build
- **Build order:** [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) — phased plan
- **Engineering guardrails:** [`CLAUDE.md`](CLAUDE.md) — how code must be written

---

## Quick start

Requires **Python 3.11+** (the codebase uses `zoneinfo`, `datetime.UTC`, PEP 604 unions).

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Opens at `http://localhost:8501`. On first run the app ingests `data/*.csv` into
`data/fleet.duckdb` (git-ignored, rebuilt whenever a source file changes); subsequent starts
reuse it.

For development, install the test/lint toolchain instead:

```bash
pip install -r requirements-dev.txt
```

### Commands

| Purpose | Command |
|---|---|
| Run the app | `streamlit run app.py` |
| Run all tests | `pytest` |
| Tests with coverage | `pytest --cov=src --cov-report=term-missing` |
| Lint | `ruff check .` |
| Format | `ruff format .` |
| Type check | `mypy src app.py` |
| **Full gate** | `ruff format --check . && ruff check . && mypy src app.py && pytest` |

The full gate is the definition of done for any change. It currently passes with
**279 tests**.

### Configuration

All settings are environment variables read by `config.py`. None are required.

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `data` | Directory holding the three source CSVs |
| `DUCKDB_PATH` | `data/fleet.duckdb` | Database file; `:memory:` is supported |
| `SIM_NOW` | unset | Override the app's "now" (ISO-8601, must carry a timezone) |
| `NWP_VALID_TIME` | unset | Override the weather valid-time independently of `SIM_NOW` |
| `STALE_AFTER_MINUTES` | `15` | Telemetry staleness threshold |
| `NWP_PROVIDER` | `stub` | Weather provider. Only `stub` is enabled — see §16 |

---

## Architecture

Dependencies point one direction only:

```
app.py  ──►  src/ui/  ──►  src/domain/  ──►  src/data/
             Streamlit     pure logic       DuckDB
             Folium        no I/O           all SQL
             Plotly
```

| Layer | Owns | May import |
|---|---|---|
| `config.py` | Every constant, threshold, colour, cap. No logic. | stdlib |
| `src/data/` | `db.py` (DDL + ingest), `queries.py` (**all** read SQL) | stdlib, duckdb, pandas, config |
| `src/domain/` | `health.py`, `aggregates.py`, `clock.py`, `geo.py`, `nwp.py`, `models.py` | + numpy, `src/data/` |
| `src/ui/` | `state.py`, `layout.py`, `map_view.py`, `charts.py`, `dashboards/` | everything |
| `app.py` | Wiring and page config only | everything |

**The layering is enforced mechanically, not by convention.**
`tests/test_architecture.py` AST-scans `src/domain/` and `src/data/` and fails the build if
either imports Streamlit, Folium, `streamlit_folium`, branca, or Plotly; if raw SQL appears
outside `db.py`/`queries.py`; or if `st.session_state` is touched outside `src/ui/state.py`.

That constraint is what makes the whole test suite runnable without a Streamlit runtime, a
browser, or network access.

Three conventions carry disproportionate weight:

- **Units in names.** `power_kw`, `wind_speed_ms`, `gearbox_temp_c`, `energy_mwh`. Unit
  confusion is this domain's most common bug class.
- **Timezone-aware datetimes everywhere.** A naive `datetime` anywhere in the codebase is a bug.
- **State behind one door.** Every read and write of `st.session_state` goes through a typed
  accessor in `state.py`, and every `st.rerun()` is guarded by a value-changed check — an
  unguarded rerun on a map click is an infinite loop.

---

## Data

### Input

Three CSVs in `data/`, ingested on startup:

| File | Rows | Contents |
|---|---|---|
| `farms.csv` | 10 | `farm_id, farm_name, latitude, longitude` |
| `turbines.csv` | 50 | `turbine_id, farm_id, farm_name, latitude, longitude` |
| `telemetry.csv` | 28,263 (2.2 MB) | 5-minute readings across 2 days |

Telemetry columns: `turbine_id, farm_id, timestamp, received_at, power_output_kw,
wind_speed_ms, rotor_rpm, blade_pitch_deg, gearbox_temp_c`.

There is **no wind-direction and no ambient-temperature channel** — a fact that shapes the
weather block (§16).

### Ingest and validation

Ingest runs inside a single transaction with `ROLLBACK` on failure, so a bad file never leaves
a half-built schema behind. Validation is fail-fast and names the file and the problem:

1. **Missing file** → `DataLoadError` naming the path, before any parsing.
2. **Missing column** → the staging table is `DESCRIBE`d and diffed against the required set;
   the error names the file *and* the missing columns.
3. **Malformed timestamp** → `timestamp` and `received_at` are read as `VARCHAR` specifically
   to **defeat DuckDB's type sniffer**, then cast with an explicit
   `strptime(..., '%Y-%m-%dT%H:%M:%SZ')`. Left to sniff, a bad timestamp would be silently
   coerced to `NULL` and the app would quietly show a gap instead of reporting bad data.

`app.py` catches `WindFleetError` at the top level and renders `st.error(...)` then
`st.stop()`. A user never sees a raw traceback.

**Duplicate timestamps** are resolved last-write-wins on arrival:

```sql
ROW_NUMBER() OVER (PARTITION BY turbine_id, timestamp ORDER BY received_at DESC) = 1
```

`(turbine_id, timestamp)` is the natural key; `received_at` is the tiebreaker, so a
re-transmitted reading supersedes the original deterministically regardless of row order in
the file. The count removed is surfaced in the app's ingest summary rather than dropped
silently.

**Missing values** are handled differently at each layer, on purpose:

| Layer | Behaviour |
|---|---|
| Ingest | `NULL` preserved — never imputed, interpolated, or dropped. Null count reported. |
| Aggregation | `NULL` excluded, so a missing reading never contributes a zero |
| Health | A `NULL` metric on a present record classifies as `Error`, not `Healthy` |
| Charts | A generated time spine `LEFT JOIN` yields `NaN` buckets; `connectgaps=False` renders a **genuine gap**, not an interpolated line |
| Render | Zero-turbine farms, telemetry-less turbines, and empty windows show a message — never raise |

---

## Health classification

`src/domain/health.py` is the single source of truth for the map dot colour, the farm alert
badge, and the turbine breach list. Four statuses: **Healthy**, **Warning**, **Critical**,
**Error**.

`Error` is a separate axis and it **short-circuits**: a missing, stale, or structurally invalid
record is classified before any threshold rule runs, so its breach lists are always empty.
*"I cannot tell you how this turbine is doing"* is a different statement from *"this turbine is
fine,"* and collapsing them would let a dead sensor render green.

Only once a record passes all three `Error` checks are the five metrics evaluated. Severity
aggregates from counts, not from a single worst metric: **Warning = 1–2 minor breaches;
Critical = ≥3 minor or ≥1 major.**

Several rules are **conditional**, because context changes meaning — zero power output is
normal below cut-in wind speed and a major fault at 10 m/s. Power is checked against a
reference power curve within a wind-speed window; blade pitch is only checked above 100 kW.
A threshold that ignores operating context generates alerts operators learn to ignore.

Every threshold lives in `config.py` and is tunable without touching logic.
`tests/test_health.py` is the most heavily tested module in the project — table-driven, with
each threshold exercised *at*, *just below*, and *just above* its boundary.

---

## Testing

**279 tests across 15 files**, mirroring the source tree. Three standing constraints: no test
requires a Streamlit runtime, no test requires network access, and no test reads the real
`data/` CSVs — `tests/fixtures/` only, since the shipped dataset is expected to grow and would
silently invalidate assertions.

| Suite | Covers |
|---|---|
| `test_health.py` | Every threshold boundary, table-driven |
| `test_architecture.py` | Layering rules, by AST scan |
| `test_ingest.py` | Missing file, missing column, bad timestamp, duplicate resolution |
| `test_queries.py` | Every read query, plus SQL-injection rejection |
| `test_performance.py` | A synthetic **432,000-row** fleet (10 farms × 50 turbines × 30 days) |
| `test_smoke.py` | Folium map builds at all three drill-down levels |
| `test_charts.py`, `test_geo.py`, `test_clock.py`, `test_nwp.py`, … | Per-module logic |

Tests assert on values, never on "no exception raised."

---

## Deployment

A `Dockerfile` at the repo root produces a self-contained image intended for **Google Cloud
Run**:

```bash
docker build --platform linux/amd64 -t wind-fleet-monitor .
docker run --rm -p 8080:8080 -e PORT=8080 wind-fleet-monitor

gcloud run deploy SERVICE_NAME --source . --region REGION \
  --allow-unauthenticated --port 8080 --session-affinity \
  --timeout 3600 --memory 2Gi --cpu 2 --min-instances 1
```

Four non-obvious constraints, all encoded in the config:

- **`--session-affinity` is mandatory.** Streamlit runs over a websocket; without sticky
  routing the page hangs at "Please wait…" forever. This is the most common way a Streamlit
  deployment fails.
- **The platform is pinned to `linux/amd64`.** Building on Apple Silicon defaults to arm64,
  and `timezonefinder` ships x86_64 wheels only — the build fails trying to compile a C
  extension in a compiler-less slim image.
- **Only `/tmp` is writable**, so the image sets `DUCKDB_PATH=/tmp/fleet.duckdb`. Safe because
  the database is *derived* — rebuilt from the committed CSVs on boot.
- **Health check path** is `/_stcore/health`.

---

## Scaling path

The project's stated scalability position (`PROJECT_SPEC.md` §12) is that **aggregation is
SQL's job**. Concretely, already in place:

- No `SELECT *` from `telemetry` into pandas anywhere. Every roll-up is `SUM` /
  `time_bucket` / window functions.
- Indexes on `(turbine_id, timestamp)` and `(farm_id, timestamp)`.
- Fixed, small query counts per view — a fleet roll-up does not issue one query per farm.
- Everything sent to the browser is capped and down-sampled **in SQL**: 2,000 points for time
  series, 5,000 for scatter. A query that would exceed its cap raises rather than silently
  truncating.
- `@st.cache_resource` on the connection (ingest runs at most once per process) and
  `@st.cache_data` with a 300 s TTL on roll-ups — applied in the UI layer only, so
  `src/data/` stays Streamlit-free.
- `st_folium` is called with `returned_objects` restricted to the two click keys, so pans and
  zooms do not trigger reruns.

**What breaks first, in order, as scale grows:**

1. **`latest_telemetry`** — a full-table window function evaluated on every health roll-up.
   First real query hot spot. Fix: materialize it at ingest instead of leaving it a view.
2. **Cold-start ingest.** Full rebuild happens in-process before the first render. At millions
   of rows this is a visibly slow first request on a scale-to-zero instance. Fix: build the
   database at image-build time and ship it in the container.
3. **Concurrent users.** The real ceiling, and unrelated to row count — Streamlit holds a
   server-side session per connection and re-executes per interaction. Tens of simultaneous
   operators, not millions of rows, is what forces an architecture change.
4. **The map.** Folium renders markers as DOM elements; a few thousand turbines needs
   clustering or a canvas renderer regardless of backend speed.

At **10 GB** the container is the first casualty, not the database: `/tmp` on Cloud Run is a
memory-backed tmpfs. That needs a mounted volume or a pre-built image, plus a move from
full-rebuild ingest to date-partitioned Parquet with append-only loads.

---

## Assumptions

- **"Now" is the dataset's own latest timestamp**, not the wall clock. The seed data is
  historical (January 2026), so wall-clock time would mark every turbine permanently stale.
- **Telemetry arrives every 5 minutes.** `TELEMETRY_INTERVAL_MINUTES = 5` is the basis for the
  energy calculation (`Σ kW × 5/60 ÷ 1000`).
- **A duplicate `(turbine_id, timestamp)` is a re-transmission**, not two distinct readings.
- **All turbines share one model.** `RATED_POWER_KW = 3500`, cut-in 3 m/s, rated 12 m/s,
  cut-out 25 m/s are global constants.
- **The reference power curve is a heuristic, not a model.** It is a piecewise-linear
  expectation used to detect underperformance, not a certified turbine curve.
- **The app is read-only and single-operator.** No writes, no auth, no multi-tenancy.

---

## Tradeoffs

| Decision | Bought | Cost |
|---|---|---|
| **Streamlit** over React + API | No client/server boundary; effort goes to logic, not plumbing | Full script rerun per interaction; awkward concurrency story |
| **DuckDB** over Postgres | Zero ops, native CSV, columnar engine matched to the query shape | Single-writer, process-embedded; no continuous ingest |
| **Strict layering** over a flat module | Whole suite runs without a UI runtime; provider swapped 3× with no data-layer change | More indirection than a project this size strictly needs |
| **Rebuild DB from CSV on boot** | Deployment is stateless; `/tmp` is sufficient | Cold-start cost scales with file size |
| **Fixed-pixel map height** | Works within `st_folium`'s API | Approximates "fills the viewport" rather than achieving it |
| **Everything capped at 2k/5k points** | Predictable payloads at any data size | Long windows are coarser than the raw data |

---

## Known gaps

Stated plainly rather than glossed:

- **No CI/CD.** The full gate is a local command; deployment is a manual `gcloud run deploy`.
  A GitHub Actions workflow running the gate on PR and deploying on merge is the next step.
- **No end-to-end UI tests.** Nothing exercises `dashboards.*.render()` — those call `st.*`
  and need a runtime. Streamlit's `AppTest` harness would cover the click → state → rerun path,
  which is where Streamlit bugs actually live.
- **Live weather is disabled.** See §16. `HRRRProvider` and `src/data/hrrr.py` are complete and
  unit-tested but unreachable; `get_provider()` rejects `NWP_PROVIDER=hrrr`.
- **Power-output forecasting is not implemented.** Deferred by `PROJECT_SPEC.md` §8.4; the
  checkbox renders the documented placeholder.
- **Health thresholds are global.** Real fleets mix turbine models with different rated power
  and cut-out speeds.
- **Ingest is full-rebuild**, keyed on source-file mtime and size. Correct for batch CSV,
  wrong for a live feed.
- **`is_mobile` has no live detection.** Responsive behaviour is entirely CSS; the flag exists
  so a later phase can wire detection without changing `state.py`'s contract.

---

## 16. Recorded Decisions

> Numbered **16** to match `PROJECT_SPEC.md` §16 and the `# SPEC-GAP:` comments throughout the
> source, which cite "README §16". Each row is a spec gap resolved by judgment, recorded here
> so a reviewer can challenge it.

| # | Gap | Resolution | Where |
|---|---|---|---|
| 1 | Two minor breaches is unspecified — Warning is "one minor", Critical is "three minor" | **Warning = 1–2 minor; Critical = ≥3 minor or ≥1 major.** Three simultaneous minor anomalies usually indicate a systemic problem, not three coincidences. | `config.py`, `health.py` |
| 2 | "Total Power Output" conflates power and energy | Rendered as **Total Energy (MWh)** = `Σ kW × 5/60 ÷ 1000`. Power and energy are different quantities. | `queries.py` |
| 3 | Dataset is historical, so a wall-clock "now" marks everything stale | `now` resolves to `MAX(telemetry.timestamp)`, overridable by `SIM_NOW`, falling back to the wall clock only if telemetry is empty. Surfaced in the header. | `clock.get_now` |
| 4 | Numeric thresholds were not specified | Defaults in `config.py` per `PROJECT_SPEC.md` §6.2, derived from the seed distributions and typical utility-scale specs. Explicitly tunable. | `config.py` |
| 5 | Farm alert trigger left incomplete in the source spec | Any Critical turbine, **or** >20% Error turbines. Both configurable (`FARM_ALERT_ON_ANY_CRITICAL`, `FARM_ALERT_ERROR_FRACTION`). | `config.py` |
| 6 | Farms may have zero turbines | `health_score` is `None`, the marker renders in Error grey with its turbine count (`0`) shown in the dot, and the farm dashboard says "No turbines registered at this farm" — never hidden. The shipped dataset now populates all 10 farms; the zero-turbine path is exercised by `tests/fixtures/` and asserted in `test_smoke.py`. | `aggregates.py`, `map_view.py` |
| 7 | No `wind_direction` or ambient-temperature column exists in telemetry | **Superseded** — originally sourced from the NWP stub. Now the wind rose draws petal *length* from real telemetry `wind_speed_ms` and petal *angle* from a deterministic theoretical bearing; air temperature is likewise synthetic. The caption states exactly which half is measured. | `nwp.telemetry_wind_rose` |
| 8 | Live HRRR weather was specified as a `NotImplementedError` skeleton, then built by request, then withdrawn | `HRRRProvider` and `src/data/hrrr.py` are complete and unit-tested but **disabled**: `get_provider()` raises `ConfigError` on `NWP_PROVIDER=hrrr`. The scientific stack is out of `requirements.txt`. Re-enabling means restoring those pins and removing the guard. | `nwp.get_provider` |
| 9 | HRRR asks for 100 m fields; the `sfc` product does not publish them | Wind taken at **80 m AGL** (HRRR's highest AGL wind level, the hub-height proxy) and temperature at **2 m AGL**. Both labelled honestly in the UI rather than presented as 100 m. | `config.py`, `hrrr.py` |
| 10 | Weather valid-time was coupled to the telemetry clock, so a dataset outside the HRRR archive made overlays unavailable | `NWP_VALID_TIME` overrides the weather valid-time **independently** of `SIM_NOW`, so dashboards stay on the dataset's own "now" while overlays target a cycle that exists. | `clock.get_nwp_time` |
| 11 | "The map fills the viewport" is not expressible in `st_folium` | `height` accepts a fixed pixel int only (no `vh`/percentage support), so `MAP_HEIGHT_PX = 900` approximates it using the same conservative-estimate approach as the panel constants, rather than JS measurement. | `config.py` |
| 12 | `IMPLEMENTATION_PLAN.md` Phase 14 names `folium.plugins.HeatMap` | Rendered as a second `ImageOverlay` instead. `HeatMap`'s constructor is unannotated in `folium~=0.18`, and mypy's per-module `strict` override is not truly module-scoped — it would flag that call project-wide. Still a smoothed coloured intensity surface, with no `# type: ignore` and no weakening of the required config. | `map_view.py` |
| 13 | `PROJECT_SPEC.md` §10.1 rules out JS viewport measurement, but the layout needs a mobile switch | Desktop/mobile placement is decided entirely in CSS. `is_mobile` stays `False` with no live detection; the accessors exist so a later phase can wire it without changing the module contract. | `state.py` |
| 14 | `CLAUDE.md` §4.1 lists the data layer's allowed imports as stdlib/duckdb/pandas/config only | `queries.py` imports `src/domain/models.py` as **shared vocabulary** — that module holds only frozen dataclasses and enums, with no logic, no I/O, and no dependency back on `src/data/`. Keeps the substantive intent of the layering rule while satisfying the Phase 4 data contracts as written. | `queries.py` |
