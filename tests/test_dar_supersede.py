"""Unit tests for scripts/_dar_supersede.supersede_prior_dars_for_table.

Covers the known_issue #73 fix — when an analyzer re-writes its DAR row
for a given (analysis_type, source_tables) pair, prior rows for that pair
must be flipped to superseded_by=<new_dar_id>, status='superseded' so the
Piece 8 context assembler's `WHERE superseded_by IS NULL` filter returns
only the latest evidence.

Tests monkeypatch the module-level `_DAR_CSV` path so each test writes
its own isolated CSV in a tmp dir.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _dar_supersede as mod  # noqa: E402


_FIELDS = mod._DAR_FIELDS


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS, lineterminator="\n",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in _FIELDS})


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _row(rid: str, analysis_type: str, source_tables: str,
         status: str = "success", superseded_by: str = "") -> dict:
    return {
        "id": rid,
        "analysis_type": analysis_type,
        "source_tables": source_tables,
        "status": status,
        "superseded_by": superseded_by,
        "executed_at_utc": "2026-04-24 01:00:00",
    }


def test_flips_prior_rows_for_same_analyzer_and_table(tmp_path, monkeypatch):
    csv_path = tmp_path / "dar.csv"
    _write(csv_path, [
        _row("DAR-01", "completeness", "equi"),
        _row("DAR-02", "completeness", "equi"),
    ])
    monkeypatch.setattr(mod, "_DAR_CSV", csv_path)

    flipped = mod.supersede_prior_dars_for_table(
        "completeness", "equi", ["DAR-02"],
    )
    assert flipped == 1

    rows = {r["id"]: r for r in _read(csv_path)}
    assert rows["DAR-01"]["superseded_by"] == "DAR-02"
    assert rows["DAR-01"]["status"] == "superseded"
    assert rows["DAR-02"]["superseded_by"] == ""
    assert rows["DAR-02"]["status"] == "success"


def test_does_not_flip_rows_for_different_analysis_type(tmp_path, monkeypatch):
    csv_path = tmp_path / "dar.csv"
    _write(csv_path, [
        _row("DAR-01", "completeness", "equi"),
        _row("DAR-02", "dimensions",   "equi"),
        _row("DAR-03", "completeness", "equi"),
    ])
    monkeypatch.setattr(mod, "_DAR_CSV", csv_path)

    flipped = mod.supersede_prior_dars_for_table(
        "completeness", "equi", ["DAR-03"],
    )
    assert flipped == 1
    rows = {r["id"]: r for r in _read(csv_path)}
    assert rows["DAR-02"]["superseded_by"] == ""
    assert rows["DAR-02"]["status"] == "success"


def test_does_not_flip_rows_for_different_source_table(tmp_path, monkeypatch):
    csv_path = tmp_path / "dar.csv"
    _write(csv_path, [
        _row("DAR-01", "completeness", "equi"),
        _row("DAR-02", "completeness", "ekko"),
        _row("DAR-03", "completeness", "equi"),
    ])
    monkeypatch.setattr(mod, "_DAR_CSV", csv_path)

    flipped = mod.supersede_prior_dars_for_table(
        "completeness", "equi", ["DAR-03"],
    )
    assert flipped == 1
    rows = {r["id"]: r for r in _read(csv_path)}
    assert rows["DAR-02"]["superseded_by"] == ""


def test_does_not_flip_rows_in_new_dar_ids(tmp_path, monkeypatch):
    """Multi-row case (temporal_coverage): helper called with all new ids.
    None of them should be flipped — they're all part of the latest run."""
    csv_path = tmp_path / "dar.csv"
    _write(csv_path, [
        _row("DAR-01", "temporal_coverage", "equi"),
        _row("DAR-02", "temporal_coverage", "equi"),
        _row("DAR-10", "temporal_coverage", "equi"),  # new
        _row("DAR-11", "temporal_coverage", "equi"),  # new
    ])
    monkeypatch.setattr(mod, "_DAR_CSV", csv_path)

    flipped = mod.supersede_prior_dars_for_table(
        "temporal_coverage", "equi", ["DAR-10", "DAR-11"],
    )
    assert flipped == 2

    rows = {r["id"]: r for r in _read(csv_path)}
    assert rows["DAR-01"]["superseded_by"] == "DAR-10"  # pointer is new_ids[0]
    assert rows["DAR-02"]["superseded_by"] == "DAR-10"
    assert rows["DAR-01"]["status"] == "superseded"
    assert rows["DAR-02"]["status"] == "superseded"
    assert rows["DAR-10"]["superseded_by"] == ""
    assert rows["DAR-11"]["superseded_by"] == ""


def test_does_not_double_flip_already_superseded_rows(tmp_path, monkeypatch):
    csv_path = tmp_path / "dar.csv"
    _write(csv_path, [
        _row("DAR-01", "completeness", "equi",
             status="superseded", superseded_by="DAR-02"),
        _row("DAR-02", "completeness", "equi"),
        _row("DAR-03", "completeness", "equi"),
    ])
    monkeypatch.setattr(mod, "_DAR_CSV", csv_path)

    flipped = mod.supersede_prior_dars_for_table(
        "completeness", "equi", ["DAR-03"],
    )
    assert flipped == 1  # only DAR-02, not DAR-01 (already superseded)

    rows = {r["id"]: r for r in _read(csv_path)}
    assert rows["DAR-01"]["superseded_by"] == "DAR-02"  # preserved original pointer
    assert rows["DAR-02"]["superseded_by"] == "DAR-03"  # newly flipped


def test_returns_zero_and_noops_when_no_priors_match(tmp_path, monkeypatch):
    csv_path = tmp_path / "dar.csv"
    _write(csv_path, [
        _row("DAR-01", "dimensions", "ekko"),
    ])
    monkeypatch.setattr(mod, "_DAR_CSV", csv_path)

    bytes_before = csv_path.read_bytes()
    flipped = mod.supersede_prior_dars_for_table(
        "completeness", "equi", ["DAR-99"],
    )
    assert flipped == 0
    # File must be untouched when there's nothing to flip — no atomic rewrite.
    assert csv_path.read_bytes() == bytes_before


def test_returns_zero_when_csv_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_DAR_CSV", tmp_path / "does_not_exist.csv")
    flipped = mod.supersede_prior_dars_for_table(
        "completeness", "equi", ["DAR-01"],
    )
    assert flipped == 0


def test_returns_zero_when_new_dar_ids_empty(tmp_path, monkeypatch):
    csv_path = tmp_path / "dar.csv"
    _write(csv_path, [_row("DAR-01", "completeness", "equi")])
    monkeypatch.setattr(mod, "_DAR_CSV", csv_path)

    flipped = mod.supersede_prior_dars_for_table(
        "completeness", "equi", [],
    )
    assert flipped == 0
