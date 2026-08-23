# CLAUDE.md — Wind Fleet Monitor

Project guardrails for all AI-assisted and human development in this repository.
**Read this file before writing any code. It overrides general convention.**

The functional requirements live in `PROJECT_SPEC.md`. The build order lives in `IMPLEMENTATION_PLAN.md`.
This file defines *how* code must be written; those files define *what* to write and *in what order*.

---

## 1. Project Summary

A Streamlit web application for monitoring a fleet of wind farms. A full-viewport Folium map drills
Fleet → Farm → Turbine; a context-sensitive dashboard renders alongside it. Data comes from three CSVs
(`farms`, `turbines`, `telemetry`) loaded into DuckDB. All aggregation happens in SQL.

---

## 2. Tech Stack & Tooling

### 2.1 Runtime

- **Python 3.11+** (uses `zoneinfo`, `datetime.UTC`, PEP 604 unions, `Self` types).
- Standard library `venv`. No Poetry, no Conda, no uv — plain `pip` + `requirements.txt` so the
  reviewer can run the project with zero extra tooling.

### 2.2 Runtime dependencies — `requirements.txt`

Use compatible-release pins (`~=`). Exact patch versions are resolved at install time.

```
streamlit~=1.40
streamlit-folium~=0.24
folium~=0.18
branca~=0.8
duckdb~=1.1
pandas~=2.2
numpy~=2.1
plotly~=5.24
scipy~=1.14
timezonefinder~=6.5
```

**Not installed in v1:** `herbie-data`. The HRRR provider is a `NotImplementedError` skeleton
(`PROJECT_SPEC.md` §9). Record it in `requirements-optional.txt` with a comment, and never `import herbie`
at module scope — if it is ever wired up, the import goes inside the method body.

### 2.3 Development dependencies — `requirements-dev.txt`

```
-r requirements.txt
pytest~=8.3
pytest-cov~=6.0
ruff~=0.8
mypy~=1.13
pandas-stubs~=2.2
```

### 2.4 Tooling choices (not specified in `PROJECT_SPEC.md` — chosen as stack standard)

| Concern | Choice | Rationale |
|---|---|---|
| Lint + format | **Ruff** (`ruff check`, `ruff format`) | Single tool replacing flake8/isort/black. Configured in `pyproject.toml`. |
| Type checking | **mypy** | `--strict` on `src/domain` and `src/data`; relaxed on `src/ui` because Streamlit and Folium ship incomplete stubs. |
| Testing | **pytest** | Fixtures in `tests/conftest.py`. No unittest classes. |
| Config / settings | **stdlib `os.environ` + frozen dataclasses** in `config.py` | Avoids adding pydantic for ~6 settings. Typed and immutable. |
| Logging | **stdlib `logging`** | Module-level `logger = logging.getLogger(__name__)`. Never `print()` outside `app.py`. |
| App state | **`st.session_state`, accessed only through `src/ui/state.py`** | See §5.1. |

### 2.5 `pyproject.toml` — required configuration

```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "A", "C4", "SIM", "ARG", "PTH", "RUF"]
ignore = ["E501"]  # formatter owns line length

[tool.mypy]
python_version = "3.11"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true

[[tool.mypy.overrides]]
module = ["src.domain.*", "src.data.*"]
strict = true

[[tool.mypy.overrides]]
module = ["folium.*", "streamlit_folium.*", "branca.*", "timezonefinder.*", "plotly.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q --strict-markers"
```

---

## 3. Build, Test & Run Commands

Every command runs from the repository root with the virtualenv active.

### 3.1 One-time setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

### 3.2 The command set

| Purpose | Command |
|---|---|
| Run the app | `streamlit run app.py` |
| Run all tests | `pytest` |
| Run one test file | `pytest tests/test_health.py -v` |
| Tests with coverage | `pytest --cov=src --cov-report=term-missing` |
| Lint | `ruff check .` |
| Auto-fix lint | `ruff check --fix .` |
| Format | `ruff format .` |
| Format check (CI-style) | `ruff format --check .` |
| Type check | `mypy src app.py` |
| **Full gate — must pass before any phase is complete** | `ruff format --check . && ruff check . && mypy src app.py && pytest` |

### 3.3 Definition of done for every phase

A phase is complete only when the **full gate** in §3.2 exits 0 *and* the phase's own verification
command in `IMPLEMENTATION_PLAN.md` passes. Do not proceed to the next phase otherwise.
Do not disable a lint rule, add `# type: ignore`, or mark a test `xfail` to make the gate pass —
fix the underlying issue. If a rule is genuinely wrong for this project, change `pyproject.toml`
and say so in the commit message.

---

## 4. Architecture & Directory Conventions

### 4.1 Layering — the central rule

```
   app.py  ──►  src/ui/  ──►  src/domain/  ──►  src/data/
                              (pure logic)      (DuckDB)
```

Dependencies point **one direction only**, right to left in the import sense:

