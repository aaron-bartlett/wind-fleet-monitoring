# Wind Fleet Monitor — Architecture Interview Notes

Answers grounded in this repository as built. Each answer is followed by a **Note** covering
where a different answer is equally defensible, and what a follow-up question would probe.

Reference numbers used throughout: `telemetry.csv` is **2.2 MB / 28,263 rows** (10 farms,
50 turbines, 2 days at 5-minute intervals). The suite is **279 tests across 15 files**. The
performance suite builds a synthetic **432,000-row** fleet.

---

## Architecture decisions

### Why did you choose your frontend framework?

Streamlit, and the honest answer is that the brief named it. But it is also the right call for
this shape of problem: a single-operator internal dashboard, read-only, where the entire value
is in the data model and the aggregation, not in bespoke interaction design. Streamlit removes
the whole client/server boundary — no API layer, no serialization contract, no separate build
toolchain — so effectively all of the effort lands on health rules, SQL, and layering rather
than on plumbing.

The cost is real and worth naming: Streamlit re-executes the entire script top-to-bottom on
every interaction. That is why `st.session_state` is walled behind `src/ui/state.py` and why
every `st.rerun()` is guarded by a value-changed check — an unguarded rerun on a map click is
an infinite loop. It is also why `st_folium` is called with `returned_objects` restricted to
just the two click keys: the default payload includes `bounds`/`zoom`/`center`, which change on
every pan and would trigger a rerun on every mouse drag.

> **Note.** If interaction design or multi-user concurrency mattered, React + FastAPI is the
> defensible alternative, and I would say so. The strongest version of this answer is not
> "Streamlit is good" but "Streamlit is correct *at this scale and audience*, and here is the
> specific point at which I would abandon it" — roughly: more than a handful of concurrent
> operators, or any write path. Dash is the other reasonable pick and keeps Plotly native.

### Why did you choose your backend framework?

There isn't one, and that is deliberate. Streamlit *is* the server process. Adding
FastAPI/Flask behind it would mean serializing DataFrames over HTTP to a process running on
the same box, for no isolation benefit at this scale.

What replaces a backend framework is the **layering discipline**: `app.py → src/ui/ →
src/domain/ → src/data/`, dependencies pointing one direction only. `src/domain/` and
`src/data/` are pure Python — no Streamlit, no Folium, no Plotly. That constraint is enforced
mechanically by `tests/test_architecture.py`, which AST-scans both trees for forbidden imports
and for raw SQL outside the two modules licensed to hold it. So the "backend" is a set of
importable, independently testable modules; the fact that a Streamlit process currently calls
them is an implementation detail, and swapping in FastAPI would not touch `src/domain/` at all.

> **Note.** A reasonable challenge: "isn't that over-engineered for a take-home?" The defence
> is that the layering is what makes 279 tests possible without a Streamlit runtime, and the
> architecture test is 60 lines. If pressed, I would concede that the `src/domain/` ↔
> `src/data/` split is the least load-bearing part of it — `queries.py` already imports domain
> dataclasses as shared vocabulary, documented as an explicit `SPEC-GAP`.

### Why did you choose your database?

DuckDB. Three reasons, in priority order:

1. **It matches the access pattern.** Every query in this app is an analytical roll-up —
   `SUM` over a time window, `time_bucket` aggregation, a window function to find each
   turbine's latest row. DuckDB is columnar and vectorized; that is exactly its workload.
   Postgres would do this correctly but is row-oriented and would need tuning to match.
2. **It has zero operational surface.** Embedded, single file, `pip install duckdb`. A
   reviewer clones the repo and runs it. No container, no connection string, no migrations.
3. **It reads CSV natively.** `read_csv_auto` means ingest is a `CREATE TABLE AS SELECT`
   rather than a hand-rolled parser, and the whole ingest runs inside one transaction with
   rollback on failure.

The alternative I actively rejected was "just use pandas." Pandas would have worked at 28k
rows and fallen over conceptually at the first scale question — and it would have put
aggregation logic in Python where it is slower and harder to reason about than SQL.

> **Note.** The honest weakness: DuckDB is single-writer and process-embedded, so it does not
> survive a multi-instance deployment with a shared write path. That does not bite here because
> the app is read-only and the database is *derived* — rebuilt from CSV on boot, which is why
> the Cloud Run container can point `DUCKDB_PATH` at ephemeral `/tmp`. If asked "what about
> Postgres/TimescaleDB?", the answer is: correct choice the moment ingest becomes continuous
> rather than batch.

