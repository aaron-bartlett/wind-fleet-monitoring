# EXECUTION_RUNBOOK.md — Driving the build with the Claude CLI

How to execute `IMPLEMENTATION_PLAN.md` phase by phase, with every command and output saved to disk.

**The core rule:** one phase per Claude session. Never say "do phases 3 through 6." Context degrades,
scope creeps, and a failure in phase 5 silently corrupts phase 3's work. One phase, one gate, one commit.

---

## Part 0 — One-time setup

### 0.1 Create the repository

```bash
cd ~/Documents/NextEra-Technical/Software_Take_Home_Exercise

mkdir -p wind-fleet-monitor/{data,scripts,logs,.claude}
cd wind-fleet-monitor

# The three planning documents Claude will work from
cp ../CLAUDE.md ../PROJECT_SPEC.md ../IMPLEMENTATION_PLAN.md .

# The seed data — Claude is forbidden from modifying these (CLAUDE.md §5.8)
cp ../farms.csv ../turbines.csv ../telemetry.csv data/

git init
git add -A
git commit -m "Planning docs and seed data"
```

### 0.2 Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -V          # must print 3.11 or higher
```

Do **not** `pip install` anything yet — Phase 0 writes `requirements-dev.txt` and installs from it.

### 0.3 Install the runner and settings

Copy `scripts/run-phase.sh` and `.claude/settings.json` (provided alongside this runbook) into place:

```bash
chmod +x scripts/run-phase.sh
command -v jq >/dev/null || brew install jq     # transcript rendering needs jq
```

`.claude/settings.json` does two things: pre-approves the tools this build needs (so headless runs
don't stall on prompts) and appends every Bash call and result to `logs/tool-calls.jsonl` /
`logs/tool-results.jsonl` via hooks. Verify the hook fires before you trust it:

```bash
claude -p "Run the command: echo hook-test" --verbose
cat logs/tool-calls.jsonl        # should contain one JSON line
```

If it's empty, the hook schema has drifted — fall back to the `script(1)` wrapper in Part 3.4 and
carry on. The `stream-json` logs from `run-phase.sh` are the primary record either way.

### 0.4 Confirm the CLI sees your CLAUDE.md

```bash
claude
> /context
```

You should see `CLAUDE.md` listed among loaded files. If not, you're in the wrong directory.
Type `/exit`.

---

## Part 1 — Two execution patterns

### Pattern A — Headless (mechanical phases)

For phases where `IMPLEMENTATION_PLAN.md` already specifies the types and steps precisely enough that
there's no real design decision left. One command, fully logged, self-gating:

```bash
scripts/run-phase.sh <N>
```

The runner writes the phase prompt, streams the whole session to `logs/phase-N-<stamp>.jsonl`,
renders a readable transcript to `.md`, then **independently re-runs the full gate itself** — it does
not take Claude's word for it — and commits only if the gate is clean.

### Pattern B — Interactive with plan mode (design-bearing phases)

Plan mode is **interactive only**; `--permission-mode plan` is not supported with `-p`. So the
design-bearing phases run in a real terminal session, with output captured by `script(1)`.

```bash
script -q logs/phase-<N>-plan-$(date +%Y%m%d-%H%M%S).txt claude
```

Inside the session:

1. Press **Shift+Tab** until the status line shows **plan mode**.
2. Paste the phase prompt (Part 2 gives the exact text per phase).
3. Read the plan. This is the step that earns its keep — check it against the plan document:
   - Does it touch only this phase's **Target Files**?
   - Does it match the **Data Contracts** signatures exactly?
   - Is it adding a dependency? Inventing a directory? Putting a constant outside `config.py`?
4. Correct it in conversation until the plan is right, then approve it to execute.
5. When it reports done, run the gate yourself in a second terminal (Part 3.1) — never trust the
   self-report.
6. `/exit`, then commit:

```bash
git add -A && git commit -m "Phase <N>: <title>"
```

### Which phases get which pattern

| Phase | Title | Pattern | Why |
|---|---|---|---|
| 0 | Repo scaffold & toolchain | **A** headless | Pure file creation from an exact spec. |
| 1 | Config & error hierarchy | **A** headless | Transcription of a table. |
| 2 | Domain models | **A** headless | Dataclasses are fully specified. |
| 3 | DuckDB ingest | **B** plan mode | Dedup, type casting, and the fixture CSV design all have real choices. |
| 4 | Query layer | **B** plan mode | The gap-spine join and SQL-injection guards are the subtlest code in the project. |
| 5 | Clock & geo | **A** headless | Small, well-specified functions. |
| 6 | Health classification | **B** plan mode | The most important module. Rule precedence and boundary semantics deserve review before code. |
| 7 | NWP provider | **A** headless | Interface is fully specified; stub is mechanical. |
| 8 | Aggregates | **A** headless | Composition of existing pieces. |
| 9 | UI foundation | **B** plan mode | State machine + responsive CSS + app shell. Highest bug density in the stack. |
| 10 | Charts | **A** headless | Four independent figure builders. |
| 11 | Map: fleet layer | **B** plan mode | Bounds padding, click-id round-tripping, and rerun guarding. |
| 12 | Farm level | **A** headless, `--resume` from 11 | Extends phase 11's patterns. |
| 13 | Turbine dashboard | **B** plan mode | Largest dashboard; dropdown/state interaction. |
| 14 | Map layer controls | **A** headless | Mostly wiring an existing provider. |
| 15 | Responsive & performance | **B** plan mode | Needs a browser in the loop; judgment-heavy. |
| 16 | README & final verify | **A** headless | Writing task over completed work. |

---

## Part 2 — Phase-by-phase commands

Every phase ends with the same rule: **the gate must be clean before the next phase starts.**

### Phase 0 — Repo scaffold & toolchain

```bash
scripts/run-phase.sh 0
```

Then, because Phase 0 writes the dependency files:

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt 2>&1 | tee logs/phase-0-pip.txt
pytest tests/test_architecture.py -v 2>&1 | tee -a logs/phase-0-verify.txt
```

