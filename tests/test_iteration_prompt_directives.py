"""C2 — content-assertion tests for term_injection_iteration_prompt.md and
term_injection_reflection_prompt.md after the deterministic-analyzer
consumption directives land (Theme 1 sub-item 3).

Pure prompt-text tests: no loader, no renderer, no DB. Each test reads
the prompt file and asserts the directive copy is present per the
locked phrasings (Q1 MIDDLE for temporal_coverage, Q2 STRICT-with-
FK-escape for schema_discovery BRIDGE).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
ITERATION_PROMPT_PATH = (
    ROOT / "scripts" / "prompts" / "term_injection_iteration_prompt.md"
)
REFLECTION_PROMPT_PATH = (
    ROOT / "scripts" / "prompts" / "term_injection_reflection_prompt.md"
)


def _normalize(text: str) -> str:
    """Collapse whitespace runs (incl. newlines) to single spaces so the
    content-assertions match even when the source prompt wraps phrases
    across lines."""
    return re.sub(r"\s+", " ", text)


@pytest.fixture(scope="module")
def iteration_prompt() -> str:
    return _normalize(ITERATION_PROMPT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def reflection_prompt() -> str:
    return _normalize(REFLECTION_PROMPT_PATH.read_text(encoding="utf-8"))


def test_iteration_prompt_teaches_temporal_coverage_consumption(iteration_prompt):
    """C2 T1 — directive #3 teaches temporal_coverage consumption with
    Q1's MIDDLE phrasing (acknowledge-or-rationalize) calibrated against
    the seed's bimodal gap_count distribution."""
    assert "temporal_coverage DAR" in iteration_prompt
    assert "MUST acknowledge" in iteration_prompt, (
        "missing the MIDDLE-strictness 'MUST acknowledge' anchor"
    )
    assert "OR include a one-line comment" in iteration_prompt, (
        "missing the rationalize-via-comment escape phrasing"
    )
    assert "rationalizing why no handling is needed" in iteration_prompt


def test_iteration_prompt_teaches_bridge_coverage_consumption(iteration_prompt):
    """Option B Phase 3 — directive #3 sub-bullet 10 teaches the LLM
    about the bridge_coverage_by_filter analyzer + runtime gate."""
    assert "bridge_coverage_by_filter DAR" in iteration_prompt
    assert "hard_stop_bridge_unreachable" in iteration_prompt
    assert "bridge_coverage_consulted" in iteration_prompt
    # Phase 2's CTE-lineage fix must be communicated so the LLM
    # doesn't think CTE-wrapping is an escape hatch.
    assert "CTE-wrapped SQL" in iteration_prompt
    assert "subquery projections" in iteration_prompt


def test_iteration_prompt_bridge_coverage_position(iteration_prompt):
    """Sub-bullet 10 lives between schema_discovery (sub-bullet 9) and
    the STATUS=SKIPPED note (now sub-bullet 11)."""
    sd_pos = iteration_prompt.find("schema_discovery DAR")
    bc_pos = iteration_prompt.find("bridge_coverage_by_filter DAR")
    skipped_pos = iteration_prompt.find("STATUS=SKIPPED")
    assert 0 < sd_pos < bc_pos < skipped_pos, (
        "bridge_coverage sub-bullet must be between schema_discovery "
        "and STATUS=SKIPPED note"
    )


def test_iteration_prompt_teaches_segmentation_threshold_consumption(iteration_prompt):
    """C2 T2 — directive #3 teaches segmentation_threshold consumption."""
    assert "segmentation_threshold DAR" in iteration_prompt
    assert "use the empirical thresholds" in iteration_prompt
    assert "Do NOT invent thresholds" in iteration_prompt


def test_iteration_prompt_teaches_performance_baseline_consumption(iteration_prompt):
    """C2 T3 — directive #3 teaches performance_baseline consumption,
    including the 'absent = magnitude was skipped' co-emission semantic
    confirmed empirically by I6."""
    assert "performance_baseline DAR" in iteration_prompt
    # Multi-key reference — the directive cites all six baseline stats.
    assert "min/max/avg/stddev/p25/p75" in iteration_prompt
    # Co-emission semantic teaching.
    assert "performance_baseline absent" in iteration_prompt
    assert "magnitude was skipped" in iteration_prompt