### Why did you structure the code this way?

The structure exists to make one property true: **business logic is testable without a UI
runtime.** Everything else follows.

- `config.py` — every constant, threshold, colour, and cap. No magic numbers in `src/`. This
  means the health rules are *readable as data* and tunable without touching logic.
- `src/data/` — `db.py` owns DDL and ingest; `queries.py` owns every read query, one named
  function each. No SQL string exists anywhere else in the repo.
- `src/domain/` — pure functions and frozen dataclasses. `health.py` (classification),
  `aggregates.py` (roll-ups), `clock.py` ("now" resolution), `geo.py`, `nwp.py`.
- `src/ui/` — Streamlit, Folium, Plotly. `state.py` is the only module permitted to touch
  `st.session_state`. `charts.py` is the only module that constructs a Plotly trace.
- `app.py` — wiring and page config only.

Two specific conventions carry disproportionate weight. First, **units in variable names**:
`power_kw`, `wind_speed_ms`, `gearbox_temp_c`, `energy_mwh`. Unit confusion is the most common
bug class in this domain, and the spec explicitly required not conflating power with energy
(the "Total Power Output" tile renders as **Total Energy (MWh)**). Second, **tz-aware
datetimes everywhere** — a naive datetime anywhere in this codebase is treated as a bug.

> **Note.** Expect "why not a flat structure for a project this size?" Fair. The counter is
> that the layering was cheap up front and paid for itself when the NWP provider was swapped
> twice (stub → live HRRR → telemetry-backed) with zero changes to the data layer. If the
> interviewer prefers vertical/feature slicing over horizontal layering, that is a legitimate
> alternative and I would not defend horizontal layering as universally superior.

### What would you change with another week?

Ranked by value, not by effort:

1. **Write the README.** It is genuinely missing. The repo has a documented `SPEC-GAP`
   convention where every ambiguity resolution points at "README §16" — and that table does
   not exist yet. That is the single largest gap and it is a documentation gap, not a code gap.
2. **Finish the deployment.** The container builds and passes a health check locally; the
   Cloud Run service exists but this revision has not been pushed to it.
3. **Make health thresholds configurable per turbine model.** They are currently global
   constants. Real fleets mix turbine models with different rated power and cut-out speeds, so
   a global `RATED_POWER_KW = 3500` is a modelling simplification, not a truth.
4. **Add an incremental ingest path.** Ingest is currently full-rebuild, keyed on source-file
   mtime/size. That is correct for batch CSV and wrong for a live feed.
5. **Property-based tests on `health.py`** via Hypothesis, to complement the table-driven
   boundary tests.

> **Note.** Interviewers usually probe whether you know what is weakest in your own work.
> Leading with "the README is missing" is stronger than leading with a feature, because it
> shows accurate self-assessment. Avoid listing features the spec explicitly deferred
> (forecasting) as though they were oversights — they were scoped out on purpose.

---

## Data

### How large was the CSV?

`telemetry.csv` is **2.2 MB, 28,263 data rows** — 50 turbines × 2 days at 5-minute intervals,
which would be 28,800 rows if complete; the shortfall is deliberate gaps and duplicates in the
fixture. `turbines.csv` is 50 rows, `farms.csv` is 10.

That is small enough to fit in memory many times over, which is precisely why I did **not**
let it dictate the design. The performance suite therefore builds a synthetic
**432,000-row** fleet (10 farms × 50 turbines × 30 days) in-memory via set-based SQL, and
asserts that the real production query plans still hold at that size.

> **Note.** The trap in this question is answering only with a number. The interviewer is
> checking whether you designed for the data you were given or for the data you will get. Name
> the size, then immediately state what you did to stop that size from being load-bearing.

### What happens with a 10 GB CSV?

Distinguish three things, because they fail at different points.

**Ingest** mostly survives. `read_csv_auto` streams and DuckDB spills to disk, so with
`DUCKDB_PATH` on real disk (not `:memory:`, not `/tmp` on Cloud Run) a 10 GB load is slow but
completes. The current full-rebuild-on-mtime-change strategy becomes untenable, though — you
cannot re-ingest 10 GB on every deploy. That is the first thing I would change: partitioned
Parquet, ingest by date partition, append-only.

