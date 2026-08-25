"""Streamlit entrypoint (`CLAUDE.md` §4.1). Wiring and page config only — no business logic.

Connects to DuckDB, resolves "now", injects CSS, initializes session state, and renders the
map/dashboard shell. The map, charts, and dashboards themselves arrive in later phases; this
phase renders placeholder text into both containers.
"""

import logging

import duckdb
import streamlit as st

import config
from src.data import db
from src.domain import clock
from src.errors import WindFleetError
from src.ui import layout, state

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

st.set_page_config(
    page_title="Wind Fleet Monitor",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Set by `get_connection()` only on the one process-lifetime call where ingest actually runs;
# `None` otherwise (including "already current" on every rerun after the first). This is
# process-wide data, not per-session Streamlit state, and belongs alongside the
# `st.cache_resource` connection it describes rather than in `src/ui/state.py`'s contract.
_last_ingest_summary: db.IngestSummary | None = None


@st.cache_resource
def get_connection() -> duckdb.DuckDBPyConnection:
    """Open (or reuse) the cached DuckDB connection, ingesting source CSVs if needed.

    Cached process-wide via `st.cache_resource` (`CLAUDE.md` §5.4) so ingest runs at most once
    per process, not once per script rerun.

    Returns:
        An open, ingested DuckDB connection.

    Raises:
        DataLoadError: A source CSV is missing or malformed (propagates from `db.ingest`).
    """
    global _last_ingest_summary
    settings = config.load_settings()
    con = db.connect(settings)
    if not db.is_ingest_current(con, settings):
        _last_ingest_summary = db.ingest(con, settings)
    return con


def _render_ingest_summary() -> None:
    """Render the collapsed ingest-summary expander, degrading gracefully if nothing ran."""
    with st.expander("Ingest summary"):
        if _last_ingest_summary is None:
            st.write("Data already ingested for the current source files; nothing to report.")
            return
        summary = _last_ingest_summary
        st.write(f"Farms: {summary.farms}")
        st.write(f"Turbines: {summary.turbines}")
        st.write(f"Telemetry rows: {summary.telemetry_rows}")
        st.write(f"Duplicates removed: {summary.duplicates_removed}")
        st.write(f"Rows with nulls: {summary.rows_with_nulls}")
        st.write(f"Telemetry range: {summary.telemetry_start} — {summary.telemetry_end}")
        st.write(f"Elapsed: {summary.elapsed_seconds:.3f}s")


def main() -> None:
    """Render the app shell: header, ingest summary, and the placeholder map/dashboard panes."""
    try:
        settings = config.load_settings()
        con = get_connection()
        now = clock.get_now(con, settings)

        layout.inject_css()
        state.init_state()
        map_container, dashboard_container = layout.render_shell()

        st.markdown(f"### Wind Fleet Monitor — Data as of: {now:%Y-%m-%d %H:%M} UTC")
        _render_ingest_summary()

        with map_container:
            st.write("Map placeholder — arrives in Phase 11.")
        with dashboard_container:
            st.write("Dashboard placeholder — arrives in Phases 11-13.")
    except WindFleetError as exc:
        logger.exception("Failed to start Wind Fleet Monitor.")
        st.error(str(exc))
        st.stop()


main()