**Expect:** 2 passing architecture tests. `src/`, `tests/`, `data/` trees exist. `pyproject.toml`
carries the ruff/mypy/pytest blocks from `CLAUDE.md` §2.5.

---

### Phase 1 — Config & error hierarchy

```bash
scripts/run-phase.sh 1
```

**Check by hand** — this is the phase where a wrong number poisons everything downstream:

```bash
grep -n "MINOR_TO_CRITICAL\|gearbox_temp_c\|RATED_POWER_KW" config.py
```

`MINOR_TO_CRITICAL` must be `3`. The gearbox thresholds must be minor 95 / major 110.

---

### Phase 2 — Domain models

```bash
scripts/run-phase.sh 2
```

**Expect:** every dataclass `frozen=True, slots=True`; `Breach` tuples not lists; `compass_point`
tested at 0°, 90°, 337.5°.

---

### Phase 3 — DuckDB ingest  ⟨plan mode⟩

```bash
script -q logs/phase-3-plan-$(date +%Y%m%d-%H%M%S).txt claude
```

Shift+Tab to plan mode, then paste:

```
Read CLAUDE.md and PROJECT_SPEC.md in full, then plan Phase 3 of IMPLEMENTATION_PLAN.md
(DuckDB Ingest Layer).

Before writing the plan, inspect data/telemetry.csv, data/farms.csv and data/turbines.csv to
confirm the real column names, timestamp format, and row counts.

Your plan must cover:
- the exact content of each tests/fixtures/*.csv row, and which edge case each row exercises
- the dedup SQL and how duplicates_removed is counted
- how TIMESTAMPTZ casting fails loudly on a malformed timestamp
- how is_ingest_current() detects a changed source file

Implement ONLY Phase 3. Do not create src/data/queries.py.
```

Review the plan against the plan document's fixture requirements — every listed edge case
(10-minute gap, duplicate timestamp, NULL metric, gearbox 126.5, pitch 44, gearbox 250, 20-minute lag,
turbine with no telemetry, farm with no turbines) must appear. Approve, let it build, then:

```bash
# second terminal
pytest tests/test_ingest.py -v 2>&1 | tee logs/phase-3-verify.txt
ruff format --check . && ruff check . && mypy src && pytest -q 2>&1 | tee logs/phase-3-gate.txt
git add -A && git commit -m "Phase 3: DuckDB ingest layer"
```

---

### Phase 4 — Query layer  ⟨plan mode⟩

```bash
script -q logs/phase-4-plan-$(date +%Y%m%d-%H%M%S).txt claude
```

