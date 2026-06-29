"""KI-102 — _tar_writer post-LLM tar_id citation validation tests.

The writer's `write_tar_run` is expected to validate that all tar_ids
cited in `sufficiency_json.lens_consideration[*].tar_ids` resolve to
TAR ids actually allocated by THIS run. On unresolved citations:
sufficiency row gets status='quarantined' + validation_errors_json
populated. Quarantine, not refuse — query rows persist regardless.

Empirical evidence motivating this fix: TARRUN-20260428215336-BG027's
TAR-00018 cited TAR-00234..00247 (215-id offset from the run's actual
TAR-00019..00032 allocations). LLM hallucinations bounded by LLM
consistency, not code-side guarantee — surface via quarantine instead
of silent persistence.

Fixture pattern mirrors tests/test_tar_writer.py: tmp CSV via tmp_path,
_swap_csv to point writer._TAR_CSV at it, sync_parquet=False to skip
side effects.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _tar_writer as writer  # noqa: E402


# ─── helpers (mirrors test_tar_writer.py) ──────────────────────────────

def _swap_csv(tmp_csv: Path):
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


def _make_sufficiency(
    *, lens_to_tar_ids: dict[str, list[str]], term_id: str = "BG-T",
) -> dict:
    """Build a sufficiency dict with the given lens→tar_ids mapping.
    Lenses not listed get decision='skipped' + empty tar_ids."""
    lc: dict = {}
    for lens, tids in lens_to_tar_ids.items():
        lc[lens] = {"decision": "picked", "tar_ids": tids}
    return writer.build_sufficiency_row(
        term_id=term_id,
        sufficiency_json={
            "lens_consideration": lc,
            "declared_sufficient": True,
        },
        confidence="high",
        query_index=99,
        grounded_in_tar_ids=[],
    )


def _make_query(*, term_id: str = "BG-T") -> dict:
    return writer.build_query_row(
        term_id=term_id, analysis_lens="measures_overview",
        stage="framework_floor", query_index=1,
        query_sql="SELECT 1", query_result_json="[]",
        result_row_count=0, interpretation="probe",
        grounded_in_tar_ids=[],
    )


# ─── tests ─────────────────────────────────────────────────────────────

def test_write_sufficiency_with_valid_tar_ids_status_success(
    tmp_path: Path,
) -> None:
    """LLM cites tar_ids that match this run's allocations → status=success
    + validation_errors_json empty."""
    csv_path = tmp_path / "tar.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        # Empty table → first run allocates TAR-00001 (suff) + TAR-00002 (query)
        suff = _make_sufficiency(
            lens_to_tar_ids={"measures_overview": ["TAR-00002"]},
        )
        run_id, new_ids = writer.write_tar_run(
            "BG-T", [_make_query()], suff,
            executed_by="test", sync_parquet=False,
        )
        rows = _read_rows(csv_path)
        suff_row = next(r for r in rows if r["row_type"] == "sufficiency")
        assert suff_row["status"] == "success"
        assert suff_row["validation_errors_json"] in ("", None)
    finally:
        writer._TAR_CSV = prev


def test_write_sufficiency_with_unresolved_tar_ids_status_quarantined(
    tmp_path: Path,
) -> None:
    """LLM cites tar_ids that don't match this run's allocations → status=
    quarantined."""
    csv_path = tmp_path / "tar.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        suff = _make_sufficiency(
            lens_to_tar_ids={"measures_overview": ["TAR-00234"]},
        )
        writer.write_tar_run(
            "BG-T", [_make_query()], suff,
            executed_by="test", sync_parquet=False,
        )
        rows = _read_rows(csv_path)
        suff_row = next(r for r in rows if r["row_type"] == "sufficiency")
        assert suff_row["status"] == "quarantined"
    finally:
        writer._TAR_CSV = prev


def test_quarantined_row_has_validation_errors_json_populated(
    tmp_path: Path,
) -> None:
    """Quarantined sufficiency carries structured diagnostic with the
    expected keys."""
    csv_path = tmp_path / "tar.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        suff = _make_sufficiency(
            lens_to_tar_ids={
                "measures_overview": ["TAR-00234", "TAR-00235"],
                "by_dimension": ["TAR-00002"],  # this one resolves
            },
        )
        writer.write_tar_run(
            "BG-T", [_make_query()], suff,
            executed_by="test", sync_parquet=False,
        )
        rows = _read_rows(csv_path)
        suff_row = next(r for r in rows if r["row_type"] == "sufficiency")
        ve = json.loads(suff_row["validation_errors_json"])
        assert ve["error_type"] == "tar_id_mismatch"
        assert "cited_ids" in ve
        assert "allocated_ids_this_run" in ve
        assert "unresolved_ids" in ve
        assert "lens_breakdown" in ve
        assert set(ve["unresolved_ids"]) == {"TAR-00234", "TAR-00235"}
        assert "TAR-00002" not in ve["unresolved_ids"]
    finally:
        writer._TAR_CSV = prev


def test_quarantined_row_lens_breakdown_correct(tmp_path: Path) -> None:
    """lens_breakdown shows per-lens cited + unresolved sets."""
    csv_path = tmp_path / "tar.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        suff = _make_sufficiency(
            lens_to_tar_ids={
                "measures_overview": ["TAR-00234"],         # bad
                "by_dimension": ["TAR-00002", "TAR-00099"],  # mixed
                "ranking": ["TAR-00002"],                    # good
            },
        )
        writer.write_tar_run(
            "BG-T", [_make_query()], suff,
            executed_by="test", sync_parquet=False,
        )
        rows = _read_rows(csv_path)
        suff_row = next(r for r in rows if r["row_type"] == "sufficiency")
        ve = json.loads(suff_row["validation_errors_json"])
        lb = ve["lens_breakdown"]
        assert lb["measures_overview"]["unresolved"] == ["TAR-00234"]
        assert set(lb["by_dimension"]["unresolved"]) == {"TAR-00099"}
        assert "TAR-00002" in lb["by_dimension"]["cited"]
        assert lb["ranking"]["unresolved"] == []
    finally:
        writer._TAR_CSV = prev


def test_query_rows_persisted_regardless_of_sufficiency_validation(
    tmp_path: Path,
) -> None:
    """Sufficiency quarantine does not affect query rows from the same
    run — they retain status='success'."""
    csv_path = tmp_path / "tar.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        suff = _make_sufficiency(
            lens_to_tar_ids={"measures_overview": ["TAR-00234"]},  # bad
        )
        writer.write_tar_run(
            "BG-T", [_make_query(), _make_query()], suff,
            executed_by="test", sync_parquet=False,
        )
        rows = _read_rows(csv_path)
        query_rows = [r for r in rows if r["row_type"] == "query"]
        assert len(query_rows) == 2
        for qr in query_rows:
            assert qr["status"] == "success"
            assert qr["validation_errors_json"] in ("", None)
    finally:
        writer._TAR_CSV = prev


def test_resolve_against_run_id_not_global_table(tmp_path: Path) -> None:
    """If a TAR id from a PRIOR run exists in the table, citing it from
    THIS run's sufficiency should still be unresolved — the validation
    is scoped to this-run allocations, not the whole table."""
    csv_path = tmp_path / "tar.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        # Run 1 (different term) — allocates TAR-00001 (suff) + TAR-00002 (query)
        suff1 = _make_sufficiency(lens_to_tar_ids={}, term_id="BG-OTHER")
        writer.write_tar_run(
            "BG-OTHER", [_make_query(term_id="BG-OTHER")], suff1,
            executed_by="test", sync_parquet=False,
            run_id="TARRUN-20260101000000-BG-OTHER",
        )
        # Run 2 (BG-T) — would allocate TAR-00003 (suff) + TAR-00004 (query).
        # Cite TAR-00002 (exists in table from run 1, but NOT this run).
        suff2 = _make_sufficiency(
            lens_to_tar_ids={"measures_overview": ["TAR-00002"]},
        )
        run2_id, run2_ids = writer.write_tar_run(
            "BG-T", [_make_query()], suff2,
            executed_by="test", sync_parquet=False,
            run_id="TARRUN-20260101000030-BG-T",
        )
        rows = _read_rows(csv_path)
        run2_suff = next(
            r for r in rows
            if r["row_type"] == "sufficiency" and r["run_id"] == run2_id
        )
        # TAR-00002 exists in table but is from run 1 → quarantine.
        assert run2_suff["status"] == "quarantined"
        ve = json.loads(run2_suff["validation_errors_json"])
        assert "TAR-00002" in ve["unresolved_ids"]
    finally:
        writer._TAR_CSV = prev


def test_build_query_row_with_error_message() -> None:
    """KI-113: error_message parameter populates the row's error_message
    field. Used when _execute_query catches a binder error and the
    runner threads the captured string into build_query_row."""
    row = writer.build_query_row(
        term_id="BG-T", analysis_lens="time_trend",
        stage="framework_floor", query_index=1,
        query_sql='SELECT "BUDAT" FROM main_staging.stg_sap__mseg',
        query_result_json="", result_row_count=0,
        interpretation="trend by month",
        grounded_in_tar_ids=[], status="error",
        error_message=(
            'BinderException: Referenced column "BUDAT" not found in '
            'FROM clause! Candidate bindings: "BWART"'
        ),
    )
    assert row["status"] == "error"
    assert row["error_message"].startswith("BinderException")
    assert "BUDAT" in row["error_message"]


def test_build_query_row_without_error_message_defaults_empty() -> None:
    """KI-113: error_message is optional and defaults to empty string for
    successful queries (the common case)."""
    row = writer.build_query_row(
        term_id="BG-T", analysis_lens="measures_overview",
        stage="framework_floor", query_index=1,
        query_sql="SELECT COUNT(*) FROM main_staging.stg_sap__mseg",
        query_result_json='[{"count": 100}]', result_row_count=1,
        interpretation="row count",
        grounded_in_tar_ids=[],
    )
    assert row["status"] == "success"
    assert row["error_message"] == ""


def test_build_sufficiency_row_error_message_always_empty() -> None:
    """KI-113 OQ-1: error_message exists on sufficiency rows for schema
    symmetry but is always empty in practice (terminal LLM call doesn't
    execute SQL). Field present + defaults to empty."""
    row = writer.build_sufficiency_row(
        term_id="BG-T",
        sufficiency_json={"declared_sufficient": True},
        confidence="high", query_index=99,
        grounded_in_tar_ids=[],
    )
    assert "error_message" in row
    assert row["error_message"] == ""


def test_writer_persists_error_message_field(tmp_path: Path) -> None:
    """KI-113: error_message round-trips through the CSV writer + reader.
    A query row written with an error_message reads back with that exact
    string in the persisted CSV column."""
    csv_path = tmp_path / "tar.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        err_str = (
            "BinderException: Referenced column \"BUDAT\" not found in "
            "FROM clause! Candidate bindings: \"BWART\""
        )
        bad_query = writer.build_query_row(
            term_id="BG-T", analysis_lens="time_trend",
            stage="framework_floor", query_index=1,
            query_sql='SELECT "BUDAT" FROM main_staging.stg_sap__mseg',
            query_result_json="", result_row_count=0,
            interpretation="trend by month",
            grounded_in_tar_ids=[], status="error",
            error_message=err_str,
        )
        good_query = writer.build_query_row(
            term_id="BG-T", analysis_lens="measures_overview",
            stage="framework_floor", query_index=2,
            query_sql="SELECT COUNT(*) FROM main_staging.stg_sap__mseg",
            query_result_json='[{"count": 100}]', result_row_count=1,
            interpretation="row count",
            grounded_in_tar_ids=[],
        )
        suff = _make_sufficiency(
            lens_to_tar_ids={"measures_overview": ["TAR-00003"]},
        )
        writer.write_tar_run(
            "BG-T", [bad_query, good_query], suff,
            executed_by="test", sync_parquet=False,
        )
        rows = _read_rows(csv_path)
        bad_row = next(r for r in rows
                       if r["row_type"] == "query"
                       and r["status"] == "error")
        good_row = next(r for r in rows
                        if r["row_type"] == "query"
                        and r["status"] == "success")
        assert bad_row["error_message"] == err_str
        assert good_row["error_message"] in ("", None)
    finally:
        writer._TAR_CSV = prev


def test_validate_runner_constructed_sufficiency_passes(tmp_path: Path) -> None:
    """Phase-restructure path: when the runner constructs sufficiency_json
    with tar_ids drawn exclusively from this run's allocations, the
    writer's validator passes vacuously and status='success'.

    Post-refactor the runner emits valid ids by construction, so this
    branch is the production case; quarantine paths above stay as
    defense-in-depth (e.g. for an implementation bug in construction).
    """
    csv_path = tmp_path / "tar.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        # Empty table → run allocates TAR-00001 (suff) + TAR-00002 (query).
        # Runner-constructed sufficiency cites only this run's query id.
        suff = _make_sufficiency(
            lens_to_tar_ids={
                "measures_overview": ["TAR-00002"],
                # Other picked lenses with empty tar_ids (no queries
                # for those lenses this run, no framework_floor cites).
                "by_dimension": [],
            },
        )
        writer.write_tar_run(
            "BG-T", [_make_query()], suff,
            executed_by="test", sync_parquet=False,
        )
        rows = _read_rows(csv_path)
        suff_row = next(r for r in rows if r["row_type"] == "sufficiency")
        assert suff_row["status"] == "success"
        assert suff_row["validation_errors_json"] in ("", None)
    finally:
        writer._TAR_CSV = prev


def test_run_id_filter_excludes_other_runs_tars(tmp_path: Path) -> None:
    """Citing the CURRENT run's just-allocated query id resolves
    cleanly even with prior-run rows in the table."""
    csv_path = tmp_path / "tar.csv"
    _write_empty_csv(csv_path)
    prev = _swap_csv(csv_path)
    try:
        # Run 1 (different term) — fills the table with TAR-00001 + TAR-00002
        suff1 = _make_sufficiency(lens_to_tar_ids={}, term_id="BG-OTHER")
        writer.write_tar_run(
            "BG-OTHER", [_make_query(term_id="BG-OTHER")], suff1,
            executed_by="test", sync_parquet=False,
            run_id="TARRUN-20260101000000-BG-OTHER",
        )
        # Run 2 (BG-T) — allocates TAR-00003 (suff) + TAR-00004 (query).
        # Cite TAR-00004 (this run's query). Should resolve.
        suff2 = _make_sufficiency(
            lens_to_tar_ids={"measures_overview": ["TAR-00004"]},
        )
        run2_id, _ = writer.write_tar_run(
            "BG-T", [_make_query()], suff2,
            executed_by="test", sync_parquet=False,
            run_id="TARRUN-20260101000030-BG-T",
        )
        rows = _read_rows(csv_path)
        run2_suff = next(
            r for r in rows
            if r["row_type"] == "sufficiency" and r["run_id"] == run2_id
        )
        assert run2_suff["status"] == "success"
        assert run2_suff["validation_errors_json"] in ("", None)
    finally:
        writer._TAR_CSV = prev


# ─── KI-111 — validate_query_grounded_in_tar_ids ───────────────────────


def _qrow(query_index: int, grounded: list[str], lens: str = "framework_floor",
          stage: str = "framework_floor") -> dict:
    return {
        "query_index": query_index,
        "analysis_lens": lens,
        "stage": stage,
        "grounded_in_tar_ids": grounded,
    }


def test_validate_grounded_empty_rows_passes() -> None:
    """No query rows -> trivially valid."""
    ok, errors = writer.validate_query_grounded_in_tar_ids([], ["TAR-00001"])
    assert ok is True
    assert errors is None


def test_validate_grounded_all_empty_citations_passes() -> None:
    """Query rows with no citations -> trivially valid regardless of bundle."""
    rows = [_qrow(1, []), _qrow(2, [])]
    ok, errors = writer.validate_query_grounded_in_tar_ids(rows, ["TAR-00001"])
    assert ok is True
    assert errors is None


def test_validate_grounded_all_cited_in_bundle_passes() -> None:
    """All cited ids are in the bundle -> valid."""
    rows = [
        _qrow(1, ["TAR-00001", "TAR-00002"]),
        _qrow(2, ["TAR-00002"]),
    ]
    ok, errors = writer.validate_query_grounded_in_tar_ids(
        rows, ["TAR-00001", "TAR-00002", "TAR-00003"],
    )
    assert ok is True
    assert errors is None


def test_validate_grounded_partial_hallucination_fails() -> None:
    """Some cited ids resolve, others don't -> reports unresolved per row."""
    rows = [
        _qrow(1, ["TAR-00001", "TAR-99999"], lens="ranking", stage="reflection"),
        _qrow(2, ["TAR-00002"]),
    ]
    ok, errors = writer.validate_query_grounded_in_tar_ids(
        rows, ["TAR-00001", "TAR-00002"],
    )
    assert ok is False
    assert errors is not None
    assert errors["error_type"] == "grounded_in_tar_id_mismatch"
    assert errors["candidate_prior_tar_ids"] == ["TAR-00001", "TAR-00002"]
    # Only row 1 has unresolved; row 2 is fully resolved
    assert len(errors["violations"]) == 1
    v = errors["violations"][0]
    assert v["query_index"] == 1
    assert v["analysis_lens"] == "ranking"
    assert v["stage"] == "reflection"
    assert v["unresolved"] == ["TAR-99999"]


def test_validate_grounded_empty_bundle_with_citations_fails() -> None:
    """Bundle is empty (BG029 pre-KI-110 case) but LLM cited ids -> all unresolved."""
    rows = [_qrow(1, ["TAR-00234", "TAR-00247"])]
    ok, errors = writer.validate_query_grounded_in_tar_ids(rows, [])
    assert ok is False
    assert errors["candidate_prior_tar_ids"] == []
    assert errors["violations"][0]["unresolved"] == ["TAR-00234", "TAR-00247"]


def test_validate_grounded_non_string_citations_skipped_safely() -> None:
    """Non-string entries in grounded_in_tar_ids are dropped before subset check."""
    rows = [_qrow(1, ["TAR-00001", None, 42, "TAR-00002"])]
    ok, errors = writer.validate_query_grounded_in_tar_ids(
        rows, ["TAR-00001", "TAR-00002"],
    )
    assert ok is True
    assert errors is None
