"""Evidence-grounded business-term discovery (analyst request).

The analyst should not need an operator to know what terms a source can
answer. The system already holds the evidence: profiled tables, the
column dictionary, and the empirically measured join graph. This module
assembles that evidence and asks the LLM to propose candidate business
terms that are actually computable from it — each citing the tables and
join evidence that make it answerable.

The suggestions are drafts for the New Term form; nothing is written
anywhere until the analyst creates the term through the normal ladder.
"""
from __future__ import annotations

import json

from _data_analysis_shared import source_schema
from db import query

_MAX_DICT_ROWS = 220
_MAX_JOIN_ROWS = 40

_SYSTEM_PROMPT = """You are a data product analyst. Given the evidence about a
source system (tables with row counts, column dictionary, empirically measured
join graph, and the business terms that already exist), propose candidate
business terms an analyst could take through a source-to-target pipeline.

Rules:
- Propose 5 to 8 candidates, ordered from most to least business-valuable.
- Every candidate MUST be computable from the listed tables using ONLY joins
  that the measured join graph supports (per_record_key or header_detail, or
  the safe direction of a pair). Never build a candidate on a
  catastrophic_fanout join direction.
- Do not duplicate or trivially rephrase the existing terms.
- Definitions must be precise contracts: state the measure, the grain, the
  unit, and every filter/exclusion explicitly, the way a business owner would
  sign it off. No implementation hints (no SQL, no table names inside the
  definition text).
- term_name is snake_case; domain is a short snake_case business area.

Return ONLY a JSON object, no markdown fences, with this shape:
{
  "candidates": [
    {
      "term_name": "...",
      "display_name": "...",
      "definition": "...",
      "unit": "...",
      "grain": "...",
      "domain": "...",
      "required_tables": ["..."],
      "evidence": "one sentence naming the join/profile evidence (cite DAR ids) that makes this computable",
      "difficulty": "easy|medium|hard",
      "business_value": "one sentence on why a business owner would want this"
    }
  ]
}"""


def _table_inventory(src: str) -> list[tuple[str, int]]:
    tables = query(
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema = '{src}' ORDER BY table_name"
    )
    out = []
    for t in tables["table_name"].tolist():
        try:
            n = query(f'SELECT COUNT(*) AS n FROM {src}."{t}"').iloc[0]["n"]
        except Exception:  # noqa: BLE001
            n = -1
        out.append((t, int(n)))
    return out


def _dictionary_lines(src: str, table_names: list[str]) -> list[str]:
    if not table_names:
        return []
    quoted = ",".join(f"'{t.lower()}'" for t in table_names)
    df = query(
        "SELECT table_name, field_name, data_type, description_en "
        "FROM main_seeds.sap_data_dictionary "
        f"WHERE LOWER(table_name) IN ({quoted}) "
        "ORDER BY table_name, field_name"
    )
    lines = []
    for _, r in df.head(_MAX_DICT_ROWS).iterrows():
        desc = (str(r["description_en"]) or "")[:90]
        lines.append(
            f"{r['table_name']}.{r['field_name']} [{r['data_type']}] {desc}"
        )
    return lines


def _join_graph_lines(table_names: list[str]) -> list[str]:
    """Latest non-superseded cardinality evidence between source tables."""
    df = query(
        "SELECT id, result_json FROM main_seeds.domain_analysis_results "
        "WHERE analysis_type = 'join_cardinality' AND status = 'success' "
        "AND (superseded_by IS NULL OR superseded_by = '') "
        "ORDER BY executed_at_utc"
    )
    names = {t.lower() for t in table_names}
    by_pair: dict[tuple, str] = {}
    for _, r in df.iterrows():
        try:
            j = json.loads(r["result_json"])
        except Exception:  # noqa: BLE001
            continue
        t1, t2 = (j.get("t1") or "").lower(), (j.get("t2") or "").lower()
        if t1 not in names or t2 not in names:
            continue
        keys = "+".join(j.get("key_columns_t1") or [])
        cls = j.get("fanout_class", "?")
        safe = j.get("safe_direction") or ""
        line = f"{t1} <-> {t2} on {keys}: {cls}"
        if j.get("avg_fanout") is not None:
            line += f" (avg {j['avg_fanout']}x)"
        if safe:
            line += f"; safe direction: {safe}"
        line += f" [{r['id']}]"
        by_pair[(t1, t2, keys)] = line
    return list(by_pair.values())[:_MAX_JOIN_ROWS]


def _existing_terms() -> list[str]:
    df = query(
        "SELECT term_name, definition FROM main_seeds.business_glossary "
        "WHERE status != 'archived'"
    )
    return [f"{r['term_name']}: {str(r['definition'])[:110]}"
            for _, r in df.iterrows()]


def assemble_source_evidence() -> str:
    src = source_schema()
    inv = _table_inventory(src)
    table_names = [t for t, _ in inv]
    parts = [f"# Source evidence: {src}", "", "## Tables (row counts)"]
    parts += [f"- {t}: {n:,} rows" for t, n in inv]
    parts += ["", "## Column dictionary"]
    parts += _dictionary_lines(src, table_names) or ["(no dictionary rows)"]
    parts += ["", "## Measured join graph (empirical cardinality evidence)"]
    parts += _join_graph_lines(table_names) or ["(no cardinality evidence yet)"]
    parts += ["", "## Existing business terms (do not duplicate)"]
    parts += _existing_terms() or ["(none)"]
    return "\n".join(parts)


def suggest_term_candidates() -> dict:
    """Returns {'candidates': [...]} or {'error': str}."""
    from claude_api import _post_claude
    evidence = assemble_source_evidence()
    result = _post_claude(_SYSTEM_PROMPT, evidence, max_tokens=4000)
    if not isinstance(result, dict):
        return {"error": f"unexpected LLM result type: {type(result).__name__}"}
    if "error" in result:
        return result
    if "candidates" not in result or not isinstance(result["candidates"], list):
        return {"error": "LLM returned JSON without a candidates list"}
    return result
