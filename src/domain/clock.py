"""Resolution of "now" for a historical dataset, plus staleness and window-start helpers.

Domain layer (`CLAUDE.md` §4.1): no Streamlit, no SQL. `get_now` composes
`src/data/queries.get_max_timestamp` behind a small pure-Python surface that `health.py`,
`aggregates.py`, and the UI layer all treat as this project's single source of truth for time.
"""

import logging
from datetime import UTC, datetime, timedelta

import duckdb

import config
from config import Settings
from src.data import queries

logger = logging.getLogger(__name__)


def get_now(con: duckdb.DuckDBPyConnection, settings: Settings) -> datetime:
    """Resolve "now" for the app.

    # SPEC-GAP: the seed dataset is historical (Jan 2026), so wall-clock time would mark every
    # turbine permanently stale. Resolution order: `settings.sim_now` if set, else the
    # dataset's own `MAX(timestamp)`, else the wall clock as a last resort (logged, since it
    # means the telemetry table is empty). See PROJECT_SPEC.md §6.1.

    Args:
        con: Open DuckDB connection.
        settings: Runtime settings; `sim_now` takes precedence when set.

    Returns:
        A tz-aware UTC datetime.
    """
    if settings.sim_now is not None:
        return settings.sim_now
    max_timestamp = queries.get_max_timestamp(con)
    if max_timestamp is not None:
        return max_timestamp
    logger.warning("No telemetry rows found; falling back to wall-clock time for 'now'.")
    return datetime.now(UTC)


def is_stale(record_timestamp: datetime, now: datetime, stale_after_minutes: int) -> bool:
    """Return whether a record is older than `stale_after_minutes` relative to `now`.

    Args:
        record_timestamp: The record's measurement timestamp; must be tz-aware.
        now: The reference "now"; must be tz-aware.
        stale_after_minutes: Staleness threshold in minutes.

    Returns:
        `True` when `record_timestamp` is strictly older than `now - stale_after_minutes`; a
        record exactly at the boundary is not stale.

    Raises:
        ValueError: Either datetime is naive.
    """
    if record_timestamp.tzinfo is None:
        raise ValueError("record_timestamp must be tz-aware.")
    if now.tzinfo is None:
        raise ValueError("now must be tz-aware.")
    return record_timestamp < now - timedelta(minutes=stale_after_minutes)


def window_start(now: datetime, window_key: str) -> datetime | None:
    """Return the start of a named history window ending at `now`.

    Args:
        now: The window's end (tz-aware).
        window_key: One of `config.TIME_WINDOWS`'s keys, e.g. `"24h"`.

    Returns:
        `now - delta`, or `None` for `"all"` (no lower bound).

    Raises:
        ValueError: `window_key` is not a recognized window.
    """
    if window_key not in config.TIME_WINDOWS:
        raise ValueError(
            f"Unknown window_key {window_key!r}; must be one of {list(config.TIME_WINDOWS)}"
        )
    delta = config.TIME_WINDOWS[window_key]
    return None if delta is None else now - delta
