"""Piece 9 Stage C unit tests for _tar_writer.

Covers:
  - TAR id sequential generation (TAR-NNNNN zero-padded).
  - run_id format (TARRUN-YYYYMMDDHHMMSS-<term_id>).
  - Row construction helpers validate enum fields + shape.
  - write_tar_run supersedes prior success rows for the same term.
  - Cross-term rows not superseded.

Fixtures use a temp CSV (tmp_path). `sync_parquet=False` passed to
write_tar_run to skip the parquet side-effect in tests.

Run standalone:
  python tests/test_tar_writer.py
Or under pytest:
  pytest tests/test_tar_writer.py
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _tar_writer as writer  # noqa: E402


# ─── helpers ───────────────────────────────────────────────────────────

def _swap_csv(tmp_csv: Path):
    """Point writer._TAR_CSV at tmp_csv; return previous for restore."""
    prev = writer._TAR_CSV
    writer._TAR_CSV = tmp_csv
    return prev


def _write_empty_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=writer.TAR_FIELDS, lineterminator="\n")
        w.writeheader()


def _read_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ─── tests ─────────────────────────────────────────────────────────────

def test_01_next_tar_id_empty() -> None:
    assert writer._next_tar_id([]) == "TAR-00001"


def test_02_next_tar_id_sequential() -> None:
    existing = [
        {"id": "TAR-00001"}, {"id": "TAR-00002"}, {"id": "TAR-00003"},
    ]
    assert writer._next_tar_id(existing) == "TAR-00004"


def test_03_next_tar_id_ignores_non_tar_ids() -> None:
    existing = [{"id": "FOO-00042"}, {"id": "TAR-00005"}, {"id": ""}]
    assert writer._next_tar_id(existing) == "TAR-00006"


def test_04_build_run_id_shape() -> None:
    rid = writer.build_run_id("BG027")
    assert rid.startswith("TARRUN-")
    assert rid.endswith("-BG027")
    parts = rid.split("-", 2)
    assert len(parts[1]) == 14  # YYYYMMDDHHMMSS
    assert parts[1].isdigit()


def test_05_build_run_id_preserves_hyphenated_term_id() -> None:
    rid = writer.build_run_id("BG-T25-TERMEDA")
    assert rid.endswith("-BG-T25-TERMEDA")


def test_06_build_query_row_shape() -> None:
    row = writer.build_query_row(
        term_id="BG027",
        analysis_lens="measures_overview",
        stage="framework_floor",
        query_index=1,
        query_sql="SELECT COUNT(*) FROM main_staging.stg_sap__mseg",
        query_result_json='[{"count": 42}]',
        result_row_count=1,
        interpretation="Total rows observed",
        grounded_in_tar_ids=["TAR-00001"],
    )
    assert row["row_type"] == "query"
    assert row["analysis_lens"] == "measures_overview"
    assert row["stage"] == "framework_floor"
    assert row["status"] == "success"
    assert row["confidence"] == ""
    assert row["sufficiency_json"] == ""
    assert json.loads(row["grounded_in_tar_ids"]) == ["TAR-00001"]


def test_07_build_query_row_rejects_invalid_lens() -> None:
    try:
        writer.build_query_row(
            term_id="BG027",
            analysis_lens="not_a_lens",  # invalid
            stage="framework_floor",
            query_index=1,
            query_sql="SELECT 1",
            query_result_json="[]",
            result_row_count=0,
            interpretation="",
            grounded_in_tar_ids=[],
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_08_build_sufficiency_row_shape() -> None:
    row = writer.build_sufficiency_row(
        term_id="BG027",
        sufficiency_json={"declared_sufficient": True},
        confidence="high",
        query_index=5,
        grounded_in_tar_ids=[],
    )
    assert row["row_type"] == "sufficiency"
    assert row["analysis_lens"] == ""
    assert row["stage"] == "terminal"
    assert row["confidence"] == "high"
    assert json.loads(row["sufficiency_json"])["declared_sufficient"] is True


def test_09_build_sufficiency_row_rejects_invalid_confidence() -> None:
    try:
        writer.build_sufficiency_row(
            term_id="BG027",
            sufficiency_json={"declared_sufficient": True},
            confidence="SUPER_HIGH",  # invalid
            query_index=1,
            grounded_in_tar_ids=[],
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_10_write_tar_run_empty_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "term_analysis_results.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        q1 = writer.build_query_row(
            term_id="BG027", analysis_lens="measures_overview",
            stage="framework_floor", query_index=1,
            query_sql="SELECT 1", query_result_json="[]",
            result_row_count=0, interpretation="",
            grounded_in_tar_ids=[],
        )
        suff = writer.build_sufficiency_row(
            term_id="BG027",
            sufficiency_json={"declared_sufficient": True,
                               "lens_consideration": {}},
            confidence="high", query_index=2,
            grounded_in_tar_ids=[],
        )
        run_id, new_ids = writer.write_tar_run(
            "BG027", [q1], suff,
            executed_by="test", sync_parquet=False,
        )
        assert run_id.startswith("TARRUN-")
        assert len(new_ids) == 2  # sufficiency + 1 query
        # Sufficiency id is first-returned
        suff_id, q_id = new_ids
        assert suff_id == "TAR-00001"
        assert q_id == "TAR-00002"

        rows = _read_rows(csv_path)
        assert len(rows) == 2
        ids = {r["id"] for r in rows}
        assert ids == {suff_id, q_id}
        for r in rows:
            assert r["run_id"] == run_id
            assert r["executed_by"] == "test"
            assert r["status"] == "success"
    finally:
        writer._TAR_CSV = prev


def test_11_write_tar_run_supersedes_prior_success(tmp_path: Path) -> None:
    csv_path = tmp_path / "term_analysis_results.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        # First run
        q1 = writer.build_query_row(
            term_id="BG027", analysis_lens="measures_overview",
            stage="framework_floor", query_index=1,
            query_sql="SELECT 1", query_result_json="[]",
            result_row_count=0, interpretation="",
            grounded_in_tar_ids=[],
        )
        suff1 = writer.build_sufficiency_row(
            term_id="BG027",
            sufficiency_json={"declared_sufficient": True},
            confidence="medium", query_index=2,
            grounded_in_tar_ids=[],
        )
        run1_id, run1_ids = writer.write_tar_run(
            "BG027", [q1], suff1,
            executed_by="test", sync_parquet=False,
            run_id="TARRUN-20260422100000-BG027",
        )
        suff1_id = run1_ids[0]

        # Second run — explicit run_id override to avoid second-precision
        # collision with run 1 in fast test execution.
        q2 = writer.build_query_row(
            term_id="BG027", analysis_lens="by_dimension",
            stage="framework_floor", query_index=1,
            query_sql="SELECT x FROM t", query_result_json="[]",
            result_row_count=0, interpretation="",
            grounded_in_tar_ids=[],
        )
        suff2 = writer.build_sufficiency_row(
            term_id="BG027",
            sufficiency_json={"declared_sufficient": True},
            confidence="high", query_index=2,
            grounded_in_tar_ids=[],
        )
        run2_id, run2_ids = writer.write_tar_run(
            "BG027", [q2], suff2,
            executed_by="test", sync_parquet=False,
            run_id="TARRUN-20260422100030-BG027",
        )
        suff2_id = run2_ids[0]
        assert run1_id != run2_id

        rows = _read_rows(csv_path)
        assert len(rows) == 4  # 2 from run1 + 2 from run2
        # Run1 rows → superseded, superseded_by points at run2's sufficiency
        run1_rows = [r for r in rows if r["run_id"] == run1_id]
        assert len(run1_rows) == 2
        for r in run1_rows:
            assert r["status"] == "superseded"
            assert r["superseded_by"] == suff2_id
        # Run2 rows → success
        run2_rows = [r for r in rows if r["run_id"] == run2_id]
        assert len(run2_rows) == 2
        for r in run2_rows:
            assert r["status"] == "success"
            assert r["superseded_by"] == ""
    finally:
        writer._TAR_CSV = prev


def test_12_write_tar_run_does_not_touch_other_terms(tmp_path: Path) -> None:
    csv_path = tmp_path / "term_analysis_results.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        # Term A run
        suff_a = writer.build_sufficiency_row(
            term_id="BG-A",
            sufficiency_json={}, confidence="high",
            query_index=1, grounded_in_tar_ids=[],
        )
        writer.write_tar_run("BG-A", [], suff_a,
                             executed_by="test", sync_parquet=False)

        # Term B run
        suff_b = writer.build_sufficiency_row(
            term_id="BG-B",
            sufficiency_json={}, confidence="high",
            query_index=1, grounded_in_tar_ids=[],
        )
        writer.write_tar_run("BG-B", [], suff_b,
                             executed_by="test", sync_parquet=False)

        rows = _read_rows(csv_path)
        # Term A row must remain status='success', not superseded by term B.
        a_row = next(r for r in rows if r["term_id"] == "BG-A")
        assert a_row["status"] == "success"
    finally:
        writer._TAR_CSV = prev


# ─── harness ───────────────────────────────────────────────────────────

def _run_standalone() -> int:
    tests = [
        test_01_next_tar_id_empty,
        test_02_next_tar_id_sequential,
        test_03_next_tar_id_ignores_non_tar_ids,
        test_04_build_run_id_shape,
        test_05_build_run_id_preserves_hyphenated_term_id,
        test_06_build_query_row_shape,
        test_07_build_query_row_rejects_invalid_lens,
        test_08_build_sufficiency_row_shape,
        test_09_build_sufficiency_row_rejects_invalid_confidence,
        test_10_write_tar_run_empty_csv,
        test_11_write_tar_run_supersedes_prior_success,
        test_12_write_tar_run_does_not_touch_other_terms,
    ]
    failed = 0
    for t in tests:
        needs_tmp = "tmp_path" in t.__code__.co_varnames
        try:
            if needs_tmp:
                with tempfile.TemporaryDirectory() as td:
                    t(Path(td))
            else:
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
