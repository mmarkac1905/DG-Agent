"""Stage D.1 unit tests for app/_term_status_utils.filter_active_terms.

Replaces the copy-pasted `_active_glossary` filter across 6 call sites
with a single tested helper. Tests cover:
  - archived rows dropped
  - missing status field defaults to active (row kept)
  - empty DataFrame returns empty unchanged
  - mixed status values
  - DataFrame without 'status' column
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))

from _term_status_utils import filter_active_terms  # noqa: E402


def test_01_drops_archived() -> None:
    df = pd.DataFrame([
        {"id": "BG001", "status": "approved"},
        {"id": "BG002", "status": "archived"},
        {"id": "BG003", "status": "draft"},
    ])
    result = filter_active_terms(df)
    assert set(result["id"]) == {"BG001", "BG003"}


def test_02_missing_status_defaults_active() -> None:
    """Rows with NaN/None status are treated as active (kept)."""
    df = pd.DataFrame([
        {"id": "BG001", "status": "approved"},
        {"id": "BG002", "status": None},
        {"id": "BG003", "status": "archived"},
    ])
    result = filter_active_terms(df)
    # BG001 kept (approved), BG002 kept (None/NaN cast to 'nan', not 'archived'),
    # BG003 dropped (archived).
    assert "BG001" in set(result["id"])
    assert "BG002" in set(result["id"])
    assert "BG003" not in set(result["id"])


def test_03_empty_dataframe() -> None:
    df = pd.DataFrame(columns=["id", "status"])
    result = filter_active_terms(df)
    assert len(result) == 0
    assert list(result.columns) == ["id", "status"]


def test_04_no_status_column() -> None:
    """DataFrame without a 'status' column — all rows are active."""
    df = pd.DataFrame([{"id": "BG001"}, {"id": "BG002"}])
    result = filter_active_terms(df)
    assert len(result) == 2
    assert set(result["id"]) == {"BG001", "BG002"}


def test_05_all_archived() -> None:
    df = pd.DataFrame([
        {"id": "BG001", "status": "archived"},
        {"id": "BG002", "status": "archived"},
    ])
    result = filter_active_terms(df)
    assert len(result) == 0


def test_06_preserves_other_columns() -> None:
    df = pd.DataFrame([
        {"id": "BG001", "status": "approved", "term_name": "a", "domain": "proc"},
        {"id": "BG002", "status": "archived", "term_name": "b", "domain": "inv"},
    ])
    result = filter_active_terms(df)
    assert list(result.columns) == ["id", "status", "term_name", "domain"]
    assert result.iloc[0]["term_name"] == "a"
    assert result.iloc[0]["domain"] == "proc"


def _run_standalone() -> int:
    tests = [
        test_01_drops_archived,
        test_02_missing_status_defaults_active,
        test_03_empty_dataframe,
        test_04_no_status_column,
        test_05_all_archived,
        test_06_preserves_other_columns,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERR ] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