def test_iteration_prompt_teaches_grain_relationship_consumption(iteration_prompt):
    """C2 T4 — directive #3 teaches grain_relationship consumption with
    the 1:1 / 1:N / N:1 classification + sum_match validation."""
    assert "grain_relationship DAR" in iteration_prompt
    assert "1:1 / 1:N / N:1" in iteration_prompt, (
        "missing the cardinality classification reference"
    )
    assert "match your GROUP BY / JOIN strategy" in iteration_prompt
    assert "sum_match_pct" in iteration_prompt, (
        "missing the sum_match validation teaching"
    )


def test_iteration_prompt_teaches_schema_discovery_consumption(iteration_prompt):
    """C2 T5 — directive #3 teaches schema_discovery consumption with
    Q2's STRICT-with-FK-escape phrasing for BRIDGE entries. Multi-clause
    assertion because schema_discovery is the longest sub-bullet (covers
    PK / FK / SHAPE / BRIDGE)."""
    assert "schema_discovery DAR" in iteration_prompt
    # All four sub-elements present in the directive.
    for elem in ("PK", "FK", "SHAPE", "BRIDGE"):
        assert elem in iteration_prompt, (
            f"schema_discovery sub-element {elem!r} missing from directive"
        )
    # Confidence-bucket gating (per I5's empirical confidence-RI%
    # correlation finding).
    assert "confidence=high are ground-truth join keys" in iteration_prompt
    assert "confidence=medium are advisory" in iteration_prompt
    # BRIDGE strict phrasing (Q2's primary recommendation).
    assert "use the rendered bridge path verbatim" in iteration_prompt
    assert "do NOT invent direct joins between bridged tables" in iteration_prompt
    # FK-escape clause for the 9% direct-FK-overlap edge case (Q2's
    # safety valve).
    assert "If a direct join is empirically supported" in iteration_prompt
    assert "appears as an FK candidate" in iteration_prompt


def test_reflection_prompt_count_delegation_refreshed(reflection_prompt):
    """C2 T6 — reflection prompt's stale L4 ('five bundle') and L28
    ('five directives') count delegations are refreshed.

    Pre-fix: L4 said 'five bundle' (stale; ATTESTATION_FIELDS has
    7), L13 said 'seven' (correct) — self-contradictory. L28 said
    'five directives' (stale from before v3.6/v3.7 added 6-8).

    Post-fix: L4 says 'seven bundle' aligning with L13. L28 says
    'eight directives' with the expanded parenthetical including
    consumer priority, semantic model Layer A, dbt semantic model
    Layer B."""
    # L4 area refresh.
    assert "seven bundle attestation sources" in reflection_prompt, (
        "L4 docstring still says 'five bundle' — stale"
    )
    assert "five bundle attestation sources" not in reflection_prompt, (
        "L4 docstring still has the stale 'five bundle' phrasing"
    )
    # L28 area refresh.
    assert "Same eight directives as iteration prompt" in reflection_prompt
    assert "Same five directives as iteration prompt" not in reflection_prompt
    # Expanded parenthetical includes the 6-8 directives that were
    # missing pre-fix.
    assert "consumer priority" in reflection_prompt
    assert "semantic model Layer A" in reflection_prompt
    assert "dbt semantic model Layer B" in reflection_prompt


def test_iteration_prompt_directives_mention_stage_a_blockers(iteration_prompt):
    """C4 Step 4(d) — iteration prompt teaches Stage A blocker citation
    discipline. Verifies the new section header, the routing-taxonomy
    teaching for each `resolves_in` value, the citation-discipline
    sub-block, the ID format, and the pre-augmentation handling."""
    # New section header.
    assert "STAGE A BLOCKERS" in iteration_prompt
    assert "KNOWN-CONCERN CONTEXT" in iteration_prompt
    # Citation-discipline sub-block.
    assert "STAGE A BLOCKER CITATION DISCIPLINE" in iteration_prompt
    # Routing taxonomy teaching: each value referenced.
    assert "domain_eda" in iteration_prompt
    assert "term_eda" in iteration_prompt
    assert "analyst_decision" in iteration_prompt
    assert "ingestion_required" in iteration_prompt
    assert "source_diagnostic_required" in iteration_prompt
    # ID format communicated.
    assert "iter{N}.b{I}" in iteration_prompt
    assert "iter1.b0" in iteration_prompt
    # Attestation field present in REQUIRED BUNDLE SOURCES preamble.
    assert "stage_a_blockers_consumed" in iteration_prompt
    # Preamble field count updated.
    assert "ten attestation fields" in iteration_prompt
    # Pre-augmentation handling (per Step 0 §SS-4).
    assert "pre-augmentation" in iteration_prompt
