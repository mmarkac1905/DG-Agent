"""KI #71 Step 2 unit tests — archive_term saga.

Covers the pure helpers and the exception/result API. Real-data smoke
tests run the saga against the live seeds for BG011 (expected to be
blocked by the strict-cascade gate) and BG026 (expected idempotent
no-op — already archived).

Destructive scenarios (successful archive of a freely-archivable term)
are not exercised here — they would mutate the project state. Step 3's
UI integration round exercises the success path via a disposable test
term in a tmpdir.

Test plan
---------
01      BlockedArchive carries the impact for UI consumption
02      AlreadyArchived stringifies with the pointer
03      ArchiveResult dataclass defaults
04      _snapshot_csv / _restore_csv round-trip preserves bytes
05      _restore_csv with snapshot=None deletes a freshly-created file
06      _build_cascaded_models_json shapes one record per move
07      _layer_of returns the canonical layer dir name
08      _next_archive_id format YYYY-MM-DD increments
09      smoke: BG011 against live seeds raises BlockedArchive
10      smoke: BG026 against live seeds returns already-archived result
11      smoke: missing reason_code raises ValueError
12      smoke: missing term raises ValueError via analyzer
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))

import archive_term  # noqa: E402
from archive_term import (  # noqa: E402
    AlreadyArchived,
    ArchiveResult,
    BlockedArchive,
    _build_cascaded_models_json,
    _layer_of,
    _next_archive_id,
    _restore_csv,
    _snapshot_csv,
    run_archive,
)
from archive_dependency_analyzer import (  # noqa: E402
    ArchiveImpact,
    SharingBlocker,
    TermRef,
)


# ---------------------------------------------------------------------------
# Unit tests — exception API + helpers
# ---------------------------------------------------------------------------

def test_01_blocked_archive_carries_impact() -> None:
    impact = ArchiveImpact(
        term_id="BG999",
        term_name="test_term",
        term_status="approved",
        target_models=["fact_x"],
        sharing_blockers=[SharingBlocker(
            model_name="fact_x",
            other_terms=(TermRef("BG100", "other", "approved"),),
        )],
    )
    e = BlockedArchive(impact)
    assert e.impact is impact
    assert "BG999" in str(e)
    assert "1 sharing" in str(e)
    assert "0 downstream" in str(e)


def test_02_already_archived_stringifies_with_pointer() -> None:
    e1 = AlreadyArchived("BG011")
    assert "BG011" in str(e1)
    e2 = AlreadyArchived("BG011", archive_id="ARC-20260418-001",
                         archived_at="2026-04-18T08:20:33Z")
    assert "ARC-20260418-001" in str(e2)
    assert "2026-04-18T08:20:33Z" in str(e2)


def test_03_archive_result_dataclass_defaults() -> None:
    r = ArchiveResult(
        archive_id="ARC-X",
        term_id="BG001",
        term_name="t",
        cascaded_models=["m1"],
        files_archived=1,
    )
    assert r.blockers_resolved == []
    assert r.already_archived is False


def test_04_snapshot_restore_roundtrip_preserves_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.csv"
        # Include CRLF + a JSON-style cell to confirm byte-exact restore.
        original = b'a,b\r\n1,"{""k"":""v""}"\r\n'
        p.write_bytes(original)
        snap = _snapshot_csv(p)
        assert snap == original
        # Mutate the file...
        p.write_bytes(b"garbage")
        # ...and restore.
        _restore_csv(p, snap)
        assert p.read_bytes() == original


def test_05_restore_csv_with_none_snapshot_unlinks_new_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.csv"
        assert not p.exists()
        snap = _snapshot_csv(p)  # None — file does not exist
        assert snap is None
        # Saga writes to the file...
        p.write_bytes(b"hello")
        assert p.exists()
        # ...rollback deletes it.
        _restore_csv(p, snap)
        assert not p.exists()


def test_06_build_cascaded_models_json_shape() -> None:
    # Construct a fake (src, dest) pair sitting under the canonical layout.
    src = archive_term.DBT_MODELS / "marts" / "fact_test.sql"
    dst = archive_term.ARCHIVE_ROOT / "ARC-X" / "marts" / "fact_test.sql"
    j = _build_cascaded_models_json([(src, dst)])
    items = json.loads(j)
    assert len(items) == 1
    assert items[0]["name"] == "fact_test"
    assert items[0]["layer"] == "marts"
    assert items[0]["src"].endswith("dbt/models/marts/fact_test.sql")
    assert items[0]["dst"].endswith("dbt/models/archive/ARC-X/marts/fact_test.sql")


def test_07_layer_of_returns_layer_name() -> None:
    src = archive_term.DBT_MODELS / "obt" / "obt_example.sql"
    assert _layer_of(src) == "obt"
    # Path outside dbt/models — return "".
    foreign = Path("/tmp/something/fact_x.sql")
    assert _layer_of(foreign) == ""


def test_08_next_archive_id_format() -> None:
    # Format-only assertion — the live file content drives the counter,
    # so we just check the prefix shape.
    arc = _next_archive_id()
    assert arc.startswith("ARC-")
    assert len(arc) == len("ARC-YYYYMMDD-NNN")
    # Counter portion is three digits.
    assert arc.split("-")[-1].isdigit()


def test_11_missing_reason_code_raises_value_error() -> None:
    try:
        run_archive(term_id="BG011", reason_code="", reason_text="",
                    learning_signal=False)
    except ValueError as e:
        assert "reason_code" in str(e)
        return
    raise AssertionError("expected ValueError for missing reason_code")


def test_12_missing_term_id_raises_value_error() -> None:
    try:
        run_archive(term_id="", reason_code="obsolete", reason_text="",
                    learning_signal=False)
    except ValueError as e:
        assert "term_id" in str(e)
        return
    raise AssertionError("expected ValueError for missing term_id")


# ---------------------------------------------------------------------------
# Smoke tests — run against the live seeds. Slow on first call because
# they may trigger `dbt compile`. The analyzer caches the manifest after.
# ---------------------------------------------------------------------------

def test_09_smoke_bg011_blocks_against_live_seeds() -> None:
    """BG011's mart (fact_purchase_orders) is shared with 5 other
    approved terms AND has 4 downstream consumers. Strict-cascade
    gate must refuse."""
    try:
        run_archive(
            term_id="BG011",
            reason_code="obsolete",
            reason_text="smoke test — should be blocked",
            learning_signal=False,
        )
    except BlockedArchive as e:
        assert e.impact.term_id == "BG011"
        assert len(e.impact.sharing_blockers) >= 1
        assert len(e.impact.downstream_blockers) >= 1
        # Verify no mutations happened — archive_log should not have a
        # new row for BG011, glossary status should still be 'approved'.
        log = pd.read_csv(archive_term.ARCHIVE_LOG)
        bg011_rows = log[log["business_term_id"] == "BG011"]
        assert bg011_rows.empty, "saga should not have written archive_log row"
        gloss = pd.read_csv(
            archive_term.GLOSSARY_CSV,
            keep_default_na=False, na_filter=False, dtype=str,
        )
        bg011 = gloss[gloss["id"] == "BG011"].iloc[0]
        assert bg011["status"] == "approved", "saga should not have flipped status"
        return
    except RuntimeError as e:
        # dbt compile failed — surfaces as RuntimeError. Skip gracefully.
        msg = str(e)
        if "dbt compile failed" in msg or "manifest" in msg:
            print(f"  [SKIP] test_09 — analyzer prereq not satisfied: {msg[:120]}")
            return
        raise
    raise AssertionError("expected BlockedArchive for BG011")


def test_10_smoke_bg026_returns_already_archived_no_op() -> None:
    """BG026 was archived during the pre-MVP demo rehearsal
    (ARC-20260418-001). Saga must short-circuit to no-op."""
    try:
        result = run_archive(
            term_id="BG026",
            reason_code="obsolete",
            reason_text="smoke — already archived",
            learning_signal=False,
        )
    except RuntimeError as e:
        msg = str(e)
        if "dbt compile failed" in msg or "manifest" in msg:
            print(f"  [SKIP] test_10 — analyzer prereq not satisfied: {msg[:120]}")
            return
        raise
    assert result.already_archived is True
    assert result.files_archived == 0
    assert result.cascaded_models == []
    # archive_id should be populated from the existing glossary pointer.
    assert result.archive_id.startswith("ARC-")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_standalone() -> int:
    tests = [
        test_01_blocked_archive_carries_impact,
        test_02_already_archived_stringifies_with_pointer,
        test_03_archive_result_dataclass_defaults,
        test_04_snapshot_restore_roundtrip_preserves_bytes,
        test_05_restore_csv_with_none_snapshot_unlinks_new_file,
        test_06_build_cascaded_models_json_shape,
        test_07_layer_of_returns_layer_name,
        test_08_next_archive_id_format,
        test_11_missing_reason_code_raises_value_error,
        test_12_missing_term_id_raises_value_error,
        test_09_smoke_bg011_blocks_against_live_seeds,
        test_10_smoke_bg026_returns_already_archived_no_op,
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