- `src/data/` may import: stdlib, duckdb, pandas, `config`, `src/errors.py`.
- `src/domain/` may import: stdlib, numpy, pandas, `config`, `src/errors.py`, and `src/data/`.
- `src/ui/` may import: everything, including `streamlit`.
- `app.py` is the only entrypoint and wires the layers together.

> **Hard rule — enforced by a test.** `src/domain/**` and `src/data/**` must **never** import
> `streamlit`, `folium`, `streamlit_folium`, `branca`, or `plotly`. `tests/test_architecture.py`
> asserts this by AST-scanning the source tree. This makes all business logic unit-testable
> without a Streamlit runtime and is an explicit evaluation criterion for the project.

### 4.2 Exact directory structure

New code goes **only** in these locations. Do not invent new top-level directories.

```
wind-fleet-monitor/
├── app.py                       # Streamlit entrypoint. Wiring + page config ONLY.
├── config.py                    # All constants, thresholds, colors, settings. No logic.
├── pyproject.toml               # ruff / mypy / pytest config
├── requirements.txt
├── requirements-dev.txt
├── requirements-optional.txt    # herbie-data, documented as not installed
├── CLAUDE.md                    # this file
├── PROJECT_SPEC.md              # functional spec
├── IMPLEMENTATION_PLAN.md       # phased build order
├── README.md                    # written in the final phase
├── .gitignore
├── data/                        # input CSVs + generated fleet.duckdb (gitignored)
│   ├── farms.csv
│   ├── turbines.csv
│   └── telemetry.csv
├── src/
│   ├── __init__.py
│   ├── errors.py                # exception hierarchy
│   ├── data/
│   │   ├── __init__.py
│   │   ├── db.py                # connection, ingest, schema, dedup, views
│   │   └── queries.py           # ALL SQL. One named function per query.
│   ├── domain/
│   │   ├── __init__.py
│   │   ├── models.py            # shared dataclasses & enums
│   │   ├── clock.py             # "now" resolution
│   │   ├── health.py            # threshold rules → HealthResult
│   │   ├── aggregates.py        # fleet/farm/turbine roll-ups
│   │   ├── geo.py               # bounds math, timezone lookup
│   │   └── nwp.py               # NWPProvider protocol + Stub + HRRR skeleton
│   └── ui/
│       ├── __init__.py
│       ├── state.py             # typed session_state accessors
│       ├── layout.py            # CSS injection, responsive shell
│       ├── map_view.py          # builds the Folium object
│       ├── charts.py            # Plotly figure builders
│       └── dashboards/
│           ├── __init__.py
│           ├── fleet.py
│           ├── farm.py
│           └── turbine.py
└── tests/
    ├── __init__.py
    ├── conftest.py              # shared fixtures (tiny in-memory DuckDB, sample records)
    ├── fixtures/                # small CSVs used by tests — NEVER the real data/
    ├── test_architecture.py
    ├── test_config.py
    ├── test_ingest.py
    ├── test_queries.py
    ├── test_clock.py
    ├── test_health.py
    ├── test_aggregates.py
    ├── test_geo.py
    ├── test_nwp.py
    ├── test_charts.py
    └── test_smoke.py
```

### 4.3 Placement rules

- **A new constant, threshold, color, or default** → `config.py`. Never inline a magic number in
  `src/`. If you type a number that is not `0`, `1`, or an array index, it belongs in `config.py`.
- **A new SQL string** → `src/data/queries.py` as a named function. SQL must never appear in
  `src/domain/` or `src/ui/`.
- **A new shared dataclass or enum** → `src/domain/models.py`. Do not redefine types per module.
- **A new Plotly figure** → `src/ui/charts.py` as a `build_*(...) -> go.Figure` function. Dashboard
  modules call these; they never construct traces themselves.
- **A new test** → `tests/test_<module>.py` mirroring the source module name.
- **Test data** → `tests/fixtures/`. Tests must never read `data/*.csv`; the real dataset will be
  expanded later and would silently break assertions.

---

## 5. Code Style & Principles

### 5.1 State management

Streamlit state is the single largest source of bugs in this stack. Three non-negotiable rules:

1. **No module outside `src/ui/state.py` may touch `st.session_state` directly.** Every read and
   write goes through a typed accessor: `state.get_level()`, `state.select_farm(farm_id)`,
   `state.reset_view()`. Grep for `session_state` — it must appear only in `state.py`.
2. **`state.py` defines the state shape once**, as a `TypedDict` (`AppState`), and initializes every
   key in a single `init_state()` called at the top of `app.py`. Never rely on
   `if "key" not in st.session_state` scattered through the codebase.
3. **Guard every `st.rerun()`.** Only mutate state and rerun when the new value *differs* from the
   current one. An unguarded rerun on a map click produces an infinite loop. Pattern:

```python
clicked_id = extract_clicked_id(map_return)
if clicked_id is not None and clicked_id != state.get_selected_farm_id():
    state.select_farm(clicked_id)
    st.rerun()
```

### 5.2 Typing

- **Every function and method has full type annotations**, including `-> None`. `disallow_untyped_defs`
  is on.
