"""Phase-restructure — Stage C runner-side sufficiency construction tests.

The runner now owns lens_consideration[lens].tar_ids construction; the
LLM emits semantic content only (decision + rationale per lens). Tests
verify the mapping logic:

  tar_ids_for_lens = {this run's query rows whose analysis_lens=lens}
                   ∪ {prior TAR ids the LLM cited for that lens at
                      framework_floor}

Eliminates the hallucination class (KI-102/KI-109) structurally — the
LLM never emits id strings, so it cannot hallucinate them.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import run_term_eda as rte  # noqa: E402

_PROMPT = _ROOT / "scripts" / "prompts" / "term_eda_prompt.md"


def _qrow(lens: str) -> dict:
    """Minimal query-row stub carrying analysis_lens (the only field the
    construction function reads)."""
    return {"analysis_lens": lens}


def _ff_decision(decision: str = "picked", rationale: str = "",
                 cite_tar_ids: list[str] | None = None) -> dict:
    return {
        "decision": decision,
        "rationale": rationale,
        "cite_tar_ids": cite_tar_ids or [],
        "queries": [],
    }


def test_construct_maps_lens_to_query_rows() -> None:
    """Each lens's tar_ids is populated from this-run query rows whose
    analysis_lens matches. Two queries on lens A → both ids appear under
    A; the other 7 lenses get empty tar_ids when no queries ran for them.
    """
    all_query_rows = [
        _qrow("measures_overview"),
        _qrow("measures_overview"),
        _qrow("by_dimension"),
    ]
    query_ids = ["TAR-00010", "TAR-00011", "TAR-00012"]
    lens_decisions = {lens: _ff_decision("skipped") for lens in rte._ALL_LENSES}
    lens_decisions["measures_overview"] = _ff_decision("picked")
    lens_decisions["by_dimension"] = _ff_decision("picked")

    lc = rte._construct_lens_consideration(
        llm_lens_consideration={},
        lens_decisions=lens_decisions,
        all_query_rows=all_query_rows,
        query_ids=query_ids,
    )

    assert set(lc["measures_overview"]["tar_ids"]) == {"TAR-00010", "TAR-00011"}
    assert lc["by_dimension"]["tar_ids"] == ["TAR-00012"]
    for lens in ("ranking", "time_trend", "cumulative",
                 "variance", "bucketing", "part_to_whole"):
        assert lc[lens]["tar_ids"] == []


def test_construct_unions_framework_floor_cite_tar_ids() -> None:
    """When framework_floor cited prior TAR ids for a picked lens with
    no new queries, those ids are unioned into the lens's tar_ids."""
    all_query_rows = [_qrow("measures_overview")]
    query_ids = ["TAR-00050"]
    lens_decisions = {lens: _ff_decision("skipped") for lens in rte._ALL_LENSES}
    lens_decisions["measures_overview"] = _ff_decision("picked")
    lens_decisions["ranking"] = _ff_decision(
        "picked", cite_tar_ids=["TAR-00007", "TAR-00008"],
    )

    lc = rte._construct_lens_consideration(
        llm_lens_consideration={},
        lens_decisions=lens_decisions,
        all_query_rows=all_query_rows,
        query_ids=query_ids,
    )

    assert lc["measures_overview"]["tar_ids"] == ["TAR-00050"]
    assert lc["ranking"]["tar_ids"] == ["TAR-00007", "TAR-00008"]


def test_construct_handles_empty_inputs() -> None:
    """Zero queries + zero framework_floor citations → every lens gets
    decision='skipped' (fallback) and empty tar_ids. All 8 keys present.
    """
    lc = rte._construct_lens_consideration(
        llm_lens_consideration={},
        lens_decisions={},
        all_query_rows=[],
        query_ids=[],
    )
    assert set(lc.keys()) == set(rte._ALL_LENSES)
    for lens in rte._ALL_LENSES:
        assert lc[lens]["decision"] == "skipped"
        assert lc[lens]["tar_ids"] == []


def test_construct_prefers_terminal_decision_over_framework_floor() -> None:
    """When LLM's terminal output emits a lens entry with decision +
    rationale, those win over framework_floor; tar_ids still comes from
    the runner regardless of any LLM-emitted tar_ids field."""
    all_query_rows = [_qrow("measures_overview")]
    query_ids = ["TAR-00100"]
    lens_decisions = {lens: _ff_decision("skipped") for lens in rte._ALL_LENSES}
    lens_decisions["measures_overview"] = _ff_decision(
        "picked", rationale="ff-rationale",
    )

    llm_terminal_lc = {
        "measures_overview": {
            "decision": "picked",
            "rationale": "terminal-refined-rationale",
            # Hallucinated id from a non-existent run; must be ignored.
            "tar_ids": ["TAR-99999"],
        },
    }

    lc = rte._construct_lens_consideration(
        llm_lens_consideration=llm_terminal_lc,
        lens_decisions=lens_decisions,
        all_query_rows=all_query_rows,
        query_ids=query_ids,
    )

    assert lc["measures_overview"]["rationale"] == "terminal-refined-rationale"
    # LLM-emitted hallucinated id is dropped; runner constructs from
    # this run's query rows only.
    assert lc["measures_overview"]["tar_ids"] == ["TAR-00100"]
    assert "TAR-99999" not in lc["measures_overview"]["tar_ids"]