```
Read CLAUDE.md and PROJECT_SPEC.md, then plan Phase 4 of IMPLEMENTATION_PLAN.md (Query Layer).

Pay particular attention in your plan to:
- the generate_series time-spine LEFT JOIN that makes missing intervals appear as NULL rows
- exactly how bucket / x_metric / y_metric are validated against config allowlists before any
  string interpolation, and why parameter binding cannot be used for those positions
- the deterministic modulo-stride down-sampling in get_scatter_data (never ORDER BY random())
- which functions raise QueryError vs return None

Implement ONLY Phase 4.
```

After it builds:

```bash
pytest tests/test_queries.py -v 2>&1 | tee logs/phase-4-verify.txt
```

**The test that matters most:** `get_scatter_data(x_metric="; DROP TABLE telemetry")` must raise
`QueryError`. Confirm it's present and passing.

```bash
ruff format --check . && ruff check . && mypy src && pytest -q 2>&1 | tee logs/phase-4-gate.txt
git add -A && git commit -m "Phase 4: Query layer"
```

---

### Phase 5 — Clock & geo

```bash
scripts/run-phase.sh 5
```

**Expect:** `TimezoneFinder()` constructed once at module level, not per call. `is_stale` raises on
naive datetimes.

---

### Phase 6 — Health classification  ⟨plan mode⟩

The single most important phase. Give it the most review attention.

```bash
script -q logs/phase-6-plan-$(date +%Y%m%d-%H%M%S).txt claude
```

```
Read CLAUDE.md and PROJECT_SPEC.md sections 6.2 and 16, then plan Phase 6 of
IMPLEMENTATION_PLAN.md (Health Classification).

In your plan, write out explicitly:
1. The precedence order: ERROR checks short-circuit before any breach evaluation.
2. For each of the five metrics, the exact comparison operators and the conditional gates
   (wind-speed windows, the 100 kW pitch gate, the power-curve fraction).
3. Your rule that a metric contributes at most one breach, with major superseding minor.
4. The full table of test cases you will write, showing for every threshold a case at the
   boundary, just below, and just above — and the expected HealthStatus for each.

Do not write code until I approve the plan. Implement ONLY Phase 6.
```

Scrutinise the operator table. The two errors most likely to slip through:

- Using `>=` where the spec says `>` — a gearbox at exactly 95.0 must be **healthy**.
- Treating `gearbox_temp_c = 250` as Critical instead of **Error** (physically impossible wins).

After it builds:

```bash
pytest tests/test_health.py -v --cov=src/domain/health --cov-report=term-missing \
  2>&1 | tee logs/phase-6-verify.txt
```

**Expect:** ≥95% coverage on `health.py`. If lower, an uncovered branch is an untested rule — ask for
the missing cases before moving on.

```bash
ruff format --check . && ruff check . && mypy src && pytest -q 2>&1 | tee logs/phase-6-gate.txt
git add -A && git commit -m "Phase 6: Health classification"
```

---

### Phase 7 — NWP provider

```bash
scripts/run-phase.sh 7
```

**Verify determinism yourself** — this is the whole point of the stub:

```bash
python -c "
from datetime import datetime, UTC
from src.domain.nwp import StubNWPProvider
p = StubNWPProvider()
t = datetime(2026,1,2,12, tzinfo=UTC)
a, b = p.point_forecast(41.25,-96.53,t), p.point_forecast(41.25,-96.53,t)
assert a == b, 'STUB IS NOT DETERMINISTIC'
print('deterministic OK:', a)
" 2>&1 | tee logs/phase-7-determinism.txt
```

Also confirm no module-level `import herbie`:

```bash
grep -n "import herbie" src/domain/nwp.py    # must only appear inside a method body, if at all
```

---

### Phase 8 — Aggregates

```bash
scripts/run-phase.sh 8
```

**Expect:** the query-count test proves `build_farm_map_rows` issues ≤3 queries regardless of farm
count. That test is the guard against an N+1 query pattern reappearing later.

---

### Phase 9 — UI foundation  ⟨plan mode⟩

```bash
script -q logs/phase-9-plan-$(date +%Y%m%d-%H%M%S).txt claude
```

```
Read CLAUDE.md sections 4 and 5.1, and PROJECT_SPEC.md sections 7 and 10.1. Then plan Phase 9 of
IMPLEMENTATION_PLAN.md (UI Foundation: State, Layout, App Shell).

Your plan must state:
- every AppState key and its default
- the exact state transitions for select_farm, select_turbine, and reset_view, naming which keys
  each one does NOT touch
- the CSS strategy: which media query drives the left-panel vs bottom-panel switch, and how the
  panel avoids covering the map controls and the Reset button
- how you will test state.py without a running Streamlit server

This is the first phase permitted to import streamlit, and only inside src/ui/ and app.py.
Implement ONLY Phase 9 — no map, no charts, no dashboards.
```