**Queries** largely hold, and this is the payoff for the "aggregate in SQL, never
`SELECT *` into pandas" rule. Every roll-up is already `SUM`/`time_bucket`/window functions
with `(turbine_id, timestamp)` and `(farm_id, timestamp)` indexes, and everything returned to
the browser is capped — 2,000 points for time series, 5,000 for scatter, down-sampled in SQL.
A fleet-wide 6-hour-bucket query over 10 GB returns the same 2,000 rows it returns today.

**The container** breaks first, and for an unglamorous reason: the current Cloud Run config
writes the DuckDB file to `/tmp`, which is a memory-backed tmpfs. A 10 GB source would exhaust
the instance long before any query ran. Fix is a mounted volume or a pre-built database image.

> **Note.** Strong answers name a *specific first failure* rather than saying "it would be
> slow." Whether you name tmpfs, ingest time, or the browser is less important than that the
> failure is concrete and the reasoning is causal. A common good alternative: "the `latest_
> telemetry` view is a full-table window function — that becomes the hot spot, and I would
> materialize it."

### How did you validate the input?

Fail fast, fail loud, and fail with a message that names the file and the problem. Four layers:

1. **File presence** — missing `farms.csv`/`turbines.csv`/`telemetry.csv` raises
   `DataLoadError` naming the path, before any parsing.
2. **Required columns** — after staging, `DESCRIBE` the staging table and diff against the
   required column set. The error names the file *and* the missing columns.
3. **Timestamp format** — this is the subtle one. `timestamp` and `received_at` are forced to
   `VARCHAR` at read time specifically to **defeat DuckDB's type sniffer**, then cast with an
   explicit `strptime(..., '%Y-%m-%dT%H:%M:%SZ')`. If sniffing were left on, a malformed
   timestamp could be silently coerced to `NULL` and the app would quietly show a gap instead
   of reporting bad data. The explicit cast raises at execution time on the first bad row.
4. **Transactional ingest** — the whole thing runs in one transaction with `ROLLBACK` in a
   `finally`, so a failure never leaves a half-built schema behind.

`app.py` catches `WindFleetError` at the top level and renders `st.error(...)` then
`st.stop()`. A user never sees a raw traceback.

> **Note.** The point worth making loudest is #3 — deliberately disabling type inference so
> that bad data is an error rather than a silent `NULL`. That is the kind of decision that
> distinguishes "I validated the input" from "I thought about how validation fails."
>
> A defensible alternative philosophy: quarantine bad rows to a rejects table and continue,
> rather than aborting. Correct for a production pipeline ingesting continuously; wrong here,
> where a malformed file means the operator is looking at the wrong data.

### How did you handle missing values?

Deliberately differently at each layer, because "missing" means different things.

- **Ingest** preserves `NULL`. It does not impute, interpolate, or drop. The ingest summary
  reports a `rows_with_nulls` count so the condition is visible rather than silent.
- **Aggregation** excludes `NULL` (`SUM` ignores it; the wind-speed series filters
  `wind_speed_ms IS NOT NULL`), so a missing reading never contributes a zero.
- **Health classification** treats a `NULL` metric on an otherwise-present record as
  `ERROR` — not `Healthy`. A turbine reporting nothing is not a turbine reporting good news.
- **Charts** render missing intervals as a **genuine visual gap**. The time series query
  `LEFT JOIN`s onto a generated time spine so empty buckets come back as `NaN` rows, and the
  Plotly trace sets `connectgaps=False`. A missing hour looks like a missing hour, not a
  straight line between two points.
- **Rendering** degrades per case: a farm with no turbines, a turbine with no telemetry, or an
  empty rose window each render an explanatory message instead of raising.

That chain — preserve, exclude, escalate, show the gap — is the whole answer.

> **Note.** `connectgaps=False` plus the time-spine `LEFT JOIN` is the detail to lead with. It
> is easy to say "I handled missing data"; it is harder to have done the work that makes a gap
> *visible*, and it directly connects to the misleading-visualization question later.

### How did you handle duplicate timestamps?

Last-write-wins on ingest arrival, resolved in SQL:

```sql
ROW_NUMBER() OVER (PARTITION BY turbine_id, timestamp ORDER BY received_at DESC) = 1
```

The key modelling decision is that `(turbine_id, timestamp)` is the natural key and
`received_at` is the tiebreaker. Two rows with the same measurement time are a *re-transmission*
— a corrected or re-sent reading — so the one that arrived later supersedes. Picking by
`received_at` rather than by file order makes the result deterministic regardless of row
ordering in the CSV.

Duplicates removed are counted and surfaced in the ingest summary rather than dropped
silently, and the fixture contains a deliberate duplicate pair (same timestamp, different
`received_at` and different power) so the test asserts the *correct* row survived — not merely
that the row count fell.

> **Note.** Alternatives worth acknowledging if pushed: (a) keep both and average — wrong,
> because it invents a reading that no sensor produced; (b) treat a duplicate as a data-quality
> *error* and reject the file — too strict for telemetry, where re-transmission is normal;
> (c) keep the *first* arrival — defensible if you regard the original as authoritative and
> re-sends as suspect. The choice matters less than being able to justify it and having a test
> that pins it down.

---

## Performance

### What is the bottleneck?

At the current 28k rows, the bottleneck is **not** the database — queries are sub-millisecond.
It is Streamlit's rerun model plus map serialization.

Every interaction re-executes the script. Without intervention that means re-running every
roll-up and re-serializing the entire Folium map to HTML on each click. Three mitigations are
in place: the DuckDB connection is `@st.cache_resource` (ingest runs at most once per process,
not once per rerun); roll-ups are `@st.cache_data` with a 300 s TTL keyed on the resolved
"now"; and `st_folium` is restricted to the two click-related return keys so pans and zooms do
not trigger reruns at all.

What remains, and would show up first under a profiler, is Folium map generation — building
and rendering markers to HTML on each genuine rerun.

At **432k rows** (the perf suite) the picture shifts toward the `latest_telemetry` view, which
is a full-table window function partitioned by turbine.

> **Note.** The answer must be scale-qualified. "The bottleneck is X" without saying "at what
> size" is a weak answer. If the interviewer pushes for a single answer: today it is the
> render path, and the database is deliberately over-provisioned for the current data.

### What would break first at 100× scale?

100× the current dataset is ~2.8 M rows — which the perf suite already partially covers at
432k, and which DuckDB handles comfortably. So the honest answer is that **data volume is not
what breaks first**; concurrency and deployment topology are.

In order:

1. **`latest_telemetry`** — a window function over the full table, evaluated on every health
   roll-up. First real query hot spot. Fix: materialize it during ingest, refresh on write.
2. **Ingest wall-time on cold start.** Full rebuild on boot, in-process, before the first page
   renders. At 2.8 M rows that is a visibly slow first request on a scale-to-zero Cloud Run
   instance. Fix: build the database at image-build time, ship it in the container.
3. **Concurrent users.** This is the real ceiling and it has nothing to do with row count.
   Streamlit holds a server-side session per connection and re-executes per interaction; the
   `@st.cache_resource` DuckDB connection is shared process-wide. Tens of simultaneous
   operators, not millions of rows, is what forces an architecture change.
4. **The map.** Folium renders markers client-side as DOM elements. A few thousand turbines
   needs clustering or a canvas/`deck.gl` renderer regardless of backend performance.

> **Note.** The best version of this answer resists the premise slightly — the question invites
> you to talk about data volume, and the interesting answer is that data volume is the part
> that was actually designed for. Be ready to back that with the specifics: SQL-side
> aggregation, both indexes, the 2,000/5,000-point caps, the 432k-row regression suite.

### How would you profile the application?

Layer by layer, because each needs a different tool:

- **SQL** — `EXPLAIN ANALYZE` in DuckDB for per-operator timings. This is where I would start,
  because it is the layer with the clearest signal.
- **Python** — `cProfile`/`snakeviz` on a headless driver script that calls the aggregate
  builders directly. That is possible *only* because `src/domain/` and `src/data/` import
  cleanly without Streamlit — the layering pays off here concretely.
- **Streamlit runtime** — `--server.enableStaticServing` timings plus browser DevTools to
  separate server-side rerun time from websocket payload size and client render time. The
  usual surprise is payload size, not compute.
- **Regression prevention** — `tests/test_performance.py` already asserts wall-clock bounds on
  the aggregate builders against a 432k-row synthetic fleet, plus point-cap invariants. That
  turns "it got slow" into a failing test rather than a bug report.

> **Note.** Mentioning the existing perf test is worth more than listing tools, because it
> shows profiling was treated as a standing property rather than a one-time investigation. A
> good follow-up to be ready for: "wall-clock assertions are flaky in CI" — true, and the
> mitigation is generous bounds that catch order-of-magnitude regressions only, which is what
> the current thresholds do.

### How would you improve dashboard load time?

The dominant cost on a cold start is ingest, so:

1. **Pre-build the database at image-build time.** Move ingest into a Docker build step and
   ship `fleet.duckdb` in the image. Turns a multi-second first request into a file open. This
   is the single highest-leverage change.
2. **Keep an instance warm.** `--min-instances 1` on Cloud Run, already recommended in the
   deploy config — scale-to-zero cold starts dominate everything else for an internal tool.
3. **Materialize `latest_telemetry`** as a table refreshed at ingest rather than a view
   recomputed per query.
4. **Defer below-the-fold work.** The scatter plot and its regression only matter once an
   operator opens a turbine; render it behind the existing selection rather than eagerly.
5. **Lower the point caps if measurement justifies it.** 2,000 points is already conservative,
   but the map, not the charts, is the payload to watch.

The measurement discipline matters more than the list: do (1), re-profile, and only then
decide whether (3)–(5) are worth the complexity.

> **Note.** Resist reciting an optimization checklist. The strongest structure is: name the
> dominant cost, propose the one change that removes it, and say explicitly that you would
> re-measure before doing more. Interviewers are listening for whether you optimize by
> evidence or by habit.

---

## Engineering

### How did you test the application?

**279 tests across 15 files**, structured to mirror the source tree, with three deliberate
constraints: no test requires a Streamlit runtime, no test requires network access, and no
test reads the real `data/` CSVs — fixtures only, since the real dataset is expected to grow
and would silently invalidate assertions.

The shape of the suite:

- **Table-driven boundary tests on `health.py`** — every threshold tested *at*, *just below*,
  and *just above* its boundary. This is the most heavily tested module in the project because
  it is the one whose output the operator acts on.
- **An architecture test.** `tests/test_architecture.py` AST-scans `src/domain/` and
  `src/data/` for forbidden imports (Streamlit, Folium, Plotly, branca), for raw SQL outside
  the two modules licensed to hold it, and for `st.session_state` outside `state.py`. The
  layering is not a convention anyone has to remember — it fails CI.
- **Ingest tests** covering the failure modes specifically: missing file, missing column,
  malformed timestamp, and duplicate resolution (asserting the *correct* row survives, not
  just that the count dropped).
- **Performance tests** against the 432k-row synthetic fleet.
- **Smoke tests** that build a Folium map at all three drill-down levels without a running
  server.

The whole thing runs behind one gate: `ruff format --check . && ruff check . && mypy src
app.py && pytest`.

> **Note.** The architecture test is the piece most worth describing in detail — it is
> unusual, it is cheap, and it demonstrates thinking about how a design *decays* rather than
> just how it starts. Also worth stating the discipline explicitly: assert on values, never on
> "it didn't raise."

### What tests would you add?

1. **End-to-end UI tests.** The biggest genuine gap. Nothing currently exercises
   `dashboards.*.render()` — those functions call `st.*` and need a runtime. Streamlit's
   `AppTest` harness would cover the click → state → rerun → render path, which is exactly
   where Streamlit bugs actually live.
2. **Property-based tests on `health.py`** (Hypothesis): for any record in physical range, the
   classifier returns exactly one status, and `ERROR` always implies empty breach tuples.
   Currently guaranteed by construction and by table tests, not by a general property.
3. **A golden-file test on ingest** — hash the post-ingest schema and row counts to catch
   accidental changes to dedup or casting.
4. **Timezone edge cases.** DST transitions in the local-time display path; the codebase is
   UTC-internal, which is correct, but the display boundary is untested at the transition.
5. **A container smoke test in CI** — build the image, boot it, assert `/_stcore/health`
   returns 200. This was done manually; it should be automatic.

> **Note.** Leading with the known gap (UI rendering) rather than a nice-to-have is the
> stronger move. If asked "why didn't you write those?", the honest answer is scope: the
> layering was designed so the UI layer is thin enough that its failure modes are mostly
> visual, and the testing budget went to the logic an operator's decisions depend on.

### How would you monitor it in production?

Split by the question each signal answers.

**Is it up?** Cloud Run's health check against `/_stcore/health`, plus an uptime probe. A
Streamlit app can serve HTTP while its websocket path is broken — a sticky-session
misconfiguration produces exactly that — so a synthetic check that completes a websocket
handshake is worth more than an HTTP 200.

**Is it correct?** This is the one people skip, and for a data app it matters most. The ingest
summary already computes the right numbers — row counts, duplicates removed, rows with nulls,
telemetry time range. Emit those as structured metrics per ingest and alert on
discontinuities: a sudden jump in null rate, a duplicate rate outside its normal band, or a
`MAX(timestamp)` that stops advancing. Stale data that renders perfectly is the dangerous
failure, because it looks healthy.

**Is it fast?** Cloud Run request latency and instance count; server-side rerun duration as a
custom metric.

**What broke?** The codebase already uses module-level `logging` with no `print()` outside
`app.py`, so switching to JSON formatting gives structured logs in Cloud Logging immediately.
Error tracking (Sentry) on the `WindFleetError` boundary in `app.py`.

> **Note.** The data-freshness point is the differentiator. Most candidates answer this
> question with uptime and latency; for a monitoring dashboard, *"the dashboard is confidently
> displaying yesterday's data"* is the failure that actually costs someone money, and it is
> invisible to infrastructure monitoring. Tie it back to the design: this is the same reason
> `clock.get_now()` resolves from the dataset rather than the wall clock.

### How would you deploy it?

Currently: a `Dockerfile` producing a self-contained image, deployed to **Google Cloud Run**
via `gcloud run deploy --source .`, which builds with Cloud Build and rolls out a new revision.

The non-obvious constraints, all of which are encoded in the deploy config:

- **`--session-affinity` is mandatory.** Streamlit runs over a websocket; without sticky
  routing the page hangs at "Please wait…" forever. This is the single most common way a
  Streamlit deployment fails.
- **`--platform linux/amd64` is pinned in the Dockerfile.** Building on Apple Silicon defaults
  to arm64, and `timezonefinder` ships x86_64 wheels only — the build fails trying to compile
  a C extension in a compiler-less slim image. Pinning also guarantees local and Cloud Run
  builds are identical.
- **`/tmp` for anything writable.** The container filesystem is read-only apart from `/tmp`,
  so `DUCKDB_PATH` points there. Safe because the database is derived and rebuilt from the
  committed CSVs on boot.
- **`--timeout 3600`** to bound websocket lifetime, and enough memory that ingest does not OOM.

What is missing: CI/CD. Deployment is currently a manual command. The next step is a GitHub
Actions workflow running the full gate on PR and deploying on merge to `main`.

> **Note.** Naming session affinity and the arm64/amd64 wheel problem is worth a lot here —
> both are things you only know from having actually deployed it, and the second one was a real
> build failure in this project, not a hypothetical. Be candid that CI/CD is absent rather than
> describing an aspirational pipeline as though it exists.

---

## Product

### How did you decide what "health" means?

The spec supplied the thresholds; the design work was in the **structure** of the
classification, and there were three real decisions.

**First: `ERROR` is a separate axis, and it short-circuits.** A missing record, a stale
record, or a structurally invalid one is classified *before* any threshold rule runs — so its
breach lists are always empty. "I cannot tell you how this turbine is doing" is a fundamentally
different statement from "this turbine is fine," and collapsing them would let a dead sensor
render as green. That is the most consequential decision in the module.

**Second: severity aggregates from counts, not from any single worst metric.** The spec
defined minor and major breaches but left the boundary between Warning and Critical ambiguous
for multiple minor breaches. Resolved as: **Warning = 1–2 minor; Critical = ≥3 minor or ≥1
major**, marked in the code as a `SPEC-GAP`. The reasoning is that three simultaneous minor
anomalies usually indicate a systemic problem rather than three coincidences.

**Third: some rules are conditional, because context changes meaning.** Zero power output is
normal below cut-in wind speed and a major fault at 10 m/s. So the power rules are evaluated
against a reference power curve within a wind-speed window, and blade pitch is only checked
above 100 kW. A threshold that ignores operating context generates alerts operators learn to
ignore.

> **Note.** The framing that lands is *alert fatigue*: an alert an operator dismisses by reflex
> is worse than no alert. Every conditional rule exists to avoid one. Be explicit about which
> parts came from the spec and which were your calls — inventing credit for the thresholds
> themselves would be easy to catch.

### How would an engineer use this dashboard?

The interaction model is a funnel, and each level answers exactly one question:

- **Fleet** — *"Does anything need me right now?"* One map, one dot per farm, coloured by
  worst-case health. Answered in about two seconds without reading a number.
- **Farm** — *"Which turbine, and is this local or site-wide?"* Turbine markers, status
  counts, farm power. The distinction matters: three turbines degrading at one site suggests
  weather or a grid curtailment; one turbine degrading suggests a machine.
- **Turbine** — *"What is wrong and what do I do?"* This is the diagnostic view, and it is
  deliberately the only place that does not summarize. Every breach is listed verbatim with its
  severity — never collapsed to a single word — alongside the raw telemetry readout with a
  per-metric status dot, ingest lag, and a historical scatter with a regression fit.

The scatter is the diagnostic workhorse: plotting power against wind speed shows immediately
whether a turbine is underperforming *for the conditions it is in*, which no single-metric
time series can show.

> **Note.** The strongest version of this answer names the *decision* at each level rather than
> the widgets. A good follow-up to anticipate: "what would an operator do next?" — realistically
> they would raise a work order, which means the natural next feature is an escalation path
> (acknowledge, snooze, assign) rather than another chart. Being able to name the next feature
> from the user's workflow rather than from the data model is the point.

### What is the most important visualization?

The **fleet map with health-coloured farm markers** — because it is the only one that answers
the question the operator actually arrived with, and it does so pre-attentively. Colour and
position are processed faster than any label. If the dashboard could show one thing, it is
"where should I look," not "what is the value."

The **turbine power-vs-wind-speed scatter** is a close and interesting second, and I would
argue it is the most *analytically* valuable: it is the only view that separates "this turbine
is producing less" from "this turbine is producing less than it should be given the wind." That
distinction is the entire difference between noise and a fault.

The distinction I would draw: the map is the most important for *triage*, the scatter for
*diagnosis*. Those are different jobs and the dashboard needs both.

> **Note.** Either answer is defensible, and the interviewer is mostly testing whether you can
> justify a ranking rather than which item you rank first. The weak answer is "they're all
> important." Picking the map and grounding it in *time-to-first-decision* is stronger than
> picking whichever chart was hardest to build.

### How would you prevent misleading visualizations?

Five concrete practices, four of which are already in the code:

1. **Never interpolate across missing data.** The time series `LEFT JOIN`s a generated time
   spine so empty buckets return as `NaN`, and Plotly is set to `connectgaps=False`. A sensor
   outage renders as a gap, not as a confident straight line between two real points. This is
   the single most common way a dashboard lies.
2. **Never silently truncate.** Down-sampling is capped in SQL, and when it happens the chart
   annotates *"Showing N of M points."* If a query would exceed its point cap it raises rather
   than quietly returning a partial series — the caller must choose a coarser bucket.
3. **Label units, and never conflate related ones.** Power and energy are different
   quantities; the spec's "Total Power Output" is rendered as **Total Energy (MWh)** because
   that is what the number actually is. Every variable in the codebase carries its unit in its
   name so the mistake is hard to make.
4. **Mark synthetic data as synthetic.** The wind rose currently draws petal *length* from real
   telemetry wind speed but petal *angle* from a theoretical bearing, because telemetry carries
   no direction channel. The caption says exactly that, in those terms. A plausible-looking
   chart built partly from invented data is the most dangerous artifact in the app.
5. **Refuse to fit what cannot be fit.** The scatter regression requires a minimum point count;
   below it, the chart renders "Insufficient data" rather than an R² computed from three
   points.

The unifying principle: **the chart should be honest about its own uncertainty.** Absence of
data must look different from a value of zero, and estimated data must look different from
measured data.

> **Note.** Point 4 is the one to lead with if you only get to make one, because it is a live
> tension in this build rather than a general principle — there was a real choice between
> "draw a nice-looking rose" and "draw a rose that tells you which half of it is invented."
> Naming that trade-off honestly demonstrates more judgment than reciting Tufte.
