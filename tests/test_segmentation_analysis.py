"""Tests for scripts/run_segmentation_analysis.analyze_table.

Covers known_issue #76 regression: when a table has numeric columns but
every column is individually skipped (constant or all-null), the loop
used to complete with emitted==0 and no DAR written. The fix emits a
status='skipped' DAR so Stage D.1 §4.3b's 'analyzer ran, no findings'
signal surfaces to downstream readers (term_eda_prereq, context
assembler).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import run_segmentation_analysis as seg  # noqa: E402
import _dar_supersede as sup  # noqa: E402


def _write_empty_dar_csv(path: Path) -> None:
    """Seed the DAR CSV with just the header so _next_dar_id resolves."""
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=seg._DAR_FIELDS, lineterminator="\n",
            quoting=csv.QUOTE_MINIMAL,
        )
        w.writeheader()


def _read_dar_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_segmentation_emits_skipped_dar_when_all_columns_constant(
    tmp_path, monkeypatch,
) -> None:
    """Regression for #76.

    Given a raw table with numeric columns whose values are all constant
    (stddev==0), `_analyze_column` returns None for each — the for loop
    skips every column individually. The analyzer must still emit a
    status='skipped' DAR so downstream prereq checks see the analyzer as
    having run.
    """
    dar_csv = tmp_path / "dar.csv"
    _write_empty_dar_csv(dar_csv)

    # Monkeypatch the CSV path on both segmentation + supersede modules.
    monkeypatch.setattr(seg, "_DAR_CSV", dar_csv)
    monkeypatch.setattr(sup, "_DAR_CSV", dar_csv)

    # In-memory DuckDB with a raw_sap table mimicking objk: one numeric
    # column (OBZAE) whose value is constant across all rows.
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute('CREATE TABLE raw_sap.objk_fixture (OBZAE BIGINT, OBKNR VARCHAR)')
    conn.execute(
        "INSERT INTO raw_sap.objk_fixture VALUES "
        "(1, 'A'), (1, 'B'), (1, 'C'), (1, 'D'), (1, 'E')"
    )

    emitted = seg.analyze_table(conn, "objk_fixture")

    # Function returns 1 (one skipped DAR written).
    assert emitted == 1, (
        f"expected 1 skipped DAR written (fallback path); got emitted={emitted}"
    )

    rows = _read_dar_rows(dar_csv)
    # One DAR row in the CSV.
    assert len(rows) == 1, f"expected 1 DAR row; got {len(rows)}: {rows}"

    r = rows[0]
    assert r["analysis_type"] == "segmentation_threshold"
    assert r["source_tables"] == "objk_fixture"
    assert r["status"] == "skipped"
    assert r["superseded_by"] in ("", None), "no prior DARs → no supersede pointer"
    # Skip reason documents WHY the table was skipped — not just that it was.
    assert "constant" in r.get("result_json", "").lower() or \
           "constant" in r.get("error_message", "").lower() or \
           "constant" in r.get("query_sql", "").lower(), (
        f"skip reason should mention 'constant'; row={r}"
    )


def test_segmentation_does_not_emit_skipped_when_a_column_had_findings(
    tmp_path, monkeypatch,
) -> None:
    """Happy path: if at least one column produces findings, no fallback
    skipped DAR is emitted — only the per-column success DARs."""
    dar_csv = tmp_path / "dar.csv"
    _write_empty_dar_csv(dar_csv)

    monkeypatch.setattr(seg, "_DAR_CSV", dar_csv)
    monkeypatch.setattr(sup, "_DAR_CSV", dar_csv)

    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute('CREATE TABLE raw_sap.mixed_fixture (VAL BIGINT, CONST_COL BIGINT)')
    # VAL has distribution → one success DAR; CONST_COL is constant → skipped.
    conn.execute(
        "INSERT INTO raw_sap.mixed_fixture VALUES "
        "(10, 1), (20, 1), (30, 1), (40, 1), (50, 1)"
    )

    emitted = seg.analyze_table(conn, "mixed_fixture")
    assert emitted == 1, f"one column produced findings; expected emitted==1, got {emitted}"

    rows = _read_dar_rows(dar_csv)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "success", (
        f"expected success DAR (fallback should not fire when emitted>0); got {r!r}"
    )


def test_segmentation_still_emits_skipped_when_no_numeric_columns(
    tmp_path, monkeypatch,
) -> None:
    """Sanity: the pre-existing top-of-function skip path (no numeric
    columns at all) still emits a skipped DAR — the #76 fix doesn't
    regress that case."""
    dar_csv = tmp_path / "dar.csv"
    _write_empty_dar_csv(dar_csv)

    monkeypatch.setattr(seg, "_DAR_CSV", dar_csv)
    monkeypatch.setattr(sup, "_DAR_CSV", dar_csv)

    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute('CREATE TABLE raw_sap.text_only (A VARCHAR, B VARCHAR)')
    conn.execute("INSERT INTO raw_sap.text_only VALUES ('x', 'y')")

    emitted = seg.analyze_table(conn, "text_only")
    assert emitted == 1

    rows = _read_dar_rows(dar_csv)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped"