After it builds, verify the layering rule still holds and the app boots:

```bash
pytest tests/test_state.py tests/test_architecture.py -v 2>&1 | tee logs/phase-9-verify.txt

streamlit run app.py --server.headless true --server.port 8501 > logs/phase-9-streamlit.log 2>&1 &
sleep 12 && curl -sf http://localhost:8501/_stcore/health && echo " <- app is up"
kill %1
```

Open `http://localhost:8501` in a browser and confirm the header shows `Data as of: 2026-01-02 23:55 UTC`
(the dataset's own max timestamp, not today's date). If it shows today, `clock.get_now` is wrong.

---

### Phase 10 — Charts

```bash
scripts/run-phase.sh 10
```

**Expect:** `build_power_timeseries` sets `connectgaps=False`. Grep for it:

```bash
grep -n "connectgaps" src/ui/charts.py
```

---

### Phase 11 — Map: fleet layer  ⟨plan mode⟩

```bash
script -q logs/phase-11-plan-$(date +%Y%m%d-%H%M%S).txt claude
```

```
Read CLAUDE.md section 5.1 and PROJECT_SPEC.md sections 8.1, 8.2, 8.5 and 10.2. Then plan Phase 11
of IMPLEMENTATION_PLAN.md (Map: Fleet Layer & Fleet Dashboard).

Your plan must decide and justify:
- the single mechanism by which a farm_id is embedded in a marker and recovered from the
  st_folium return dict, and how extract_clicked_id handles a malformed or empty dict
- the exact returned_objects list passed to st_folium, and why restricting it prevents a rerun
  on every pan and zoom
- how fit_bounds padding keeps every marker out of the region the dashboard panel covers
- the guarded rerun pattern, written out as code

Implement ONLY Phase 11. No turbine layer, no farm dashboard.
```

This is the phase most likely to produce an infinite rerun loop. After it builds, watch the log while
you click:

```bash
streamlit run app.py --server.headless true --server.port 8501 2>&1 | tee logs/phase-11-streamlit.log
```

Click a farm dot. You should see **one** rerun, not a stream of them. Pan and zoom the map — those
should produce **zero** reruns. If they don't, `returned_objects` is wrong.

**Manual checks to record in the log file:**

- All 10 farms visible at load, no marker behind the left panel.
- Farms with 0 turbines show a gray dot with `0`.
- Reset View returns to fleet after drilling in.

---

### Phase 12 — Farm level

Resume phase 11's session so the map conventions carry over:

```bash
scripts/run-phase.sh 12 --resume
```

**Manual check:** clicking `FARM03` (no turbines) shows the empty-state message, not a traceback.

---

### Phase 13 — Turbine dashboard  ⟨plan mode⟩

```bash
script -q logs/phase-13-plan-$(date +%Y%m%d-%H%M%S).txt claude
```

```
Read PROJECT_SPEC.md section 10.4 and CLAUDE.md section 5.1. Then plan Phase 13 of
IMPLEMENTATION_PLAN.md (Turbine Dashboard).

Your plan must cover:
- the itemised breach list: exactly what renders for WARNING, CRITICAL, and ERROR turbines
- how the two selectboxes bind to state.py accessors without triggering a rerun loop
- the y-axis swap rule when x is power_output_kw
- what renders when the turbine has no telemetry at all (TURB999 in fixtures)

Implement ONLY Phase 13.
```

**Manual check:** pick a turbine with the gearbox 126.5 anomaly. It must show CRITICAL with a breach
line naming gearbox temperature, the value, and the 110 °C limit — not just a red chip.

---

### Phase 14 — Map layer controls

```bash
scripts/run-phase.sh 14
```

**Manual check of the caching lifecycle** — this is the specified behavior most likely to be faked:

1. Check "Wind streams" → overlay appears after a brief compute.
2. Uncheck → overlay disappears.
3. Re-check → overlay appears **instantly** (served from `nwp_cache`).
4. Refresh the browser → cache is gone, recomputes.
5. Check "Forecasted power output" → **only** the ToDo message, nothing on the map.

