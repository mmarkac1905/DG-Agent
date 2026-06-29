"""C6 — static prompt-content tests for DIRECTIVE 3h.

Verify that the Create S2T system prompt in app/claude_api.py contains:
  - DIRECTIVE 3h (the bridge_coverage_by_filter directive).
  - The empirical-reachability vocabulary that distinguishes 3h from
    3g (cardinality vs reachability).
  - The post-generation bridge-coverage validator reference.
  - The `bridge_coverage_consulted` attestation field name in both
    the directive body and the output JSON skeleton.

Mirrors `tests/test_create_s2t_directive_3g.py` — source-string tests
only; LLM behavior is covered by empirical validation, not unit tests.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CLAUDE_API = _ROOT / "app" / "claude_api.py"


@pytest.fixture(scope="module")
def claude_api_source() -> str:
    return _CLAUDE_API.read_text(encoding="utf-8")


def _system_prompt_block(source: str) -> str:
    m = re.search(
        r'system_prompt\s*=\s*r"""(?P<body>.*?)"""',
        source, re.DOTALL,
    )
    assert m, "could not locate `system_prompt = r\"\"\"...\"\"\"` block"
    return m.group("body")


def test_directive_3h_present_in_system_prompt(claude_api_source):
    body = _system_prompt_block(claude_api_source)
    assert "DIRECTIVE 3h" in body


def test_directive_3h_mentions_bridge_coverage_by_filter(claude_api_source):
    body = _system_prompt_block(claude_api_source)
    assert "bridge_coverage_by_filter" in body
    # The reachability primitive — distinguishes 3h from 3g (cardinality).
    assert "EMPIRICALLY MEASURED reachability" in body


def test_directive_3h_mentions_attestation_field(claude_api_source):
    body = _system_prompt_block(claude_api_source)
    assert "bridge_coverage_consulted" in body


def test_directive_3h_mentions_post_gen_validator(claude_api_source):
    body = _system_prompt_block(claude_api_source)
    assert "post-generation bridge-coverage validator" in body


def test_directive_3h_mentions_unreachable_refusal(claude_api_source):
    body = _system_prompt_block(claude_api_source)
    # The directive must explicitly tell the LLM that filtering on
    # unreachable values causes rejection.
    assert "unreachable" in body
    # And must offer the escape valve (don't generate SQL when
    # forced to filter on unreachable values).
    assert "do NOT generate SQL" in body


def test_directive_3h_appears_after_3g(claude_api_source):
    """DIRECTIVE 3h is data-side reachability; conceptually it sits
    next to 3g (data-side cardinality). The two evidence-based
    refusal directives should be consecutive."""
    body = _system_prompt_block(claude_api_source)
    g_idx = body.find("DIRECTIVE 3g")
    h_idx = body.find("DIRECTIVE 3h")
    e_idx = body.find("DIRECTIVE 3e")
    assert g_idx >= 0 and h_idx >= 0 and e_idx >= 0
    assert g_idx < h_idx < e_idx


def test_output_schema_documents_bridge_coverage_consulted(claude_api_source):
    """The documented output JSON skeleton in the prompt includes the
    new attestation list (mirrors the join_cardinality_consulted
    schema-doc test for F.2)."""
    body = _system_prompt_block(claude_api_source)
    assert '"bridge_coverage_consulted"' in body