def test_construct_dedupes_overlap_between_query_rows_and_cite() -> None:
    """If a framework_floor cite_tar_id happens to coincide with one of
    this run's allocated query ids (boundary case), the result has each
    id exactly once."""
    all_query_rows = [_qrow("measures_overview")]
    query_ids = ["TAR-00010"]
    lens_decisions = {lens: _ff_decision("skipped") for lens in rte._ALL_LENSES}
    lens_decisions["measures_overview"] = _ff_decision(
        "picked", cite_tar_ids=["TAR-00010", "TAR-00007"],
    )

    lc = rte._construct_lens_consideration(
        llm_lens_consideration={},
        lens_decisions=lens_decisions,
        all_query_rows=all_query_rows,
        query_ids=query_ids,
    )

    assert sorted(lc["measures_overview"]["tar_ids"]) == ["TAR-00007", "TAR-00010"]
    # No duplicates.
    assert len(lc["measures_overview"]["tar_ids"]) == len(set(lc["measures_overview"]["tar_ids"]))


def test_construct_filters_non_string_cite_entries() -> None:
    """Defensive: framework_floor cite_tar_ids may include non-string
    entries (None / int / dict) if the LLM emits malformed values.
    Construction filters to strings only — no crash, no bad ids."""
    all_query_rows = [_qrow("measures_overview")]
    query_ids = ["TAR-00010"]
    lens_decisions = {lens: _ff_decision("skipped") for lens in rte._ALL_LENSES}
    lens_decisions["measures_overview"] = _ff_decision(
        "picked", cite_tar_ids=["TAR-00007", None, 42, {"id": "x"}],
    )

    lc = rte._construct_lens_consideration(
        llm_lens_consideration={},
        lens_decisions=lens_decisions,
        all_query_rows=all_query_rows,
        query_ids=query_ids,
    )

    assert sorted(lc["measures_overview"]["tar_ids"]) == ["TAR-00007", "TAR-00010"]


def test_terminal_prompt_is_id_free_per_phase_restructure() -> None:
    """The TERMINAL — SUFFICIENCY PAYLOAD section of the prompt no longer
    asks the LLM to emit `tar_ids` in lens_consideration; the runner
    constructs them server-side. Source-string verification mirrors the
    other prompt-shape tests."""
    src = _PROMPT.read_text(encoding="utf-8")
    terminal_idx = src.find("## TERMINAL — SUFFICIENCY PAYLOAD")
    next_section_idx = src.find("\n## ", terminal_idx + 1)
    assert terminal_idx > -1
    terminal_block = src[terminal_idx:next_section_idx]

    # No `tar_ids` field in the terminal lens_consideration schema.
    assert '"tar_ids"' not in terminal_block
    # No TAR-XXXXX placeholder example anywhere in the terminal block.
    assert "TAR-XXXXX" not in terminal_block
    # Explicit instruction names server-side construction so the LLM
    # knows not to emit tar_ids.
    assert "constructs" in terminal_block
    assert "server-side" in terminal_block


def test_construct_terminal_lens_entry_non_dict_falls_back() -> None:
    """If LLM's terminal lens entry is not a dict (e.g. a string by
    mistake), construction falls back to framework_floor cleanly."""
    all_query_rows = [_qrow("by_dimension")]
    query_ids = ["TAR-00200"]
    lens_decisions = {lens: _ff_decision("skipped") for lens in rte._ALL_LENSES}
    lens_decisions["by_dimension"] = _ff_decision("picked", rationale="ff")

    llm_terminal_lc = {"by_dimension": "this is not a dict"}

    lc = rte._construct_lens_consideration(
        llm_lens_consideration=llm_terminal_lc,
        lens_decisions=lens_decisions,
        all_query_rows=all_query_rows,
        query_ids=query_ids,
    )

    assert lc["by_dimension"]["decision"] == "picked"
    assert lc["by_dimension"]["rationale"] == "ff"
    assert lc["by_dimension"]["tar_ids"] == ["TAR-00200"]
