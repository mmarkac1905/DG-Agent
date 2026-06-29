"""Piece 9 Stage B unit tests for _stage_a_blocker_loader.

15 tests covering eligibility filters, blocker-resolution routing, truncation,
tolerance paths, and render output. Fixtures use temporary CSV files
(`tmp_path`) — no live seed touched, no LLM calls, no DB dependency.

Run standalone (no pytest):
  python tests/test_stage_a_blocker_loader.py

Or under pytest:
  pytest tests/test_stage_a_blocker_loader.py

Exit 0 on all-pass, 1 on any failure.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _stage_a_blocker_loader as loader  # noqa: E402


# ─── Fixture helpers ──────────────────────────────────────────────────

_BG_HEADERS = [
    "id", "term_name", "display_name", "definition", "unit", "grain",
    "domain", "notes", "business_join_description",
    "business_filter_description", "status", "archive_id", "archived_at_utc",
    "archived_reason_code", "archived_reason_text",
    "scope_derivation_history_json",
]


def _make_blocker(
    *,
    resolves_in: str | None = "domain_eda",
    tables: list[str] | None = None,
    short_title: str = "Blocker title",
    what_it_means: str = "means text",
    what_llm_needs: str = "needs text",
    resolves_via: str = "via text",
    user_action_now: str = "action text",
    btype: str = "scope_concern",
) -> dict:
    out = {
        "type": btype,
        "tables": tables if tables is not None else ["mseg"],
        "short_title": short_title,
        "what_it_means": what_it_means,
        "what_llm_needs": what_llm_needs,
        "resolves_via": resolves_via,
        "user_action_now": user_action_now,
    }
    # resolves_in is explicitly set as None vs missing to cover both paths.
    if resolves_in is not None:
        out["resolves_in"] = resolves_in
    return out


def _make_iteration(
    blockers: list[dict],
    *,
    analyst_action: str | None = "confirmed",
    iter_num: int = 1,
    proposed_tables: list[str] | None = None,
) -> dict:
    return {
        "iter_num": iter_num,
        "mode": "propose",
        "timestamp": "2026-04-22T10:00:00",
        "analyst_action": analyst_action,
        "validation_issues": [],
        "llm_response": {
            "proposed_tables": proposed_tables or ["mseg"],
            "blockers": blockers,
            "attestation_echo": {},
            "confidence": "high",
            "confidence_rationale": "test",
        },
        "usage": {},
    }


def _make_history(
    iterations: list[dict],
    *,
    final_iter_num: int | None = None,
    confirmed_at_utc: str | None = None,
) -> dict:
    h: dict = {"iterations": iterations}
    if final_iter_num is not None:
        h["final_iter_num"] = final_iter_num
    if confirmed_at_utc is not None:
        h["confirmed_at_utc"] = confirmed_at_utc
    return h


def _write_bg_csv(
    path: Path,
    rows: list[dict],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_BG_HEADERS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in _BG_HEADERS})


def _bg_row(
    id_: str,
    status: str,
    history: dict | str,
    *,
    term_name: str = "test_term",
) -> dict:
    if isinstance(history, dict):
        history_json = json.dumps(history, ensure_ascii=False)
    else:
        history_json = history
    return {
        "id": id_,
        "term_name": term_name,
        "display_name": term_name,
        "definition": "",
        "unit": "",
        "grain": "",
        "domain": "",
        "notes": "",
        "business_join_description": "",
        "business_filter_description": "",
        "status": status,
        "archive_id": "",
        "archived_at_utc": "",
        "archived_reason_code": "",
        "archived_reason_text": "",
        "scope_derivation_history_json": history_json,
    }


def _swap_bg_csv(path: Path):
    """Context manager-like: point loader._BG_CSV at path, return previous."""
    prev = loader._BG_CSV
    loader._BG_CSV = path
    return prev


# ─── Tests ─────────────────────────────────────────────────────────────

def test_01_empty_history(tmp_path: Path) -> None:
    """Term with scope_derivation_history_json='{}' -> returns ([], 0)."""
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG001", "scope_confirmed", "{}")])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        assert entries == [], f"expected empty, got {entries!r}"
        assert trunc == 0, f"expected 0 truncation, got {trunc}"
    finally:
        loader._BG_CSV = prev


def test_02_draft_term_excluded(tmp_path: Path) -> None:
    """Draft status excluded by eligibility filter."""
    history = _make_history(
        [_make_iteration([_make_blocker()])],
        final_iter_num=1,
        confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG001", "draft", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        assert entries == []
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_03_pre_augmentation_blocker_skipped(tmp_path: Path) -> None:
    """BG027-shape: confirmed iteration, blocker missing resolves_in -> skip."""
    blocker = _make_blocker(resolves_in=None)  # explicitly absent
    history = _make_history(
        [_make_iteration([blocker])],
        final_iter_num=1,
        confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG027", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        assert entries == []
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_04_domain_eda_matching_table(tmp_path: Path) -> None:
    """resolves_in=domain_eda + table in tables -> single entry returned."""
    blocker = _make_blocker(resolves_in="domain_eda", tables=["mseg"])
    history = _make_history(
        [_make_iteration([blocker])],
        final_iter_num=1,
        confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG100", "scope_confirmed", history,
                                term_name="t1")])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        assert len(entries) == 1, f"expected 1 entry, got {len(entries)}"
        assert entries[0]["term_id"] == "BG100"
        assert entries[0]["term_name"] == "t1"
        assert entries[0]["blocker"]["resolves_in"] == "domain_eda"
        assert entries[0]["blocker"]["short_title"] == "Blocker title"
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_05_domain_eda_wrong_table(tmp_path: Path) -> None:
    """resolves_in=domain_eda but table not in tables list -> exclude."""
    blocker = _make_blocker(resolves_in="domain_eda", tables=["ekpo"])
    history = _make_history(
        [_make_iteration([blocker])],
        final_iter_num=1,
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG100", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        assert entries == []
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_06_term_eda_excluded(tmp_path: Path) -> None:
    """resolves_in=term_eda -> Stage B filter excludes."""
    blocker = _make_blocker(resolves_in="term_eda", tables=["mseg"])
    history = _make_history(
        [_make_iteration([blocker])],
        final_iter_num=1,
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG100", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        assert entries == []
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_07_multiple_terms_same_table(tmp_path: Path) -> None:
    """Two terms each with an MSEG domain_eda blocker -> two entries, no merge."""
    b1 = _make_blocker(resolves_in="domain_eda", tables=["mseg"],
                       short_title="BWART semantics")
    b2 = _make_blocker(resolves_in="domain_eda", tables=["mseg"],
                       short_title="Null-rate baseline")
    h1 = _make_history([_make_iteration([b1])], final_iter_num=1,
                       confirmed_at_utc="2026-04-22T10:00:00")
    h2 = _make_history([_make_iteration([b2])], final_iter_num=1,
                       confirmed_at_utc="2026-04-22T11:00:00")
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [
        _bg_row("BG100", "scope_confirmed", h1, term_name="term_a"),
        _bg_row("BG101", "scope_confirmed", h2, term_name="term_b"),
    ])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        assert len(entries) == 2, f"expected 2 entries, got {len(entries)}"
        term_ids = {e["term_id"] for e in entries}
        assert term_ids == {"BG100", "BG101"}
        titles = {e["blocker"]["short_title"] for e in entries}
        assert titles == {"BWART semantics", "Null-rate baseline"}
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_08_malformed_json_tolerated(tmp_path: Path) -> None:
    """Malformed history JSON -> skip row, continue scanning."""
    good_blocker = _make_blocker(resolves_in="domain_eda", tables=["mseg"])
    good_history = _make_history(
        [_make_iteration([good_blocker])],
        final_iter_num=1,
        confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [
        _bg_row("BG-BAD", "scope_confirmed", "{not valid json"),
        _bg_row("BG-GOOD", "scope_confirmed", good_history),
    ])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        assert len(entries) == 1
        assert entries[0]["term_id"] == "BG-GOOD"
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_09_analyst_action_resolves_iteration(tmp_path: Path) -> None:
    """Three iterations; iter 2 marked 'confirmed' with superseded siblings."""
    b_stale = _make_blocker(resolves_in="domain_eda", tables=["mseg"],
                            short_title="STALE — from iter 1")
    b_conf = _make_blocker(resolves_in="domain_eda", tables=["mseg"],
                           short_title="CONFIRMED — from iter 2")
    b_later = _make_blocker(resolves_in="domain_eda", tables=["mseg"],
                            short_title="SUPERSEDED-LATER — from iter 3")
    iter1 = _make_iteration([b_stale], analyst_action="superseded", iter_num=1)
    iter2 = _make_iteration([b_conf], analyst_action="confirmed", iter_num=2)
    iter3 = _make_iteration([b_later], analyst_action="superseded", iter_num=3)
    history = _make_history(
        [iter1, iter2, iter3],
        final_iter_num=2,
        confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG200", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        assert len(entries) == 1
        assert entries[0]["blocker"]["short_title"] == "CONFIRMED — from iter 2"
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_10_final_iter_num_fallback(tmp_path: Path) -> None:
    """No analyst_action='confirmed'; final_iter_num resolves correctly."""
    b_late = _make_blocker(resolves_in="domain_eda", tables=["mseg"],
                           short_title="FROM FINAL ITER 2")
    iter1 = _make_iteration(
        [_make_blocker(resolves_in="term_eda", tables=["mseg"])],
        analyst_action=None, iter_num=1)
    iter2 = _make_iteration([b_late], analyst_action=None, iter_num=2)
    history = _make_history(
        [iter1, iter2],
        final_iter_num=2,
        confirmed_at_utc="2026-04-22T11:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG300", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        assert len(entries) == 1
        assert entries[0]["blocker"]["short_title"] == "FROM FINAL ITER 2"
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_11_render_empty() -> None:
    """render_analyst_concerns_block([]) -> empty string."""
    out = loader.render_analyst_concerns_block([])
    assert out == "", f"expected empty string, got {out!r}"


def test_12_render_non_empty_shape() -> None:
    """Non-empty list renders header + all 6 augmentation fields.

    KI-115 closure: the historical `blockers_addressed` directive was
    removed; this test now also asserts (negatively) that the dead
    directive text does not sneak back in.
    """
    entry = {
        "term_id": "BG-X",
        "term_name": "my_term",
        "blocker": _make_blocker(
            resolves_in="domain_eda",
            tables=["mseg"],
            short_title="Title XYZ",
            what_it_means="Meaning text",
            what_llm_needs="Needs text",
            resolves_via="Via text",
            user_action_now="Action text",
            btype="scope_concern",
        ),
    }
    out = loader.render_analyst_concerns_block([entry])
    # Section header
    assert "### Analyst concerns to address in this analysis" in out
    # Framing sentence with term count
    assert "1 downstream business term(s)" in out
    # Per-term attribution
    assert "BG-X (my_term)" in out
    # All 6 augmentation fields present
    assert "Title XYZ" in out
    assert "Meaning text" in out
    assert "Needs text" in out
    assert "Via text" in out
    assert "Action text" in out
    # Blocker type + tables
    assert "scope_concern" in out
    assert "mseg" in out
    # resolves_in stage label
    assert "domain_eda" in out
    # KI-115: dead `blockers_addressed` directive must NOT be present
    assert "blockers_addressed" not in out, (
        "KI-115 regression: dead `blockers_addressed` directive "
        "reappeared in rendered concerns block"
    )
    assert "addressable from this table's data alone" not in out, (
        "KI-115 regression: dead closing directive reappeared"
    )


def test_13_truncation_cap() -> None:
    """15 matching blockers -> returns 10 entries + truncation 5, sorted by
    confirmed_at_utc DESC."""
    rows = []
    # 15 terms, each with one matching blocker; confirmed_at_utc ranges so
    # that we can assert the top 10 are the most recent.
    for i in range(15):
        b = _make_blocker(
            resolves_in="domain_eda", tables=["mseg"],
            short_title=f"Blocker {i:02d}",
        )
        # Higher i = more recent timestamp.
        ts = f"2026-04-22T{10 + (i % 10):02d}:{i:02d}:00"
        history = _make_history(
            [_make_iteration([b])],
            final_iter_num=1,
            confirmed_at_utc=ts,
        )
        rows.append((f"BG{i:03d}", "scope_confirmed", history, ts, i))

    # Sort rows by timestamp so we can assert the cap's kept set matches.
    rows_sorted_desc = sorted(rows, key=lambda r: r[3], reverse=True)
    top10_indexes = {r[4] for r in rows_sorted_desc[:10]}

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        bg = Path(td) / "business_glossary.csv"
        _write_bg_csv(
            bg,
            [_bg_row(r[0], r[1], r[2]) for r in rows],
        )
        prev = _swap_bg_csv(bg)
        try:
            entries, trunc = loader.load_blockers_for_table(
                "mseg", max_blockers=10)
            assert len(entries) == 10, f"expected 10, got {len(entries)}"
            assert trunc == 5, f"expected truncation 5, got {trunc}"
            kept_titles = {e["blocker"]["short_title"] for e in entries}
            expected_titles = {f"Blocker {i:02d}" for i in top10_indexes}
            assert kept_titles == expected_titles, (
                f"kept={kept_titles} expected={expected_titles}"
            )
        finally:
            loader._BG_CSV = prev


def test_14_truncation_note_rendered() -> None:
    """render with truncation_count > 0 includes the note."""
    entry = {
        "term_id": "BG-X",
        "term_name": "t",
        "blocker": _make_blocker(),
    }
    out = loader.render_analyst_concerns_block([entry], truncation_count=5)
    assert "5 additional blocker(s) truncated" in out
    assert "Showing the 1 most recently confirmed" in out


def test_15_final_iter_num_out_of_range(tmp_path: Path) -> None:
    """History with final_iter_num=5 but only 2 iterations -> skip term,
    does not raise IndexError."""
    iter1 = _make_iteration(
        [_make_blocker(resolves_in="domain_eda", tables=["mseg"])],
        analyst_action=None, iter_num=1)
    iter2 = _make_iteration(
        [_make_blocker(resolves_in="domain_eda", tables=["mseg"])],
        analyst_action=None, iter_num=2)
    # final_iter_num points past end.
    history = _make_history(
        [iter1, iter2],
        final_iter_num=5,
        confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG-CORRUPT", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_table("mseg")
        # No exception; term skipped because no confirmed iteration resolves.
        assert entries == [], f"expected empty, got {entries!r}"
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


# ─── C4 — per-term loader tests ───────────────────────────────────────

def test_16_load_for_term_no_history_returns_empty(tmp_path: Path) -> None:
    """Term with empty scope_derivation_history_json -> ([], 0)."""
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG500", "scope_confirmed", "{}")])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_term("BG500")
        assert entries == []
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_17_load_for_term_draft_status_excluded(tmp_path: Path) -> None:
    """Draft status excluded by eligibility filter (mirrors per-table)."""
    history = _make_history(
        [_make_iteration([_make_blocker(resolves_in="term_eda")])],
        final_iter_num=1, confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG501", "draft", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_term("BG501")
        assert entries == []
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_18_load_for_term_confirmed_returns_all_blockers(tmp_path: Path) -> None:
    """Confirmed iteration with mixed-routing blockers -> all returned,
    in emission order, with iter_num + blocker_index attribution."""
    b0 = _make_blocker(resolves_in="term_eda", tables=["mseg"],
                       short_title="BWART semantics")
    b1 = _make_blocker(resolves_in="domain_eda", tables=["equi"],
                       short_title="EQUI temporal")
    b2 = _make_blocker(resolves_in="analyst_decision", tables=[],
                       short_title="vendor onboarding cutoff")
    history = _make_history(
        [_make_iteration([b0, b1, b2])],
        final_iter_num=1, confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG600", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_term("BG600")
        assert len(entries) == 3
        assert trunc == 0
        # Order preserved.
        assert entries[0]["blocker_index"] == 0
        assert entries[0]["iter_num"] == 1
        assert entries[0]["blocker"]["short_title"] == "BWART semantics"
        assert entries[1]["blocker_index"] == 1
        assert entries[1]["blocker"]["resolves_in"] == "domain_eda"
        assert entries[2]["blocker"]["resolves_in"] == "analyst_decision"
    finally:
        loader._BG_CSV = prev


def test_19_load_for_term_no_resolves_in_filter(tmp_path: Path) -> None:
    """All five resolves_in routing values surface — no filter applied."""
    routings = [
        "domain_eda", "term_eda", "analyst_decision",
        "ingestion_required", "source_diagnostic_required",
    ]
    blockers = [
        _make_blocker(resolves_in=r, tables=[], short_title=f"r-{r}")
        for r in routings
    ]
    history = _make_history(
        [_make_iteration(blockers)],
        final_iter_num=1, confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG601", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_term("BG601")
        assert len(entries) == 5
        seen = {e["blocker"]["resolves_in"] for e in entries}
        assert seen == set(routings)
    finally:
        loader._BG_CSV = prev


def test_20_load_for_term_id_not_in_glossary_returns_empty(
    tmp_path: Path,
) -> None:
    """Term ID absent from business_glossary -> ([], 0)."""
    history = _make_history(
        [_make_iteration([_make_blocker()])],
        final_iter_num=1, confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG602", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_term("BG-NOT-FOUND")
        assert entries == []
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_21_load_for_term_malformed_json_tolerated(tmp_path: Path) -> None:
    """Malformed history JSON -> ([], 0) without raising."""
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG603", "scope_confirmed", "{not valid")])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_term("BG603")
        assert entries == []
        assert trunc == 0
    finally:
        loader._BG_CSV = prev


def test_22_load_for_term_truncation_cap(tmp_path: Path) -> None:
    """15 blockers in one iteration with max_blockers=10 -> 10 + trunc=5."""
    blockers = [
        _make_blocker(resolves_in="term_eda", tables=[],
                      short_title=f"Blocker {i:02d}")
        for i in range(15)
    ]
    history = _make_history(
        [_make_iteration(blockers)],
        final_iter_num=1, confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG604", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_term(
            "BG604", max_blockers=10)
        assert len(entries) == 10
        assert trunc == 5
        # Emission order preserved within the cap.
        assert entries[0]["blocker"]["short_title"] == "Blocker 00"
        assert entries[9]["blocker"]["short_title"] == "Blocker 09"
    finally:
        loader._BG_CSV = prev


def test_23_load_for_term_pre_augmentation_blocker_kept(tmp_path: Path) -> None:
    """Pre-augmentation blockers (no resolves_in) are KEPT — unlike
    load_blockers_for_table which filters them out (per Step 0 §SS-4).
    """
    pre_aug = _make_blocker(resolves_in=None, tables=["mseg"],
                            short_title="legacy concern")
    history = _make_history(
        [_make_iteration([pre_aug])],
        final_iter_num=1, confirmed_at_utc="2026-04-22T10:00:00",
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG605", "scope_confirmed", history)])
    prev = _swap_bg_csv(bg)
    try:
        entries, trunc = loader.load_blockers_for_term("BG605")
        assert len(entries) == 1
        assert "resolves_in" not in entries[0]["blocker"]
    finally:
        loader._BG_CSV = prev


def test_24_render_stage_a_blockers_section_empty() -> None:
    """render_stage_a_blockers_section([]) -> empty string."""
    assert loader.render_stage_a_blockers_section([]) == ""


def test_25_render_stage_a_blockers_section_full_shape() -> None:
    """Full schema render: header, ID, routing tag, all 6 detail fields,
    optional truncation note. Pre-augmentation routing renders as
    '(unset (pre-augmentation))'."""
    e1 = {
        "iter_num": 1,
        "blocker_index": 0,
        "blocker": _make_blocker(
            resolves_in="term_eda", tables=["mseg"],
            short_title="Title A",
            what_it_means="Means A.",
            what_llm_needs="Needs A.",
            resolves_via="Via A.",
            user_action_now="Action A.",
            btype="scope_concern",
        ),
    }
    e2 = {
        "iter_num": 1,
        "blocker_index": 1,
        "blocker": _make_blocker(
            resolves_in=None, tables=["equi"],
            short_title="Legacy B",
            btype="missing_domain_eda",
        ),
    }
    out = loader.render_stage_a_blockers_section([e1, e2], truncation_count=3)
    assert "## Stage A blockers" in out
    # IDs.
    assert "iter1.b0" in out
    assert "iter1.b1" in out
    # Routing tags (rendered + pre-augmentation fallback).
    assert "term_eda" in out
    assert "unset (pre-augmentation)" in out
    # Type tags.
    assert "scope_concern" in out
    assert "missing_domain_eda" in out
    # All 6 detail fields surfaced for the first blocker.
    assert "Tables: mseg" in out
    assert "Title A" in out
    assert "Means A." in out
    assert "Needs A." in out
    assert "Via A." in out
    assert "Action A." in out
    # Truncation note.
    assert "3 additional blocker(s) truncated" in out


# ─── KI-114 — filter_resolved kwarg tests ─────────────────────────────

def _patched_resolved_keys(monkeypatch_target: set[tuple[str, str]]):
    """Replace _load_resolved_blocker_keys with a stub returning the
    given set. Returns the original function for restoration."""
    original = loader._load_resolved_blocker_keys
    loader._load_resolved_blocker_keys = lambda conn=None: monkeypatch_target
    return original


def test_26_filter_resolved_excludes_resolved_blocker(tmp_path: Path) -> None:
    """KI-114: filter_resolved=True excludes blockers whose
    (term_id, short_title) appears in the resolved-keys set returned by
    the unified blocker_state view."""
    blocker = _make_blocker(
        resolves_in="domain_eda", tables=["mseg"],
        short_title="BWART filter unclear",
    )
    history = _make_history(
        [_make_iteration([blocker])],
        final_iter_num=1,
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG029", "ready_for_s2t", history,
                                 term_name="goods_receipts")])
    prev_csv = _swap_bg_csv(bg)
    prev_resolved = _patched_resolved_keys({("BG029", "BWART filter unclear")})
    try:
        # filter_resolved=False (default) — entry returned.
        entries, _ = loader.load_blockers_for_table("mseg")
        assert len(entries) == 1, "default behavior should include blocker"

        # filter_resolved=True — entry filtered out.
        entries_f, trunc_f = loader.load_blockers_for_table(
            "mseg", filter_resolved=True,
        )
        assert entries_f == [], (
            f"filter_resolved=True should exclude resolved blocker; "
            f"got {entries_f}"
        )
        assert trunc_f == 0
    finally:
        loader._BG_CSV = prev_csv
        loader._load_resolved_blocker_keys = prev_resolved


def test_27_filter_resolved_keeps_unresolved_blocker(tmp_path: Path) -> None:
    """KI-114: filter_resolved=True keeps blockers NOT in the resolved
    set. The resolved set being non-empty doesn't filter unrelated
    blockers."""
    blocker = _make_blocker(
        resolves_in="domain_eda", tables=["mseg"],
        short_title="BWART filter unclear",
    )
    history = _make_history(
        [_make_iteration([blocker])],
        final_iter_num=1,
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG100", "ready_for_s2t", history,
                                 term_name="other_term")])
    prev_csv = _swap_bg_csv(bg)
    # Resolved set contains a different (term_id, short_title) tuple.
    prev_resolved = _patched_resolved_keys({
        ("BG-OTHER", "Different blocker"),
    })
    try:
        entries, trunc = loader.load_blockers_for_table(
            "mseg", filter_resolved=True,
        )
        assert len(entries) == 1, (
            f"unresolved blocker should remain; got {entries}"
        )
        assert entries[0]["term_id"] == "BG100"
        assert trunc == 0
    finally:
        loader._BG_CSV = prev_csv
        loader._load_resolved_blocker_keys = prev_resolved


def test_28_filter_resolved_view_absent_falls_back_to_legacy(
    tmp_path: Path,
) -> None:
    """KI-114: when the view is unavailable (e.g., not yet materialized
    in initial migration window), _load_resolved_blocker_keys returns an
    empty set; filter_resolved=True degrades to legacy unfiltered
    behavior — the blocker IS surfaced."""
    blocker = _make_blocker(
        resolves_in="domain_eda", tables=["mseg"],
        short_title="BWART filter unclear",
    )
    history = _make_history(
        [_make_iteration([blocker])],
        final_iter_num=1,
    )
    bg = tmp_path / "business_glossary.csv"
    _write_bg_csv(bg, [_bg_row("BG029", "ready_for_s2t", history,
                                 term_name="goods_receipts")])
    prev_csv = _swap_bg_csv(bg)
    # View absent → empty set returned.
    prev_resolved = _patched_resolved_keys(set())
    try:
        entries, trunc = loader.load_blockers_for_table(
            "mseg", filter_resolved=True,
        )
        assert len(entries) == 1, (
            "view absent should fall back to legacy unfiltered behavior"
        )
        assert entries[0]["term_id"] == "BG029"
        assert trunc == 0
    finally:
        loader._BG_CSV = prev_csv
        loader._load_resolved_blocker_keys = prev_resolved


# ─── Harness (pytest-compatible + standalone) ─────────────────────────

def _run_standalone() -> int:
    import tempfile
    failed = 0
    tests: list = [
        test_01_empty_history,
        test_02_draft_term_excluded,
        test_03_pre_augmentation_blocker_skipped,
        test_04_domain_eda_matching_table,
        test_05_domain_eda_wrong_table,
        test_06_term_eda_excluded,
        test_07_multiple_terms_same_table,
        test_08_malformed_json_tolerated,
        test_09_analyst_action_resolves_iteration,
        test_10_final_iter_num_fallback,
        test_11_render_empty,
        test_12_render_non_empty_shape,
        test_13_truncation_cap,
        test_14_truncation_note_rendered,
        test_15_final_iter_num_out_of_range,
        test_16_load_for_term_no_history_returns_empty,
        test_17_load_for_term_draft_status_excluded,
        test_18_load_for_term_confirmed_returns_all_blockers,
        test_19_load_for_term_no_resolves_in_filter,
        test_20_load_for_term_id_not_in_glossary_returns_empty,
        test_21_load_for_term_malformed_json_tolerated,
        test_22_load_for_term_truncation_cap,
        test_23_load_for_term_pre_augmentation_blocker_kept,
        test_24_render_stage_a_blockers_section_empty,
        test_25_render_stage_a_blockers_section_full_shape,
        test_26_filter_resolved_excludes_resolved_blocker,
        test_27_filter_resolved_keeps_unresolved_blocker,
        test_28_filter_resolved_view_absent_falls_back_to_legacy,
    ]
    for t in tests:
        needs_tmp_path = "tmp_path" in t.__code__.co_varnames
        try:
            if needs_tmp_path:
                with tempfile.TemporaryDirectory() as td:
                    t(Path(td))
            else:
                t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  [ERR ] {t.__name__}: {type(e).__name__}: {e}")
    total = len(tests)
    print(f"\n{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
