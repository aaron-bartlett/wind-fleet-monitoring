"""Shared pytest fixtures for the Wind Fleet Monitor test suite."""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from config import Settings
from src.data import db


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the directory containing small test CSV fixtures.

    Tests must never read the real `data/` CSVs — only files under this
    directory, which are checked into the repo and stay small and stable.
    """
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def db_con(fixtures_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """An in-memory DuckDB connection ingested from `tests/fixtures/`.

    Function-scoped so every test starts from a clean ingest — several tests assert exact
    row/dedup counts that a shared connection could accumulate across tests.
    """
    settings = Settings(
        data_dir=fixtures_dir,
        duckdb_path=Path(":memory:"),
        sim_now=None,
        stale_after_minutes=15,
    )
    con = db.connect(settings)
    db.ingest(con, settings)
    yield con
    con.close()
