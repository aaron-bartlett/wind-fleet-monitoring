"""Shared pytest fixtures for the Wind Fleet Monitor test suite."""

from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the directory containing small test CSV fixtures.

    Tests must never read the real `data/` CSVs — only files under this
    directory, which are checked into the repo and stay small and stable.
    """
    return Path(__file__).parent / "fixtures"
