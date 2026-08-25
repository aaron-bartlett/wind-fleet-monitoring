"""Enforces the layering rule in CLAUDE.md §4.1: `src/domain/` and `src/data/` must never
import Streamlit-or-rendering-related packages, and must never embed raw SQL outside the two
modules designated to hold it. This keeps business logic unit-testable without a Streamlit
runtime.
"""

import ast
from pathlib import Path

FORBIDDEN_MODULES = {"streamlit", "folium", "streamlit_folium", "branca", "plotly"}
LAYER_DIRS = ("src/domain", "src/data")
SQL_ALLOWED_FILES = {"src/data/queries.py", "src/data/db.py"}
SQL_SUBSTRINGS = ("select ", "insert ", "create table")


def _iter_py_files() -> list[Path]:
    repo_root = Path(__file__).parent.parent
    files: list[Path] = []
    for layer_dir in LAYER_DIRS:
        files.extend((repo_root / layer_dir).rglob("*.py"))
    return files


def _top_level_imports(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module.split(".")[0])
    return modules


def test_domain_and_data_never_import_streamlit_or_rendering_libs() -> None:
    """`src/domain/**` and `src/data/**` must not import UI/rendering packages."""
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        imported = _top_level_imports(tree)
        offending = imported & FORBIDDEN_MODULES
        assert not offending, (
            f"{path} imports forbidden module(s) {sorted(offending)}; "
            f"src/domain/ and src/data/ must stay Streamlit/rendering-free."
        )


def test_domain_and_data_have_no_raw_sql_outside_designated_files() -> None:
    """SQL strings must live only in `src/data/queries.py` and `src/data/db.py`."""
    repo_root = Path(__file__).parent.parent
    for path in _iter_py_files():
        relative_path = path.relative_to(repo_root).as_posix()
        if relative_path in SQL_ALLOWED_FILES:
            continue
        text_lower = path.read_text().lower()
        for substring in SQL_SUBSTRINGS:
            assert substring not in text_lower, (
                f"{relative_path} contains raw SQL substring {substring!r}; "
                f"all SQL must live in src/data/queries.py or src/data/db.py."
            )