---

### Phase 15 — Responsive & performance  ⟨plan mode⟩

```bash
script -q logs/phase-15-plan-$(date +%Y%m%d-%H%M%S).txt claude
```

```
Read PROJECT_SPEC.md section 10.1 and 12. Then plan Phase 15 of IMPLEMENTATION_PLAN.md.

For the performance test, state in your plan how you will generate ~430k synthetic rows inside the
test itself, in memory, without writing anything to data/.

For the responsive work, list the specific CSS changes you expect to need and what could go wrong
at 390x844.

Implement ONLY Phase 15.
```

Then check both aspect ratios in Chrome DevTools device toolbar — 1440×900 and 390×844 — and record
what you saw:

```bash
pytest tests/test_performance.py -v --durations=10 2>&1 | tee logs/phase-15-perf.txt
```

**Expect:** `build_farm_map_rows` under 2 seconds on 430k rows. If it isn't, an N+1 query slipped in.

---

### Phase 16 — README & final verification

```bash
scripts/run-phase.sh 16
```

Then the clean-checkout proof — the thing a reviewer will actually do:

```bash
cd /tmp && rm -rf wfm-verify
git clone ~/Documents/NextEra-Technical/Software_Take_Home_Exercise/wind-fleet-monitor wfm-verify
cd wfm-verify
cp ~/Documents/NextEra-Technical/Software_Take_Home_Exercise/*.csv data/
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ruff format --check . && ruff check . && mypy src app.py && pytest --cov=src \
  2>&1 | tee ~/Documents/NextEra-Technical/Software_Take_Home_Exercise/wind-fleet-monitor/logs/final-clean-checkout.txt
streamlit run app.py
```

If that sequence works from a clean clone, you're done.

---

## Part 3 — Operating notes

### 3.1 The gate, as a one-liner

Keep this aliased; you'll run it after every phase:

```bash
alias gate='ruff format --check . && ruff check . && mypy src app.py && pytest -q'
```

### 3.2 When a phase fails

Resume the same session rather than starting fresh — the model still has the phase's context:

```bash
scripts/run-phase.sh <N> --resume
```

Or interactively, `claude --continue`, then paste the gate output and:

```
The gate failed. Here is the output:
<paste>

Fix the underlying code. Do not disable the lint rule, add a type: ignore, or weaken the test.
Then re-run the gate and report PASS or FAIL.
```

If a phase fails twice, that's a signal the plan is wrong, not the model. Reset and rethink:

```bash
git reset --hard HEAD          # discard the phase's work
```

Then re-run it in plan mode regardless of what the table says, and fix the phase description in
`IMPLEMENTATION_PLAN.md` before retrying.

### 3.3 Guarding against the model gaming the gate

`CLAUDE.md` §3.3 forbids it, but check anyway — once, after phase 8 and again after phase 16:

```bash
grep -rn "type: ignore\|noqa\|xfail\|pytest.skip" src/ tests/ app.py config.py
```

Anything that turns up should have a written justification. Silent suppressions are how a green gate
stops meaning anything.

### 3.4 Fallback logging

If the hooks don't fire, wrap any session in `script(1)`:

```bash
script -q logs/session-$(date +%Y%m%d-%H%M%S).txt claude
```

That captures the raw terminal, including your own keystrokes — less structured than the
`stream-json` logs but completely reliable.

### 3.5 What the logs give you

`logs/` becomes the evidence base for the exercise's **AI Usage Summary** deliverable:

```bash
# every prompt you gave
grep -h '"type":"user"' logs/*.jsonl | jq -r '.message.content' | head -50

# total cost and wall-clock across the build
cat logs/*.meta

# every command Claude ran
jq -r '.tool_input.command // empty' logs/tool-calls.jsonl | sort | uniq -c | sort -rn
```

That last one is worth pasting straight into the write-up. It answers "what did the AI actually do"
with data instead of recollection.

### 3.6 Keep a decisions log as you go

Add a line to `logs/DECISIONS.md` whenever you override or correct the model — a wrong plan you
rejected, a threshold you changed, a hallucinated API you caught. Write it at the moment it happens;
you will not remember by phase 16, and this is precisely what the AI Usage Summary asks for:

```
Phase 6 — Claude's first plan used >= for the gearbox minor threshold, which would flag a turbine at
exactly 95.0 °C. Spec says strictly greater. Corrected in plan mode before any code was written.
```