- Use modern syntax: `str | None`, `list[str]`, `dict[str, float]`. Never `Optional`, `List`, `Dict`.
- Prefer `@dataclass(frozen=True, slots=True)` for value objects; `enum.StrEnum` for status/level
  enums so they serialize cleanly into Folium tooltips.
- Timestamps are **always** `datetime` objects with `tzinfo` set. A naive datetime anywhere in this
  codebase is a bug. Construct with `datetime.now(UTC)` or `datetime(..., tzinfo=UTC)`.
- Public functions returning tabular data return `pd.DataFrame`; document the expected columns in the
  docstring. Functions returning single entities return dataclasses, not dicts.

### 5.3 Error handling

Define the hierarchy in `src/errors.py`:

```python
class WindFleetError(Exception): ...
class DataLoadError(WindFleetError): ...      # missing file, missing column, bad parse
class QueryError(WindFleetError): ...          # DuckDB failure
class ConfigError(WindFleetError): ...         # invalid threshold config
class NWPUnavailableError(WindFleetError): ... # provider cannot serve a request
```

Rules:

- **Fail fast and loud at startup.** A missing CSV or a missing required column raises `DataLoadError`
  with a message naming the file and the specific problem. `app.py` catches `WindFleetError` at the top
  level and renders `st.error(str(e))` followed by `st.stop()`. Never a bare traceback in the UI.
- **Degrade gracefully at render time.** A farm with no turbines, a turbine with no telemetry, or an
  unavailable NWP provider must render an explanatory message in place of the widget — never raise.
  See `PROJECT_SPEC.md` §11 for the required behavior per case.
- **Never use a bare `except:` or `except Exception: pass`.** Catch the narrowest type. If you catch
  broadly at a UI boundary, log the exception with `logger.exception(...)` and show the user something.
- **Never return sentinel values** (`-1`, `""`, `0`) to signal failure. Return `None` with an
  `| None` annotation, or raise.

### 5.4 Data access

- **Never `SELECT *` from `telemetry` into pandas.** All aggregation, bucketing, and filtering is SQL.
  A reviewer will check this; it is the project's stated scalability position (`PROJECT_SPEC.md` §12).
- **Always bind parameters** with `?`. Never f-string a value into SQL, even an internally-derived one.
- Every query function in `queries.py` takes `con: duckdb.DuckDBPyConnection` as its first argument.
  No module-level global connection.
- Cache the connection with `@st.cache_resource` and query results with `@st.cache_data` — but apply
  those decorators in the **UI layer only** (thin wrappers in `src/ui/`), never on `src/data/` functions.
  This keeps `src/data/` free of Streamlit imports per §4.1.
- Time series returned to the browser are capped at **2,000 points**; scatter plots at **5,000 points**.
  Down-sample in SQL, not in pandas.

### 5.5 Naming & units

- Any variable carrying a physical quantity **must** end in its unit: `power_kw`, `wind_speed_ms`,
  `temp_c`, `energy_mwh`, `pitch_deg`, `lag_minutes`. This is the domain's most common bug class.
- Do not conflate power and energy. `PROJECT_SPEC.md` §16 requires "Total Power Output" be rendered
  as **Total Energy (MWh)**.
- Entity ID variables are `farm_id`, `turbine_id` — never `id`, never `fid`.
- Private helpers are `_prefixed`. Modules export a small, deliberate public surface.

### 5.6 Documentation

- Every module opens with a one-paragraph docstring stating its responsibility and its layer.
- Every public function has a docstring with Args/Returns/Raises. Skip docstrings on obvious private
  helpers rather than writing noise.
- Comments explain **why**, never **what**. A comment restating the code is deleted.
- When a decision resolves a gap in `PROJECT_SPEC.md`, add `# SPEC-GAP: <decision> (see PROJECT_SPEC.md §16)`
  at the site and confirm the row exists in that table.

### 5.7 Testing discipline

- Business logic in `src/domain/` requires **table-driven tests covering each threshold at, just below,
  and just above its boundary.** `health.py` is the most heavily tested module in the project.
- Tests must not require a running Streamlit server, network access, or the real `data/` CSVs.
- Use `tests/conftest.py` fixtures for an in-memory DuckDB seeded from `tests/fixtures/*.csv`.
- Assert on values, not on "no exception raised". A test that only checks a call succeeds is not a test.

### 5.8 What not to do

- Do not add dependencies not listed in §2. If a task seems to need one, stop and flag it.
- Do not build features marked ToDo in `PROJECT_SPEC.md` (power-output forecasting §8.4, real HRRR
  fetching §9). Build the placeholder exactly as specified and move on.
- Do not generate, synthesize, or modify the CSVs in `data/`. They will be expanded externally.
- Do not "improve" the spec mid-implementation. If something is wrong or ambiguous, implement the
  closest reasonable reading, add a `# SPEC-GAP:` comment, and record it in `README.md`.
- Do not reformat, refactor, or reorganize files outside the current phase's **Target Files**.
