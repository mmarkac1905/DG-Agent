"""Direction F.2 — static prompt-content tests.

Verify that the Create S2T system prompt in app/claude_api.py contains:
  - DIRECTIVE 3g (the cardinality-evidence directive).
  - the bucket vocabulary (per_record_key / catastrophic_fanout / etc).
  - the override line (cardinality > integrity).
  - the new attestation field name (join_cardinality_consulted).
  - DIRECTIVE 3f's cross-reference pointing readers at 3g.

These tests exercise the source string only — they do NOT validate
LLM behavior. Behavior verification is F.2.5 and lives outside the
test suite (one-shot live LLM call).
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
    # Extract the body of the system_prompt raw-string literal so that
    # incidental file-level matches (other prompts, comments) do not
    # pollute the assertions. Anchored to `system_prompt = r"..."` with
    # triple-quote delimiters.
    m = re.search(
        r'system_prompt\s*=\s*r"""(?P<body>.*?)"""',
        source, re.DOTALL,
    )
    assert m, "could not locate `system_prompt = r\"\"\"...\"\"\"` block"
    return m.group("body")


def test_create_s2t_prompt_contains_directive_3g(claude_api_source):
    body = _system_prompt_block(claude_api_source)
    assert "DIRECTIVE 3g" in body
    # Bucket vocabulary
    for term in ("per_record_key", "header_detail",
                 "catastrophic_fanout", "no_signal"):
        assert term in body, f"missing bucket label {term!r} in DIRECTIVE 3g"
    # Override line
    assert "cardinality evidence always wins" in body
    # Attestation field
    assert "join_cardinality_consulted" in body
    # Bridge usage example
    assert "bridge via seri" in body


def test_create_s2t_prompt_3f_references_3g(claude_api_source):
    body = _system_prompt_block(claude_api_source)
    # The 3f -> 3g cross-reference lives inside 3f's bullet list, so
    # `DIRECTIVE 3g` appears at least twice in the prompt: once as the
    # cross-ref text and once as the standalone section header below.
    assert body.count("DIRECTIVE 3g") >= 2, (
        "expected DIRECTIVE 3g to appear at least twice (cross-ref in 3f "
        "+ standalone header)"
    )
    # The cross-reference fragment that 3f embeds is verbatim from the
    # spec; assert it is present in its bullet-list form.
    assert "cardinality overrides integrity" in body
    f_idx = body.find("DIRECTIVE 3f")
    g_header_idx = body.rfind("DIRECTIVE 3g")  # the LAST occurrence is the header
    assert f_idx < g_header_idx, "header for 3g must follow 3f"


def test_output_schema_documents_join_cardinality_consulted(claude_api_source):
    """F.2.3 — the documented output JSON skeleton in the prompt
    includes the new attestation list."""
    body = _system_prompt_block(claude_api_source)
    # The skeleton is a Python-style dict literal embedded in the prompt
    # text after the SELF-ATTESTATION FIELDS guidance.
    assert '"join_cardinality_consulted"' in body
