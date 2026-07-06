"""Pre-S2T Reasoning Layer runner — the term-analysis (BAR) runner.

Implements the full mainline flow, steps 0a–15: the multi-turn Term
EDA loop that produces business_term_analysis_results rows, plus the
context-integrity safeguards.

Delivered here:
- Four prompt templates: scripts/prompts/term_{injection_iteration,
  injection_reflection, injection_finalization, condition_extraction}_prompt.md
- Attestation checker (safeguard 3) — machine-checkable five-field list
  presence check, invoked after every LLM response.
- Iteration loop with all detectors: oscillation, mechanical
  regression, two-consecutive-mechanical, alignment regression,
  complexity explosion, scope-sanity, budget cap.
- Finalization with synthesis-fallback when budget exhausts
  before the finalization LLM call.
- Step 14 conditional UPDATE via _bar_writer.update_bar_row
  + sibling-recovery row on affected_rows==0.
- Drift probe with glossary-row hash sub-check (safeguard 2)
- Pre-injection context audit (safeguard 1)
- Citation audit at step 6d (safeguard 4) — structured token scanner.
- Budget pressure instrumentation (safeguard 5) — bundle_trimmed_layers
  recorded in iteration_trace.

Exit codes:
  0 — soft convergence (any confidence)
  1 — hard-stop (any convergence_reason) or preflight operational failure
      (term not found, archived, concurrency rejection)
  2 — infrastructure failure (DuckDB locked, I/O error) — no BAR row written
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import duckdb
from _source_config import SOURCE_SCHEMA
import requests

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
from _bar_writer import (  # noqa: E402
    BAR_TABLE_FQN,
    sweep_orphaned_inprogress,
    update_bar_row,
    write_bar_row,
)
from _context_assembler import assemble_context, ContextBundle, ContextDegradedError  # noqa: E402
from _drift_probe import compute_drift_probe, hash_glossary_row  # noqa: E402

_PROJECT_ROOT = _SCRIPTS_DIR.parent
DB_PATH = _PROJECT_ROOT / "cpe_analytics.duckdb"
PROMPTS_DIR = _SCRIPTS_DIR / "prompts"

# app/claude_api.py holds _post_claude (the raw POST wrapper), and API_KEY.
sys.path.insert(0, str(_PROJECT_ROOT / "app"))
from claude_api import API_KEY  # noqa: E402


# ─── Configuration ────────────────────────────────────────────────────

# Anthropic API endpoint — shared across all the runner's LLM calls.
API_URL = "https://api.anthropic.com/v1/messages"

# Model pricing per 1M tokens. Sonnet for reasoning, Haiku for
# preflight extraction. Prices are input/output base; cache_read gets 0.1x,
# cache_write gets 1.25x (5m TTL) or 2x (1h TTL) of input.
MODEL_SONNET = "claude-sonnet-4-5"        # iteration / reflection / finalization
MODEL_HAIKU = "claude-haiku-4-5"          # preflight extraction only

_PRICING = {
    MODEL_SONNET:            {"input_per_mtok": 3.00,  "output_per_mtok": 15.00},
    "claude-sonnet-4-6":     {"input_per_mtok": 3.00,  "output_per_mtok": 15.00},
    "claude-sonnet-4-20250514": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
    MODEL_HAIKU:             {"input_per_mtok": 0.80,  "output_per_mtok": 4.00},
    "claude-haiku-4-5-20251001": {"input_per_mtok": 0.80, "output_per_mtok": 4.00},
}

# Cache-outage resilience: CACHE_ENABLED=false disables cache_control blocks
# at the request level. Exercised by regression Scenario 12.
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "true").lower() != "false"

# SDK note: we don't import the anthropic SDK —
# this project uses requests.post directly (existing pattern in app/claude_api.py).
# The JSON body supports cache_control blocks identically; SDK would be a wrapper
# not a requirement.

# Attestation fields every one of the runner's LLM responses must echo.
# `semantic_model_consumed` covers Layer A.
# `dbt_semantic_model_consumed` covers Layer B — list of
# dbt model_name rows the LLM consulted. Empty list is acceptable when
# Layer B has no rows for this scope (scope is raw-only or out-of-graph).
#
# Option B Phase 4 split: iteration LLM consults bridge_coverage_by_filter
# DARs while generating SQL (must attest bridge_coverage_consulted);
# reflection + finalization LLMs summarize/evaluate iteration results and
# do not themselves consult those DARs (so requiring them to attest
# bridge_coverage_consulted would be semantically wrong, and surfaced as
# a Phase 4 v1 regression where finalization-attestation overwrote a
# correct hard_stop_bridge_unreachable convergence_reason).
ATTESTATION_FIELDS_ITERATION = (
    "ontology_consumed",
    "domain_facts_consumed",
    "analysis_findings_consumed",
    "dar_consumed",
    "prior_bar_consumed",
    "semantic_model_consumed",
    "dbt_semantic_model_consumed",
    # Option B Phase 3 (OQ-3a) — always-emitted; conditional validation
    # against in-scope bridge_coverage_by_filter DARs lives in
    # _bridge_coverage_gate._check_bridge_coverage_attestation.
    "bridge_coverage_consulted",
    # C3 (Theme 1 sub-items 1+2) — TAR-NNNNN citation discipline.
    # Iteration LLM emits the list of term_analysis_results.id rows
    # consulted from the bundle's TERM EDA section (both row_type=query
    # and row_type=sufficiency; both current-term TARs and cross-term
    # prior TARs). Always-emit; no conditional gate (per OQ-C3-2:
    # bridge_coverage's empirical-refutation rationale doesn't transfer).
    "tars_consulted",
    # C4 (Theme 1 sub-item 5) — Stage A blocker citation discipline.
    # Iteration LLM emits the list of Stage A blocker IDs consulted
    # from the "## Stage A blockers" bundle section. ID format
    # "iter{N}.b{I}" mirrors render_stage_a_blockers_section. Surfaces
    # all resolves_in routings (per OQ-C4-2); analyst_decision and
    # ingestion_required blockers are precisely the ones the iteration
    # LLM cannot delegate upstream and must acknowledge in
    # reasoning_summary. Always-emit (per OQ-C4 leans).
    "stage_a_blockers_consumed",
)

ATTESTATION_FIELDS_FINALIZATION = (
    "ontology_consumed",
    "domain_facts_consumed",
    "analysis_findings_consumed",
    "dar_consumed",
    "prior_bar_consumed",
    "semantic_model_consumed",
    "dbt_semantic_model_consumed",
)

# Backward-compat alias for any caller still referencing the old name.
ATTESTATION_FIELDS = ATTESTATION_FIELDS_ITERATION


# ─── Helpers ──────────────────────────────────────────────────────────


def _load_prompt(template_name: str) -> str:
    """Load a prompt template by filename. Caller uses _fill_template
    for variable interpolation."""
    path = PROMPTS_DIR / template_name
    return path.read_text(encoding="utf-8")


def _fill_template(tmpl: str, subs: dict) -> str:
    """Substitute {key} tokens with subs[key] via plain string replace.

    Avoids str.format_map's choking on unescaped braces in JSON examples
    inside prompt templates (surfaced during live verification —
    extraction prompt's output-format JSON block contains literal `{`
    that format_map interprets as a substitution field).

    Braces in template content that are NOT surrounded by a known key
    pass through unchanged. Missing subs keys raise KeyError on access
    at format time (caller error) — we fail fast on typos but don't
    attempt dict-lookup-style safety.
    """
    result = tmpl
    for key, value in subs.items():
        result = result.replace("{" + key + "}", str(value))
    return result


_MOCK_CALL_COUNTERS: dict[str, int] = {}


def _infer_call_type(task_prompt: str, model: str) -> str:
    """Classify the call from its prompt signature for mock dispatch."""
    if model.startswith("claude-haiku"):
        return "extraction"
    # Sonnet calls: match on distinctive JSON schema + task framing per prompt.
    # Iteration prompt ends with "Propose the next SQL candidate now." (capital)
    # Reflection prompt's YOUR TASK starts with "Given the SQL from the current iteration"
    # Finalization prompt's YOUR TASK starts with "The iteration loop has terminated with"
    if "Given the SQL from the current iteration" in task_prompt:
        return "reflection"
    if "The iteration loop has terminated with" in task_prompt:
        return "finalization"
    if ("Propose the next SQL candidate now" in task_prompt
            or "propose\nthe next SQL candidate" in task_prompt
            or "propose\nthe **next** SQL candidate" in task_prompt):
        return "iteration"
    # Fallback: check for schema-distinctive keys in the JSON example block
    if "shadow_rubric_breakdown" in task_prompt:
        return "reflection"
    if "final_metric_value" in task_prompt and "term_conditions_covered" in task_prompt:
        return "finalization"
    if '"query_sql"' in task_prompt:
        return "iteration"
    return "unknown"


def _load_mock_response(mode: str, system_prompt: str, task_prompt: str, model: str) -> dict:
    """PIECE8_MOCK_MODE dispatch — load fixture JSON for scenario.

    Fixture layout: tests/piece8_mocks/<mode>/<call_type>_<N>.json where
    N is a 1-indexed counter per call_type within this process. Missing
    fixture → return an 'error' dict mimicking an API failure.
    """
    call_type = _infer_call_type(task_prompt, model)
    counter_key = f"{mode}:{call_type}"
    _MOCK_CALL_COUNTERS[counter_key] = _MOCK_CALL_COUNTERS.get(counter_key, 0) + 1
    seq = _MOCK_CALL_COUNTERS[counter_key]

    mock_dir = _PROJECT_ROOT / "tests" / "piece8_mocks" / mode
    fixture_path = mock_dir / f"{call_type}_{seq}.json"
    if not fixture_path.exists():
        # Fall back to unnumbered fixture
        alt_path = mock_dir / f"{call_type}.json"
        if alt_path.exists():
            fixture_path = alt_path
        else:
            return {
                "error": f"mock fixture missing: {fixture_path}",
                "response": {},
                "cost_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }

    try:
        with fixture_path.open("r", encoding="utf-8") as f:
            fixture = json.load(f)
    except Exception as e:
        return {
            "error": f"mock fixture load failed: {e}",
            "response": {},
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    return {
        "error": fixture.get("error"),
        "response": fixture.get("response", {}),
        "cost_usd": fixture.get("cost_usd", 0.001),
        "input_tokens": fixture.get("input_tokens", 100),
        "output_tokens": fixture.get("output_tokens", 50),
        "cache_read_input_tokens": fixture.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": fixture.get("cache_creation_input_tokens", 0),
    }


def _compute_call_cost(usage: dict, model: str, bp1_ttl: str = "1h") -> float:
    """Split cost by cache state.
    cache_read × 0.1x input rate; cache_write × 1.25x (5m) or 2x (1h);
    uncached_input × 1x; output × output_rate.
    bp1_ttl is the TTL of the longest-cached block (BP1). Other
    blocks are always 5m. We conservatively charge the 2x premium against
    the 1h-TTL block for cache_creation; in practice Anthropic's usage.cache_creation
    field doesn't split by TTL, so this is a slight over-estimate when BP1
    dominates the write.
    """
    rates = _PRICING.get(model, _PRICING[MODEL_SONNET])
    input_rate = rates["input_per_mtok"] / 1_000_000
    output_rate = rates["output_per_mtok"] / 1_000_000

    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    uncached_input = usage.get("input_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0

    write_multiplier = 2.0 if bp1_ttl == "1h" else 1.25
    return (
        cache_read * input_rate * 0.1
        + cache_write * input_rate * write_multiplier
        + uncached_input * input_rate
        + output * output_rate
    )


def _call_piece8_prompt(
    *,
    system_prompt: str,
    bundle: Optional[ContextBundle],
    task_prompt: str,
    model: str = MODEL_SONNET,
    max_tokens: int = 4000,
    preflight_only: bool = False,
) -> dict:
    """4-breakpoint caching wrapper via requests.post.

    BP1 (system, 1h): system_prompt
    BP2 (user, 5m):   static + examples layers (skipped if preflight_only)
    BP3 (user, 5m):   ontology + business layers (skipped if preflight_only)
    BP4 (user, 5m):   dynamic layer (skipped if preflight_only)
    Task tail (user, uncached): task_prompt

    When CACHE_ENABLED=false (Scenario 12 env variant), cache_control
    blocks are omitted. Semantic behavior is identical; cost rises ~2×.

    Returns dict with keys: response, cost_usd, input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens, error.
    """
    # Mock mode for the regression harness.
    # PIECE8_MOCK_MODE=<scenario> loads fixture responses from
    # tests/piece8_mocks/<scenario>/<call_key>.json. call_key derived
    # from model + task_prompt hash or explicit counter in fixture dir.
    mock_mode = os.environ.get("PIECE8_MOCK_MODE")
    if mock_mode:
        return _load_mock_response(mock_mode, system_prompt, task_prompt, model)

    if not API_KEY or API_KEY == "your-api-key-here":
        return {
            "error": "ANTHROPIC_API_KEY not set",
            "response": {},
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    # System block — BP1
    if CACHE_ENABLED:
        system_payload = [{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }]
    else:
        system_payload = system_prompt

    # User content blocks — BP2/3/4 + task tail
    content: list[dict] = []
    if bundle is not None and not preflight_only:
        bp2_text = ((bundle.static_layer_text or "")
                    + ("\n\n" + bundle.examples_layer_text if bundle.examples_layer_text else ""))
        bp3_text = ((bundle.ontology_layer_text or "")
                    + ("\n\n" + bundle.business_layer_text if bundle.business_layer_text else ""))
        bp4_text = bundle.dynamic_layer_text or ""

        def _add_block(text: str, cache: bool) -> None:
            if not text.strip():
                return
            block = {"type": "text", "text": text}
            if cache and CACHE_ENABLED:
                block["cache_control"] = {"type": "ephemeral"}
            content.append(block)

        _add_block(bp2_text, cache=True)
        _add_block(bp3_text, cache=True)
        _add_block(bp4_text, cache=True)

    # Task tail is always uncached (changes every call)
    content.append({"type": "text", "text": task_prompt})

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_payload,
        "messages": [{"role": "user", "content": content}],
    }

    try:
        response = requests.post(
            API_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json=body,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        return {
            "error": f"API request failed: {e}",
            "response": {},
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    # Extract text from response content blocks
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        parsed = json.loads(text)
        error = None
    except json.JSONDecodeError as e:
        parsed = {"raw_response": text[:500]}
        error = f"JSON parse: {e}"

    usage = data.get("usage", {})
    cost = _compute_call_cost(usage, model, bp1_ttl="1h" if CACHE_ENABLED else "5m")

    return {
        "response": parsed,
        "cost_usd": cost,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "error": error,
    }


def attestation_complete(response: dict, fields: tuple = ATTESTATION_FIELDS_ITERATION) -> bool:
    """Safeguard 3 machine check (attestation).

    Returns True only if every field in `fields` is present in the
    response and is a list (possibly empty). None, missing, or non-list
    → False.

    The `fields` parameter lets the iteration gate require the 8-field
    contract (ATTESTATION_FIELDS_ITERATION, includes bridge_coverage_consulted)
    while reflection + finalization gates use the 7-field contract
    (ATTESTATION_FIELDS_FINALIZATION). Defaults to ITERATION for
    backward-compat with older call sites and tests.
    """
    if not isinstance(response, dict):
        return False
    for field in fields:
        if field not in response:
            return False
        value = response[field]
        if value is None or not isinstance(value, list):
            return False
    return True


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).lower()


def _sql_hash(sql: str) -> str:
    return hashlib.sha256(_normalize_sql(sql).encode()).hexdigest()[:16]


def _sql_stability(current: str, prior: Optional[str]) -> float:
    """Normalized edit distance between two SQL strings.
    Returns 0.0 if prior is None (first iteration, trivially stable).
    """
    if prior is None:
        return 0.0
    a = _normalize_sql(current)
    b = _normalize_sql(prior)
    if not a and not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return 1.0 - ratio


_CREATE_COLLISION_PATTERN = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:TABLE|VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(?:\"[^\"]+\"|\w+)\.)?(\"[^\"]+\"|\w+)",
    re.IGNORECASE,
)


def _ontology_collision_check(sql: str, existing_models: list[str]) -> list[str]:
    """Post-hoc ontology-collision check.

    Greps SQL for CREATE TABLE/VIEW <name> patterns. If any <name>
    matches an existing_models entry (case-insensitive, strip quotes
    + schema prefix), return it in the collision list. Caller hard-stops
    with `hard_stop_ontology_collision`.

    This replaces an earlier Jinja {{ ref() }} directive which would fail
    the DuckDB mechanical gate. Iteration prompts now direct the LLM
    to use literal table names; this function enforces no-collision.
    """
    if not existing_models:
        return []
    existing_set = {m.strip('"').lower() for m in existing_models}
    collisions: list[str] = []
    for match in _CREATE_COLLISION_PATTERN.finditer(sql):
        created_name = match.group(1).strip('"').lower()
        if created_name in existing_set:
            collisions.append(created_name)
    return sorted(set(collisions))


# AST-based citation audit — promoted from an earlier regex version.
# sqlparse-based tokenization + DuckDB validation for table/column classes.
# Design target: threshold=0 for identifier classes.
# known_issue #32 closed here.

try:
    import sqlparse  # noqa: F401
    from sqlparse.sql import IdentifierList, Identifier, Function, Parenthesis
    from sqlparse.tokens import Keyword, DML, Whitespace, Punctuation, Name
    _SQLPARSE_OK = True
except ImportError:
    _SQLPARSE_OK = False

# DuckDB + SAP + SQL-idiom allowlist for function calls.
# Not exhaustive — DuckDB has ~300 built-ins; this covers what the runner's SQL
# realistically emits. Unknown functions fall through to identifier class
# and can be surfaced explicitly.
_FN_ALLOWLIST = frozenset({
    # Aggregate
    "count", "sum", "avg", "min", "max", "stddev", "variance",
    "string_agg", "array_agg", "list_agg",
    # Scalar
    "coalesce", "nullif", "nvl", "ifnull", "case",
    "abs", "round", "floor", "ceil", "ceiling", "mod", "power", "sqrt",
    "sign", "log", "ln", "exp", "greatest", "least",
    # String
    "lower", "upper", "concat", "concat_ws", "substring", "substr",
    "length", "char_length", "trim", "ltrim", "rtrim", "replace",
    "regexp_matches", "regexp_extract", "regexp_replace", "split_part",
    "starts_with", "ends_with", "contains",
    # Date / time
    "date_trunc", "date_part", "date_diff", "date_add", "date_sub",
    "datediff", "dateadd", "extract", "now", "today",
    "current_date", "current_timestamp", "current_time",
    "strftime", "strptime", "julianday", "epoch", "to_timestamp",
    "age", "year", "month", "day", "hour", "minute", "second",
    # Cast / type
    "cast", "try_cast", "convert",
    # JSON (DuckDB)
    "json_extract", "json_extract_string", "json_array_length",
    "json_keys", "to_json", "from_json",
    # Window
    "row_number", "rank", "dense_rank", "lag", "lead",
    "first_value", "last_value", "nth_value", "percent_rank",
    # Array
    "unnest", "list_contains", "array_length", "array_slice", "list_value",
})

# SQL keyword allowlist — tokens that never need DuckDB validation.
_SQL_KEYWORDS = frozenset({
    "select", "from", "where", "group", "order", "by", "having", "limit",
    "offset", "fetch", "first", "row", "rows", "only",
    "join", "inner", "left", "right", "outer", "cross", "full", "natural",
    "on", "using", "lateral",
    "and", "or", "not", "null", "is", "isnull", "notnull",
    "as", "distinct", "union", "intersect", "except", "all", "any", "some",
    "case", "when", "then", "else", "end",
    "in", "between", "like", "ilike", "exists", "over", "partition",
    # Window-frame vocabulary (BG034 hard-stopped because UNBOUNDED and
    # PRECEDING were flagged as unknown columns by this audit)
    "unbounded", "preceding", "following", "current", "range", "groups",
    "exclude", "ties", "others", "window", "qualify", "respect", "ignore",
    "asc", "desc", "nulls", "first", "last",
    "true", "false", "with", "recursive",
    # Types (when bare)
    "int", "integer", "bigint", "smallint", "tinyint",
    "varchar", "text", "char", "string", "blob",
    "date", "timestamp", "timestamptz", "time", "interval",
    "double", "float", "real", "decimal", "numeric",
    "boolean", "bool", "json", "array", "list", "struct", "map",
    # Schema prefixes used in project
    "main", "main_seeds", "main_staging", "main_vault", "main_marts",
    "main_obt", "main_knowledge", f"{SOURCE_SCHEMA}", "information_schema",
    # DML + DDL keywords (CREATE TABLE/VIEW used in collision scenarios)
    "default", "values", "insert", "update", "delete",
    "create", "table", "view", "drop", "alter", "if", "exists",
    "temp", "temporary", "replace",
    # Common table expression keyword
    "cte",
})

_CITATION_ID_PATTERN = re.compile(r"\b(?:DAR-\d{5,}|DF-\d{4,}|AF\d{3,}|BAR-\d{5,})\b")
_FROM_JOIN_PATTERN = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:(\w+)\.)?(\w+)(?:\s+(?:AS\s+)?(\w+))?",
    re.IGNORECASE,
)

# SQL functions that use FROM as a positional delimiter.
# Must be stripped before _FROM_JOIN_PATTERN matches, otherwise their
# inner column argument is falsely classified as a table reference
# (e.g. EXTRACT(QUARTER FROM m.goods_receipt_date) would yield
# 'table:goods_receipt_date' — surfaced on BG002 iter 1 pre-fix).
# Limitation: one level of parenthesis nesting. Deeper nesting falls
# through to the status-quo FROM/JOIN match — not a regression.
_FUNCTION_FROM_PATTERN = re.compile(
    r"\b(?:EXTRACT|TRIM|SUBSTRING)\s*\([^()]*?\bFROM\b[^()]*?\)",
    re.IGNORECASE,
)
# Require alpha-start for the prefix in
# `<prefix>.<col>`. Real SQL aliases / table names / CTEs never start
# with a digit; numeric prefixes only occur in decimal literals like
# 0.05 (tolerance percentages) or 100.50 (amounts). The legacy regex
# captured those as "unknown alias:0" / "alias:100", triggering
# citation-audit hard-stops on legitimate SQL. BG028 live verification
# 2026-04-21 tripped this on `ABS(x - y) / po_qty < 0.05`.
_QUALIFIED_COL_PATTERN = re.compile(r"\b([a-zA-Z_]\w*)\.(\w+)\b")
_FUNCTION_PATTERN = re.compile(r"\b(\w+)\s*\(")
_CTE_PATTERN = re.compile(r"\bWITH\s+(?:RECURSIVE\s+)?(.+?)\bSELECT\b", re.IGNORECASE | re.DOTALL)
_CTE_NAME_PATTERN = re.compile(r"\b(\w+)\s+AS\s*\(", re.IGNORECASE)
_BARE_IDENT_PATTERN = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\b")
# SELECT output alias extraction.
# Matches `AS <name>` appearing anywhere in SQL; safe because:
# - CTE `WITH <name> AS (` is already handled separately via _CTE_NAME_PATTERN
# - Table aliases `FROM x AS alias` are already tracked in `aliases` dict
# - SELECT output aliases `AVG(col) AS metric` are the case this catches
# - Implicit aliases (no AS) are not caught — documented deferral
#
# Regex tightened to require alpha-start for alias names.
# SQL output aliases MUST begin with [a-zA-Z_]; the legacy `\w+` match
# also captured numeric tokens from CAST type arguments (e.g.
# `CAST(x AS DECIMAL(13,2))` flagged `0` + `2`; `CAST(y AS INTEGER)` is
# fine because INTEGER starts with alpha but arguments `(10)` followed
# by AS cause issues). Tightened to `[a-zA-Z_]\w*` so numeric-only
# tokens no longer match — closes the BG028 iter-1 citation-audit
# hard-stop case (known_issue #39).
_OUTPUT_ALIAS_PATTERN = re.compile(r"\bAS\s+([a-zA-Z_]\w*)", re.IGNORECASE)


def _extract_cte_names(sql: str) -> set[str]:
    """Parse CTE names from WITH clauses. Returns lowercased set.

    Bug fix: the original implementation used non-greedy
    WITH...SELECT match which stopped at the FIRST `SELECT` inside the
    first CTE's body, missing all subsequent CTEs (like `po_gr_pairs`
    in BG001's multi-CTE SQL). Rewritten to search the whole SQL for
    `<name> AS (` patterns — safe because:
    - Table aliases `FROM x AS alias` don't have `(` after alias
    - Subquery aliases `(SELECT ...) AS subq` have `) AS subq` order
    - Only CTE defs and SELECT output aliases like `AVG(x) AS metric`
      use the `<name> AS (` sequence; the latter would need `AVG(x)`
      before, so our regex `\b(\w+)\s+AS\s*\(` captures `<name> AS (`
      but the CONTEXT determines CTE vs alias.
    Broad capture is safe for audit purposes — all these names become
    "known" identifiers.
    """  # noqa: W605 — regex chars in prose
    return {n.lower() for n in _CTE_NAME_PATTERN.findall(sql)}


def _strip_function_from_clauses(sql: str) -> str:
    """Blank out EXTRACT/TRIM/SUBSTRING arg spans so
    _FROM_JOIN_PATTERN doesn't misclassify their inner columns as
    table names. Length-preserving (whitespace replacement) so any
    downstream position math stays valid."""
    def blank(m: re.Match) -> str:
        return " " * len(m.group(0))
    return _FUNCTION_FROM_PATTERN.sub(blank, sql)


def _extract_table_refs(sql: str) -> list[tuple[str, str, str]]:
    """Returns list of (schema, table, alias) from FROM/JOIN clauses."""
    sql = _strip_function_from_clauses(sql)
    results = []
    for m in _FROM_JOIN_PATTERN.finditer(sql):
        schema = (m.group(1) or "").lower()
        table = m.group(2).lower()
        alias = (m.group(3) or "").lower()
        if table in _SQL_KEYWORDS or table == "select":
            continue  # Subquery, not a table name
        results.append((schema, table, alias))
    return results


def _strip_string_literals(sql: str) -> str:
    """Remove content between string quotes so the identifier audit doesn't
    flag values like 'V001' or "EUR" as unknown columns. Preserves
    surrounding whitespace so regex position math stays valid."""
    # Replace string contents with blanks (preserve length)
    def blank(match):
        return match.group(0)[0] + " " * (len(match.group(0)) - 2) + match.group(0)[-1]
    stripped = re.sub(r"'[^']*'", blank, sql)
    stripped = re.sub(r'"[^"]*"', blank, stripped)
    return stripped


def _ast_audit(
    sql: str,
    bundle_text: str,
    response_attestation: Optional[dict],
    conn: Optional["duckdb.DuckDBPyConnection"] = None,
    scope_tables: Optional[list[str]] = None,
) -> list[str]:
    """AST-based citation audit — known_issue #32 fix.

    Four identifier classes with distinct validation:
    1. Table refs (FROM/JOIN): must resolve in information_schema.tables OR
       be a CTE defined within this SQL.
    2. Column refs (qualified `x.y` or bare): must resolve in
       information_schema.columns OR sap_data_dictionary.field_name.
       Qualified refs where prefix is an alias inherit the alias's table.
    3. Function calls (`name(`): must be in _FN_ALLOWLIST.
    4. Citation IDs (DAR-/DF-/AFNNN/BAR-): must appear in
       response_attestation declared lists OR in bundle text.

    Returns list of "<class>:<ident>" strings for each unknown. Zero
    unknowns = pass. Caller hard-stops on any non-empty result.
    """
    unknowns: list[str] = []
    # Strip string-literal content so 'V001', "EUR" etc. don't get flagged
    # as identifiers. Position-preserving so regexes still align with sql.
    sql_stripped = _strip_string_literals(sql)
    cte_names = _extract_cte_names(sql_stripped)
    flagged_tables: set[str] = set()  # tables already flagged — skip in bare pass
    # Extract SELECT output aliases (and all AS-bound names).
    # Captures CTE aliases (redundant but harmless — cte_names covers them)
    # and SELECT-list output aliases like `SUM(x) AS metric`. These are
    # newly-defined names, not references to existing columns, so must be
    # skipped during column validation.
    output_aliases = {m.group(1).lower()
                      for m in _OUTPUT_ALIAS_PATTERN.finditer(sql_stripped)}

    # --- Class 4: Citation IDs (against ORIGINAL sql — they're inside strings sometimes) ---
    # Citation detection runs on ORIGINAL sql (string-stripping preserves
    # position but blanks content — citations inside string literals would
    # be missed). Positions from original still align with sql_stripped since
    # _strip_string_literals is length-preserving.
    citation_id_spans: list[tuple[int, int]] = []
    for match in _CITATION_ID_PATTERN.finditer(sql):
        cid = match.group()
        citation_id_spans.append((match.start(), match.end()))
        if cid in bundle_text:
            continue
        if response_attestation:
            declared = []
            for key in ("ontology_consumed", "domain_facts_consumed",
                        "analysis_findings_consumed", "dar_consumed",
                        "prior_bar_consumed"):
                declared.extend(response_attestation.get(key, []))
            if cid in declared:
                continue
        unknowns.append(f"citation:{cid}")

    # --- Class 1: Table refs ---
    aliases: dict[str, str] = {}  # alias → table
    if conn is not None:
        known_tables = {
            r[0].lower() for r in conn.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
    else:
        known_tables = set()

    for schema, table, alias in _extract_table_refs(sql_stripped):
        if table in cte_names:
            if alias:
                aliases[alias] = table
            continue
        if conn is not None and table not in known_tables:
            unknowns.append(f"table:{table}")
            flagged_tables.add(table)
            continue
        if alias:
            aliases[alias] = table

    # --- Class 3: Function calls (allowlist) ---
    function_tokens: set[str] = set()
    for match in _FUNCTION_PATTERN.finditer(sql_stripped):
        fn = match.group(1).lower()
        function_tokens.add(fn)
        if fn in _SQL_KEYWORDS or fn in _FN_ALLOWLIST:
            continue
        if fn in cte_names or fn in aliases:
            continue
        # CASE WHEN ... END — `case` is a keyword, not a function
        if fn == "case":
            continue
        # Not in allowlist — candidate column in its own clause doesn't
        # use `(`, so this is likely an unknown function OR a subquery
        # expression like `row_number() OVER ...`. Flag if not otherwise
        # recognized.
        # Don't flag — functions are soft: if the SQL executes in DuckDB
        # the mechanical gate catches unknown functions. Keep audit
        # focused on identifier classes 1, 2, 4.

    # --- Class 2: Column refs ---
    if conn is not None:
        known_columns = {
            r[0].lower() for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
            ).fetchall()
        }
        # Also accept SAP data dictionary fields
        sap_fields = conn.execute(
            "SELECT DISTINCT field_name FROM main_seeds.sap_data_dictionary"
        ).fetchall()
        known_columns.update(r[0].lower() for r in sap_fields)
    else:
        known_columns = set()

    # Extract qualified refs first
    qualified_cols: set[str] = set()
    qualified_col_spans: list[tuple[int, int]] = []
    for match in _QUALIFIED_COL_PATTERN.finditer(sql_stripped):
        qualified_col_spans.append((match.start(), match.end()))
    # Also mark output aliases as "qualified" so the prefix
    # (e.g. `alias.field`) isn't re-flagged. Qualified refs where the
    # prefix is an output alias pass through the alias-known branch.
    for match in _QUALIFIED_COL_PATTERN.finditer(sql_stripped):
        prefix = match.group(1).lower()
        col = match.group(2).lower()
        qualified_cols.add(col)
        # Skip schema-qualified table refs handled as tables above
        if prefix in ("main_seeds", "main_staging", "main_vault",
                      "main_marts", "main_obt", "main_knowledge", f"{SOURCE_SCHEMA}",
                      "main", "information_schema"):
            continue
        # If prefix is a known alias / CTE / table, validate column
        is_known_prefix = (
            prefix in aliases
            or prefix in cte_names
            or prefix in known_tables
        )
        if not is_known_prefix:
            unknowns.append(f"alias:{prefix}")
            continue
        # If prefix resolves (via aliases) to a CTE,
        # skip column validation. CTE output columns are defined inside
        # the CTE's SELECT list and cannot be validated against
        # information_schema.columns or sap_data_dictionary. The
        # mechanical gate (DuckDB execute) will catch real column typos.
        prefix_resolves_to_cte = (
            prefix in cte_names
            or aliases.get(prefix) in cte_names
        )
        if prefix_resolves_to_cte:
            continue
        if conn is not None and col not in known_columns:
            unknowns.append(f"column:{prefix}.{col}")

    # Bare identifiers (not preceded by `.`) — skip positions already
    # consumed by citation IDs or qualified column refs.
    def _pos_in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
        for start, end in spans:
            if start <= pos < end:
                return True
        return False

    for match in _BARE_IDENT_PATTERN.finditer(sql_stripped):
        pos = match.start()
        tok = match.group()
        tok_lower = tok.lower()
        # Skip token positions that are INSIDE a citation ID or qualified ref
        if _pos_in_spans(pos, citation_id_spans):
            continue
        if _pos_in_spans(pos, qualified_col_spans):
            continue
        # Skip SQL keywords / types
        if tok_lower in _SQL_KEYWORDS:
            continue
        # Skip function names (has `(` after it)
        if tok_lower in function_tokens:
            continue
        # Skip CTE names and aliases
        if tok_lower in cte_names or tok_lower in aliases:
            continue
        # Skip SELECT-list output aliases (AS-bound names)
        if tok_lower in output_aliases:
            continue
        # Skip known tables (they were validated as class 1)
        if tok_lower in known_tables:
            continue
        # Skip tables already flagged in class 1 — no duplicate flagging
        if tok_lower in flagged_tables:
            continue
        # Skip all-digit numeric tokens (literals masquerading as idents)
        if tok.isdigit():
            continue
        # Skip common single-char or two-char aliases like 'e', 'a', 'b'
        if len(tok) <= 2:
            continue
        # Must be a known column
        if conn is not None and tok_lower not in known_columns:
            unknowns.append(f"column:{tok}")

    return sorted(set(unknowns))


# Backward-compat shim: _grep_audit name kept for existing callers but
# delegates to _ast_audit. The env-var threshold was removed.
def _grep_audit(sql: str, bundle_text: str,
                response_attestation: Optional[dict] = None,
                conn: Optional["duckdb.DuckDBPyConnection"] = None,
                scope_tables: Optional[list[str]] = None) -> list[str]:
    return _ast_audit(sql, bundle_text, response_attestation, conn, scope_tables)


def _pre_injection_audit(
    conn: duckdb.DuckDBPyConnection,
    bundle: ContextBundle,
    scope_tables: list[str],
    term_id: str,
) -> Optional[str]:
    """Safeguard 1 — expected-vs-actual bundle diff.

    Catches known_issue #26-class silent-empty-loader bugs: a layer loader
    that swallowed a query error and returned 0 rows would have produced
    a bundle that passes ContextDegradedError (layer is not "all sources
    empty" — just this one was) but is missing content the iteration loop
    assumes is present.

    Returns None if no drift detected. Returns a short description string
    if expected-vs-actual counts diverge materially.
    """
    # Expected: business layer term notes present (LLM reasons over these)
    term_row = conn.execute(
        "SELECT notes, definition FROM main_seeds.business_glossary WHERE id = ?",
        [term_id],
    ).fetchone()
    expected_term_notes = bool(term_row and (term_row[0] or term_row[1]))
    actual_term_notes_in_bundle = (
        bool(bundle.business_layer_text and len(bundle.business_layer_text) > 100)
        or "TERM_NOTES:" in (bundle.formatted_prompt or "")
        or "term_definition" in (bundle.formatted_prompt or "").lower()
    )

    if expected_term_notes and not actual_term_notes_in_bundle:
        return (
            f"business layer missing term content for {term_id}: "
            f"glossary row exists with notes/definition but bundle business_layer_text "
            f"is empty or below 100 chars"
        )

    # Expected: ontology layer non-empty if existing_models would match scope
    # (at least one main_* model touches a scope table via dbt_column_lineage)
    if scope_tables and bundle.layer_summary.get("ontology", 0) == 0:
        # Check if ontology would have been non-empty
        existing_model_count = conn.execute(
            """
            SELECT COUNT(DISTINCT target_model)
            FROM main_seeds.s2t_mapping
            WHERE source_table IN (
                SELECT UNNEST(?)
            )
            """,
            [scope_tables],
        ).fetchone()
        if existing_model_count and existing_model_count[0] > 0:
            return (
                f"ontology layer empty but s2t_mapping has "
                f"{existing_model_count[0]} target_model(s) for scope {scope_tables}"
            )

    return None


# ─── LLM call wrappers — four prompts (iteration/reflection/finalization + extraction) ──────


def _call_extraction(term_name: str, term_definition: str, term_notes: str) -> dict:
    """Haiku on preflight. No bundle (task-tail-only)."""
    tmpl = _load_prompt("term_condition_extraction_prompt.md")
    user = _fill_template(tmpl, {
        "term_name": term_name,
        "term_definition": term_definition or "",
        "term_notes": term_notes or "",
    })
    return _call_piece8_prompt(
        system_prompt="You extract atomic term conditions from a business-term's text.",
        bundle=None,
        task_prompt=user,
        model=MODEL_HAIKU,
        max_tokens=2000,
        preflight_only=True,
    )


def _call_iteration(
    bundle: ContextBundle,
    term_def: str,
    term_notes: str,
    term_conditions: list[dict],
    scope_tables: list[str],
    prior_iterations_summary: str,
) -> dict:
    """4-breakpoint call. Bundle layers via cache blocks;
    term + conditions + scope + prior-iterations summary go in task tail."""
    tmpl = _load_prompt("term_injection_iteration_prompt.md")
    # Task tail: only the non-bundle parts. Bundle layers travel via BP2/3/4.
    task_tail = _fill_template(tmpl, {
        "bundle": "(bundle delivered via cache breakpoints; see BP2/3/4)",
        "term_definition": term_def or "",
        "term_notes": term_notes or "",
        "term_conditions": json.dumps(term_conditions, indent=2),
        "scope_tables": json.dumps(scope_tables),
        "prior_iterations_summary": prior_iterations_summary or "(iteration 1 — no priors)",
    })
    return _call_piece8_prompt(
        system_prompt="You propose the next SQL candidate for a term-scoped reasoning loop.",
        bundle=bundle,
        task_prompt=task_tail,
        model=MODEL_SONNET,
        max_tokens=4000,
    )


def _call_reflection(
    bundle: ContextBundle,
    current_sql: str,
    current_result_summary: dict,
    gates: dict,
    term_conditions: list[dict],
    prior_iterations_summary: str,
) -> dict:
    """4-breakpoint call; bundle reused from iteration (BP1-4 cache hit)."""
    tmpl = _load_prompt("term_injection_reflection_prompt.md")
    task_tail = _fill_template(tmpl, {
        "current_sql": current_sql,
        "current_result_summary": json.dumps(current_result_summary, default=str, indent=2),
        "gates_result": json.dumps(gates),
        "term_conditions": json.dumps(term_conditions, indent=2),
        "prior_iterations_summary": prior_iterations_summary or "(iteration 1 — no priors)",
    })
    return _call_piece8_prompt(
        system_prompt="You reflect on a SQL candidate and score its alignment with term conditions.",
        bundle=bundle,
        task_prompt=task_tail,
        model=MODEL_SONNET,
        max_tokens=3000,
    )


def _union_attestation_from_trace_and_finalize(
    iteration_trace: list[dict],
    finalize: dict,
    field: str,
) -> list:
    """Unify attestation for BAR persistence.

    Pulls citations from three sources, in order, with order-preserving dedup:
      1. The LLM's finalization response (`finalize.<field>`).
      2. Each iteration's threaded gates_result.<field> (Layer A/B today).
      3. Each iteration's response_attestation echo (`trace[N].response.<field>`).

    Source 3 was added in Option B Phase 4 to support iteration-only
    attestation fields like `bridge_coverage_consulted`: post-Phase-4-Gap-C,
    the finalization LLM doesn't echo it (semantic split), and only iteration
    threads it via gates_result for Layer A/B. The response_attestation echo
    (populated by the Gap B comprehension over ATTESTATION_FIELDS_ITERATION)
    is the only available source for iteration-only fields.

    Guarantees BAR.<field> is a superset of every iteration's attestation
    so iter-0 citations can't silently drop when the LLM's finalize response
    omits them or when the field is iteration-only by design.
    """
    seen: set = set()
    out: list = []
    for v in (finalize.get(field, []) or []):
        if v not in seen:
            seen.add(v)
            out.append(v)
    for it in iteration_trace or []:
        gates = it.get("gates_result") or {}
        for v in (gates.get(field, []) or []):
            if v not in seen:
                seen.add(v)
                out.append(v)
        resp = it.get("response") or {}
        for v in (resp.get(field, []) or []):
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out


def _call_finalization(bundle: ContextBundle, iteration_trace: list[dict],
                       convergence_reason: str) -> dict:
    """4-breakpoint call; bundle + session-trace cache hit."""
    tmpl = _load_prompt("term_injection_finalization_prompt.md")
    task_tail = _fill_template(tmpl, {
        "iteration_trace": json.dumps(iteration_trace, default=str, indent=2),
        "convergence_reason": convergence_reason,
    })
    return _call_piece8_prompt(
        system_prompt="You write the final BAR row audit content from an iteration trace.",
        bundle=bundle,
        task_prompt=task_tail,
        model=MODEL_SONNET,
        max_tokens=3000,
    )


# ─── Step 11a fallback — synthesize finalization without LLM call ───


def _synthesize_finalization_from_trace(
    iteration_trace: list[dict],
    convergence_reason: str,
) -> dict:
    """Deterministic finalization when remaining budget < projection.

    Reads the last iteration's gates_result + reflection output and
    produces the BAR row's audit fields without a finalization LLM call.
    Pure function; no I/O.
    """
    last = iteration_trace[-1] if iteration_trace else {}
    alignment = last.get("semantic_alignment_score", 0)
    condition_assessment = last.get("term_condition_assessment", [])
    missed = [c["condition"] for c in condition_assessment if c.get("status") == "MISSED"]
    covered = [c["condition"] for c in condition_assessment if c.get("status") == "COVERED"]

    # Union attestation across iterations so the synthesized
    # fallback produces the same 7-field shape as the LLM finalization
    # path. Preserves attestation for BAR persistence even when the
    # finalization LLM call is skipped due to budget exhaustion.
    def _union_field(field: str) -> list:
        seen: set = set()
        out: list = []
        for it in iteration_trace:
            resp = it.get("response") or {}
            for v in resp.get(field, []) or []:
                if v not in seen:
                    seen.add(v)
                    out.append(v)
            # Also read from gates_result for Layer A/B
            # attestation. Trace entries don't carry the raw "response"
            # dict; iter_sm_consumed / iter_dsm_consumed are threaded
            # into gates_result by _trace_entry instead.
            if field in ("semantic_model_consumed",
                         "dbt_semantic_model_consumed"):
                gates = it.get("gates_result") or {}
                for v in gates.get(field, []) or []:
                    if v not in seen:
                        seen.add(v)
                        out.append(v)
        return out

    return {
        "ontology_consumed": _union_field("ontology_consumed"),
        "domain_facts_consumed": _union_field("domain_facts_consumed"),
        "analysis_findings_consumed": _union_field("analysis_findings_consumed"),
        "dar_consumed": _union_field("dar_consumed"),
        "prior_bar_consumed": _union_field("prior_bar_consumed"),
        "semantic_model_consumed": _union_field("semantic_model_consumed"),
        "dbt_semantic_model_consumed": _union_field("dbt_semantic_model_consumed"),
        "final_metric_value": None,
        "final_metric_unit": "",
        "final_metric_interpretation": (
            f"Synthesized from iteration trace (finalization skipped — budget). "
            f"Last iteration alignment={alignment}; convergence={convergence_reason}."
        ),
        "term_conditions_covered": covered,
        "term_conditions_missed": missed,
        "confidence_rationale": (
            f"Auto-synthesized from iteration trace due to budget exhaustion "
            f"before finalization LLM call. Convergence: {convergence_reason}."
        ),
        "analyst_review_needed": True,
        "analyst_review_reason": "Budget-exhausted synthesis — analyst should review trace directly",
        "_synthesized": True,
    }


# ─── Step 13 confidence mapping ────────────────────────────────────────


def _compute_confidence(
    convergence_reason: str,
    alignment: int,
    conditions_missed: list,
    analyst_flags_triggered: bool,
) -> tuple[str, bool, str]:
    """Returns (confidence, analyst_review_needed, analyst_review_reason)."""
    failed_enums = {
        "hard_stop_two_consecutive_mechanical",
        "hard_stop_mechanical_regression",
        "hard_stop_preflight_no_scope",
        "hard_stop_preflight_empty_heavy",
        "hard_stop_attestation_failure",
        "hard_stop_citation_audit_failure",
        "hard_stop_finalization_attestation_failure",
        "hard_stop_bundle_fingerprint_drift",
        "hard_stop_orphaned_inprogress",
        "hard_stop_glossary_drift",
        "hard_stop_ontology_collision",
        "hard_stop_bridge_unreachable",   # Option B Phase 2
        "hard_stop_bridge_attestation_missing",   # Option B Phase 3 OQ-3a
    }
    if convergence_reason in failed_enums:
        return (
            "failed",
            True,
            f"Convergence reason: {convergence_reason}",
        )
    if convergence_reason == "converged_soft":
        if alignment >= 95 and not conditions_missed:
            return ("high", False, "")
        if alignment >= 80 and not conditions_missed:
            return ("medium", False, "")
        n = len(conditions_missed)
        return (
            "medium",
            True,
            f"Converged but {n} in-scope condition(s) remain unmet",
        )
    if convergence_reason in {
        "hard_stop_max_iters",
        "hard_stop_oscillation",
        "hard_stop_alignment_regression",
        "hard_stop_complexity_explosion",
        "hard_stop_budget",
        "hard_stop_scope_mismatch",
    }:
        return (
            "low",
            True,
            f"Convergence reason: {convergence_reason}",
        )
    return ("failed", True, f"Unknown convergence reason: {convergence_reason}")


# ─── Step 11b: C5 sourcing-recommendations trigger ──────────────────


_C5_OPTION_B_CONVERGENCE_REASONS: frozenset[str] = frozenset({
    # Single-fire triggers from Option B's data-side gate (Phase 4
    # commit 856eb8e): the gate breaks the iteration loop on first
    # fire, so "consecutive twice" semantics don't apply — one
    # observation is sufficient empirical evidence to invoke C5.
    "hard_stop_bridge_unreachable",
    "hard_stop_bridge_attestation_missing",
})


def _should_fire_c5(
    iteration_trace: list[dict],
    convergence_reason: Optional[str] = None,
) -> bool:
    """C5 fires when EITHER:
      (a) The last two iterations both flagged scope_sanity=no
          (consecutive-twice; conservative shape for soft LLM signals
          producing convergence_reason=hard_stop_scope_mismatch), OR
      (b) convergence_reason is one of Option B's data-side hard-stops
          (single-fire; the gate breaks the iteration loop on first
          fire, so consecutive semantics don't apply — one observation
          of an empirical reachability violation is sufficient).

    Pure function — no side effects, no I/O. Reads the top-level
    `scope_sanity_answer` field on each iteration trace entry (set by
    `_trace_entry` at step 9a from `reflect.scope_sanity_answer`).

    convergence_reason defaults to None so existing callers that don't
    pass it (e.g., the original consecutive-no test fixtures) preserve
    behavior — only path (a) is evaluated. The runner's call site
    passes the runtime convergence_reason so path (b) becomes active.

    Decoupled from convergence-reason logic so future trigger
    refinements (e.g. budget, term-type gating) live here, not in
    the convergence-reason switch.
    """
    # Path (b) — Option B single-fire.
    if convergence_reason in _C5_OPTION_B_CONVERGENCE_REASONS:
        return True
    # Path (a) — consecutive scope_sanity=no.
    if len(iteration_trace) < 2:
        return False
    return (
        iteration_trace[-2].get("scope_sanity_answer") == "no"
        and iteration_trace[-1].get("scope_sanity_answer") == "no"
    )


def _format_reachability_violations_block(
    iteration_trace: list[dict],
    convergence_reason: Optional[str],
) -> str:
    """Render the [REACHABILITY VIOLATIONS] prompt block for C5.

    Returns the formatted block string when the runner is firing C5 due
    to an Option B data-side hard-stop (`hard_stop_bridge_unreachable` or
    `hard_stop_bridge_attestation_missing`); empty string otherwise.

    Field-name asymmetry between the two Option B paths:
      - `hard_stop_bridge_unreachable` populates
        `gates_result["bridge_violations"]` as a list of human-readable
        strings (run_term_injection.py:1742).
      - `hard_stop_bridge_attestation_missing` populates
        `gates_result["violation"]` as a single string
        (run_term_injection.py:1654). Wrapped to a single-element list
        at format time so the bullet renderer is uniform.
    """
    if convergence_reason not in _C5_OPTION_B_CONVERGENCE_REASONS:
        return ""
    last = iteration_trace[-1] if iteration_trace else {}
    gates = last.get("gates_result") or {}
    if convergence_reason == "hard_stop_bridge_unreachable":
        violations = gates.get("bridge_violations") or []
    else:  # hard_stop_bridge_attestation_missing
        single = gates.get("violation") or ""
        violations = [single] if single else []
    if not violations:
        return ""
    bullets = "\n".join(f"- {v}" for v in violations)
    return (
        "[REACHABILITY VIOLATIONS] — empirical evidence from Option B's runtime gate\n\n"
        "The system attempted SQL on this term but hit data-side "
        "reachability violations:\n\n"
        f"{bullets}\n\n"
        "Each violation cites a specific bridge + filter combo that "
        "empirically can't reach the value the SQL filtered on. Use "
        "these to focus your sourcing recommendations on tables that "
        "could resolve the specific reachability gaps (not just any "
        "related SAP table).\n"
    )


def _load_c5_catalog() -> list[dict]:
    """Load full sap_table_catalog.csv. Returns
    list of row dicts with the canonical 9-column schema."""
    catalog_path = _PROJECT_ROOT / "dbt" / "seeds" / "sap_table_catalog.csv"
    with open(catalog_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _format_c5_catalog_block(catalog: list[dict]) -> str:
    """Format catalog rows in the canonical line format:

        - {TABLE} | {description} | release: {STAMP} | key_fields: {F1, F2, ...}

    The cost projection (~$0.01 first-call, $0.003 cached for the
    45-table catalog) was measured against this exact format.
    Don't reformat without re-measuring."""
    lines = []
    for row in catalog:
        lines.append(
            f"- {row['table_name']} | {row.get('brief_description', '')} | "
            f"release: {row.get('source_release_stamp') or 'unknown'} | "
            f"key_fields: {row.get('key_fields', '')}"
        )
    return "\n".join(lines)


def _call_c5_sourcing(
    *,
    term_name: str,
    term_definition: str,
    term_grain: str,
    term_conditions: list[dict],
    confirmed_scope_tables: list[str],
    last_iteration_sql: str,
    last_iteration_reflection: str,
    scope_sanity_rationale: str,
    catalog_block: str,
    reachability_violations: str = "",
) -> dict:
    """Step 11b — C5 sourcing-recommendations LLM call.

    Loads scripts/prompts/c5_sourcing_recommendation_prompt.md, substitutes
    placeholders, calls _call_piece8_prompt with bundle=None (no bundle
    layers, no BP2/3/4 cache — by design C5 uses no prompt caching).
    Returns the standard result dict.

    `reachability_violations` is the pre-formatted [REACHABILITY VIOLATIONS]
    block from `_format_reachability_violations_block()` when the runner
    fires C5 due to an Option B convergence reason; empty string when
    firing via the existing scope_sanity-no path.
    """
    tmpl = _load_prompt("c5_sourcing_recommendation_prompt.md")
    if term_conditions:
        conds_lines = []
        for i, c in enumerate(term_conditions, start=1):
            if isinstance(c, dict):
                conds_lines.append(f"{i}. {json.dumps(c)}")
            else:
                conds_lines.append(f"{i}. {c}")
        conds_fmt = "\n".join(conds_lines)
    else:
        conds_fmt = "(no extracted conditions)"
    task_tail = _fill_template(tmpl, {
        "term_name": term_name,
        "term_definition": term_definition or "(definition unavailable)",
        "term_grain": term_grain or "(grain not specified)",
        "term_conditions": conds_fmt,
        "confirmed_scope_tables": ", ".join(confirmed_scope_tables) if confirmed_scope_tables else "(none)",
        "last_iteration_sql": last_iteration_sql or "(none)",
        "last_iteration_reflection": last_iteration_reflection or "(none)",
        "scope_sanity_rationale": scope_sanity_rationale or "(none)",
        "reachability_violations": reachability_violations,
        "catalog_block": catalog_block,
    })
    return _call_piece8_prompt(
        system_prompt="You are a SAP data architect recommending tables to extend an analytics scope.",
        bundle=None,
        task_prompt=task_tail,
        model=MODEL_SONNET,
        max_tokens=2000,
    )


# ─── Iteration trace summary formatter ───────────────────────────────


def _format_prior_iterations(iteration_trace: list[dict]) -> str:
    """Compact summary for injection into iteration/reflection prompts.
    Bloat analysis: ~3K tokens per prior iteration at this
    format; iteration 5 prompt projected ~64K tokens total.
    """
    if not iteration_trace:
        return ""
    lines = []
    for entry in iteration_trace:
        sql = entry.get("query_sql", "")
        if len(sql) > 1500:
            sql = sql[:1500] + "... [truncated]"
        lines.append(
            f"--- Iteration {entry['iteration_num']} ---\n"
            f"SQL: {sql}\n"
            f"alignment={entry.get('semantic_alignment_score')} "
            f"rubric={entry.get('shadow_rubric_score')} "
            f"scope_sanity={entry.get('scope_sanity_answer')}\n"
            f"Reflection: {entry.get('llm_self_reflection', '')[:400]}"
        )
    return "\n\n".join(lines)


# ─── CLI + mainline ───────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-S2T Reasoning Layer runner — the term-analysis (BAR) "
            "runner: the full multi-turn iteration loop (steps 0a-15)."
        ),
    )
    parser.add_argument("--term-id", required=True, help="Term id from business_glossary.id (e.g. BG001).")
    parser.add_argument("--max-iters", type=int, default=5, help="Max iteration-loop passes. Default 5.")
    parser.add_argument("--budget-cap", type=float, default=1.00,
                        help="Total session LLM spend cap in USD (cached-baseline default).")
    parser.add_argument("--inprogress-ttl-hours", type=int, default=4, help="TTL for orphan sweep.")
    parser.add_argument("--finalization-cost-projection", type=float, default=0.10,
                        help="Reserved budget for finalization. Below this remaining → synthesize.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preflight + placeholder skip, NO iteration loop (no LLM calls).")
    parser.add_argument("--max-tokens", type=int, default=50_000, help="assemble_context max_tokens.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        conn = duckdb.connect(str(DB_PATH))
    except duckdb.IOException as e:
        print(f"INFRASTRUCTURE ERROR: cannot open {DB_PATH}: {e}", file=sys.stderr)
        return 2

    bar_id: Optional[str] = None
    try:
        # ─── Verify term + archive guard (RULE 41, anti-pattern #64) ──
        row = conn.execute(
            "SELECT id, term_name, status, definition, notes FROM main_seeds.business_glossary WHERE id = ?",
            [args.term_id],
        ).fetchone()
        if row is None:
            print(f"ERROR: term_id={args.term_id!r} not found.", file=sys.stderr)
            return 1
        term_id, term_name, term_status, term_def, term_notes = row
        if term_status == "archived":
            print(f"ERROR: term {term_id} is archived; refused per RULE 41.", file=sys.stderr)
            return 1
        print(f"Preflight: term {term_id} {term_name!r} status={term_status}")

        # ─── Step 0b: orphan sweep ───────────────────────────────
        swept = sweep_orphaned_inprogress(conn, term_id=term_id, ttl_hours=args.inprogress_ttl_hours)
        if swept:
            print(f"Step 0b: swept {len(swept)} orphan(s): {swept}")

        # ─── Step 0a: concurrency check post-sweep ───────────────
        remaining = conn.execute(
            f"SELECT id, inprogress_since_utc FROM {BAR_TABLE_FQN} "
            "WHERE business_term_id = ? AND status = 'in_progress'",
            [term_id],
        ).fetchall()
        if remaining:
            other_id, other_since = remaining[0]
            print(f"ERROR: {term_id} already has in_progress run {other_id} since {other_since}.",
                  file=sys.stderr)
            return 1

        if args.dry_run:
            print(f"DRY RUN: preflight OK for {term_id}. No placeholder, no iterations.")
            return 0

        # ─── Step 1: placeholder BAR ─────────────────────────────
        bar_id = write_bar_row(conn, business_term_id=term_id, status="in_progress")
        print(f"Step 1: wrote placeholder BAR {bar_id}")

        # ─── Step 2: scope resolution ────────────────────────────
        # Reuses assemble_context's internal resolve_scope via its
        # scope_resolution field — no external scope_tables passed.
        try:
            bundle = assemble_context(
                purpose="pre_s2t_reasoning",
                term_id=term_id,
                max_tokens=args.max_tokens,
                strict=True,
                include_debug_metadata=True,
                conn=conn,  # thread conn to avoid config mismatch
            )
        except ContextDegradedError as e:
            _finalize_failed(
                conn, bar_id, term_id,
                convergence_reason="hard_stop_preflight_empty_heavy",
                rationale=f"HEAVY layer empty: {e.layer}. Reason: {e.reason}",
            )
            return 1
        except Exception as e:
            # Scope resolution failure surfaces here (Step 2 empty scope).
            msg = str(e)
            if "scope" in msg.lower() and "empty" in msg.lower():
                _finalize_failed(
                    conn, bar_id, term_id,
                    convergence_reason="hard_stop_preflight_no_scope",
                    rationale=f"Scope resolution returned empty: {msg}",
                )
                return 1
            raise

        scope_tables = bundle.scope_resolution.get("resolved_tables", [])
        bundle_fingerprint = bundle.debug.get("schema_fingerprint") if bundle.debug else "unknown"
        print(f"Step 3: bundle {bundle.token_count} tokens, scope={scope_tables}")

        # ─── Safeguard 1: pre-injection context audit ────────────────
        # Expected-vs-actual diff between the helper's reported layer
        # summary and independent counts queried from DuckDB. Catches
        # known_issue #26-class silent-empty-loader bugs before they
        # corrupt the iteration loop.
        audit_drift = _pre_injection_audit(conn, bundle, scope_tables, term_id)
        if audit_drift:
            _finalize_failed(
                conn, bar_id, term_id,
                convergence_reason="hard_stop_preflight_empty_heavy",
                rationale=f"Pre-injection audit detected drift: {audit_drift}",
            )
            return 1

        # ─── Step 4: bundle_fingerprint + drift probe baseline ────
        # Capture baseline probe for per-iteration
        # drift detection at step 6a0 (inside the loop below).
        drift_probe_baseline = compute_drift_probe(conn, scope_tables, term_id)
        baseline_glossary_hash = hash_glossary_row(conn, term_id)

        # ─── Step 5: preflight condition extraction ──────────────
        extract_result = _call_extraction(term_name, term_def, term_notes)
        if extract_result["error"]:
            _finalize_failed(conn, bar_id, term_id,
                             convergence_reason="hard_stop_preflight_empty_heavy",
                             rationale=f"Extraction LLM error: {extract_result['error']}")
            return 1
        term_conditions = extract_result["response"].get("conditions", [])
        print(f"Step 5: extracted {len(term_conditions)} term conditions")

        # ─── Step 5a: budget init (count preflight against cap) ──
        # Track all four token-accounting fields per call.
        budget_used_usd = extract_result["cost_usd"]
        total_input_tokens = extract_result["input_tokens"]
        total_output_tokens = extract_result["output_tokens"]
        total_cache_read_tokens = extract_result.get("cache_read_input_tokens", 0)
        total_cache_creation_tokens = extract_result.get("cache_creation_input_tokens", 0)
        if budget_used_usd >= args.budget_cap:
            _finalize_failed(conn, bar_id, term_id,
                             convergence_reason="hard_stop_budget",
                             rationale="Preflight extraction alone exhausted budget_cap")
            return 1

        # ─── Iteration loop ──────────────────────────────────────────
        iteration_trace: list[dict] = []
        prior_sql_hashes: list[str] = []
        prior_alignment: Optional[int] = None
        consecutive_scope_no_count = 0
        convergence_reason: Optional[str] = None

        for iter_num in range(1, args.max_iters + 1):
            prior_summary = _format_prior_iterations(iteration_trace)

            # Step 6a0: drift probe continuity check
            # Two-class branching: glossary-row drift → hard_stop_glossary_drift
            # (checklist obsolete); bundle fingerprint drift (non-glossary) →
            # hard_stop_bundle_fingerprint_drift.
            current_probe = compute_drift_probe(conn, scope_tables, term_id)
            if current_probe != drift_probe_baseline:
                # Probe mismatch — rebuild bundle to verify real drift vs
                # probe false positive (seed touched, content unchanged).
                try:
                    fresh_bundle = assemble_context(
                        purpose="pre_s2t_reasoning",
                        term_id=term_id,
                        max_tokens=args.max_tokens,
                        strict=False,   # tolerate drift, don't fail here
                        include_debug_metadata=True,
                        conn=conn,  # thread conn (config-mismatch fix)
                    )
                    fresh_fp = fresh_bundle.debug.get("schema_fingerprint") if fresh_bundle.debug else "unknown"
                except Exception:
                    fresh_fp = "rebuild_failed"

                if fresh_fp != bundle_fingerprint:
                    new_glossary_hash = hash_glossary_row(conn, term_id)
                    if new_glossary_hash != baseline_glossary_hash:
                        convergence_reason = "hard_stop_glossary_drift"
                    else:
                        convergence_reason = "hard_stop_bundle_fingerprint_drift"
                    break
                else:
                    # Probe false positive (seed mtime churn, content unchanged)
                    drift_probe_baseline = current_probe

            # Step 6a: iteration prompt
            # Track accumulated tokens for hard-stop trace entries
            # BEFORE the LLM call, so paths that fail BEFORE accumulation still
            # produce trace. Initialize to 0 so pre-LLM paths don't KeyError.
            iter_cache_read = 0
            iter_cache_creation = 0
            iter_input_tokens = 0
            iter_output_tokens = 0
            iter_cost_usd = 0.0
            # Pre-LLM defaults for per-iteration attestation
            # so hard-stop paths firing BEFORE propose is parsed still have
            # safe values to thread into gates_result.
            iter_sm_consumed: list = []
            iter_dsm_consumed: list = []
            # Fix: preserve all 7 attestation fields under "response" so the
            # synthesis path (_union_field at L1060+) finds them when the
            # finalization LLM call is skipped due to budget projection.
            # Pre-#95-fix-followup, only sm_consumed + dsm_consumed survived
            # trace construction; the other 5 (ontology, domain_facts,
            # analysis_findings, dar, prior_bar) were silently dropped from
            # the trace, causing the synthesis to union as [] regardless of
            # what the iteration LLM emitted (BG027 BAR-00002 case).
            iter_response_attestation: dict = {}
            sql = ""

            it_result = _call_iteration(
                bundle, term_def, term_notes,
                term_conditions, scope_tables, prior_summary,
            )
            if it_result["error"]:
                convergence_reason = "hard_stop_attestation_failure"
                # Append trace before break so iterations_count>0
                iteration_trace.append(_trace_entry(
                    iter_num, "", [], 0,
                    {"compile": "not_evaluated", "run": "not_evaluated",
                     "llm_call": "fail", "error": it_result.get("error")},
                    None, None, None, None, bundle,
                    iter_input_tokens=0, iter_output_tokens=0,
                    iter_cache_read_tokens=0, iter_cache_creation_tokens=0,
                    iter_cost_usd=0.0,
                semantic_model_consumed=iter_sm_consumed,
                dbt_semantic_model_consumed=iter_dsm_consumed,
                response_attestation=iter_response_attestation,
                ))
                break

            propose = it_result["response"]
            budget_used_usd += it_result["cost_usd"]
            total_input_tokens += it_result["input_tokens"]
            total_output_tokens += it_result["output_tokens"]
            total_cache_read_tokens += it_result.get("cache_read_input_tokens", 0)
            total_cache_creation_tokens += it_result.get("cache_creation_input_tokens", 0)
            iter_cache_read = it_result.get("cache_read_input_tokens", 0)
            iter_cache_creation = it_result.get("cache_creation_input_tokens", 0)
            iter_input_tokens = it_result.get("input_tokens", 0)
            iter_output_tokens = it_result.get("output_tokens", 0)
            iter_cost_usd = it_result.get("cost_usd", 0.0)

            # Extract per-iteration attestation for trace
            # persistence. iter_sm_consumed / iter_dsm_consumed go into
            # gates_result (back-compat with the earlier BAR schema).
            # iter_response_attestation captures every iteration-contract
            # attestation field under "response" so the synthesis path's
            # _union_field finds them.
            #
            # Option B Phase 4 Gap B refactor: build the dict generically
            # by iterating ATTESTATION_FIELDS_ITERATION so future
            # attestation field additions need only update the constant
            # (and the iteration prompt directive that teaches the LLM to
            # emit the field) — no second edit here.
            if isinstance(propose, dict):
                iter_sm_consumed = propose.get("semantic_model_consumed", []) or []
                iter_dsm_consumed = propose.get("dbt_semantic_model_consumed", []) or []
                iter_response_attestation = {
                    field: (propose.get(field, []) or [])
                    for field in ATTESTATION_FIELDS_ITERATION
                }
            else:
                iter_sm_consumed = []
                iter_dsm_consumed = []
                iter_response_attestation = {}

            if not attestation_complete(propose, ATTESTATION_FIELDS_ITERATION):
                convergence_reason = "hard_stop_attestation_failure"
                # Append trace before break
                iteration_trace.append(_trace_entry(
                    iter_num, propose.get("query_sql", "") if isinstance(propose, dict) else "",
                    [], 0,
                    {"compile": "not_evaluated", "run": "not_evaluated",
                     "attestation": "fail"},
                    None, None, None, None, bundle,
                    iter_input_tokens=iter_input_tokens, iter_output_tokens=iter_output_tokens,
                    iter_cache_read_tokens=iter_cache_read, iter_cache_creation_tokens=iter_cache_creation,
                    iter_cost_usd=iter_cost_usd,
                semantic_model_consumed=iter_sm_consumed,
                dbt_semantic_model_consumed=iter_dsm_consumed,
                response_attestation=iter_response_attestation,
                ))
                break

            # Option B Phase 3 (OQ-3a Option β) — conditional
            # bridge_coverage_consulted check. When DARs exist for
            # scope, the LLM must cite at least one. Empty list with
            # DARs present means LLM ignored available evidence.
            from _bridge_coverage_gate import (
                _check_bridge_coverage_attestation,
            )
            bc_att_ok, bc_att_msg = _check_bridge_coverage_attestation(
                propose, conn, scope_tables,
            )
            if not bc_att_ok:
                convergence_reason = "hard_stop_bridge_attestation_missing"
                iteration_trace.append(_trace_entry(
                    iter_num, propose.get("query_sql", "") if isinstance(propose, dict) else "",
                    [], 0,
                    {"compile": "not_evaluated", "run": "not_evaluated",
                     "bridge_coverage_attestation": "fail",
                     "violation": bc_att_msg or ""},
                    None, None, None, None, bundle,
                    iter_input_tokens=iter_input_tokens, iter_output_tokens=iter_output_tokens,
                    iter_cache_read_tokens=iter_cache_read, iter_cache_creation_tokens=iter_cache_creation,
                    iter_cost_usd=iter_cost_usd,
                semantic_model_consumed=iter_sm_consumed,
                dbt_semantic_model_consumed=iter_dsm_consumed,
                response_attestation=iter_response_attestation,
                ))
                break

            sql = propose.get("query_sql", "")
            sql_hash_cur = _sql_hash(sql)

            # Step 6c: oscillation detector
            if iter_num >= 3 and sql_hash_cur == prior_sql_hashes[iter_num - 3]:
                convergence_reason = "hard_stop_oscillation"
                # Append trace before break
                iteration_trace.append(_trace_entry(
                    iter_num, sql, [], 0,
                    {"compile": "not_evaluated", "run": "not_evaluated",
                     "oscillation": "detected", "sql_hash": sql_hash_cur},
                    None, None, None, None, bundle,
                    iter_input_tokens=iter_input_tokens, iter_output_tokens=iter_output_tokens,
                    iter_cache_read_tokens=iter_cache_read, iter_cache_creation_tokens=iter_cache_creation,
                    iter_cost_usd=iter_cost_usd,
                semantic_model_consumed=iter_sm_consumed,
                dbt_semantic_model_consumed=iter_dsm_consumed,
                response_attestation=iter_response_attestation,
                ))
                break

            # Step 6d: AST-based citation audit (Safeguard 4).
            unknown_refs = _ast_audit(sql, bundle.formatted_prompt, propose,
                                      conn=conn, scope_tables=scope_tables)
            if unknown_refs:
                print(f"  iter {iter_num} ast_audit unknowns ({len(unknown_refs)}): "
                      f"{unknown_refs[:10]}{'...' if len(unknown_refs) > 10 else ''}")
                convergence_reason = "hard_stop_citation_audit_failure"
                # Append trace before break
                iteration_trace.append(_trace_entry(
                    iter_num, sql, [], 0,
                    {"compile": "not_evaluated", "run": "not_evaluated",
                     "citation_audit": "fail", "unknown_refs": unknown_refs},
                    None, None, None, None, bundle,
                    iter_input_tokens=iter_input_tokens, iter_output_tokens=iter_output_tokens,
                    iter_cache_read_tokens=iter_cache_read, iter_cache_creation_tokens=iter_cache_creation,
                    iter_cost_usd=iter_cost_usd,
                semantic_model_consumed=iter_sm_consumed,
                dbt_semantic_model_consumed=iter_dsm_consumed,
                response_attestation=iter_response_attestation,
                ))
                break

            # Step 6d.1: ontology collision check.
            ontology_consumed_list = propose.get("ontology_consumed", [])
            collisions = _ontology_collision_check(sql, ontology_consumed_list)
            if collisions:
                convergence_reason = "hard_stop_ontology_collision"
                # Append trace before break
                iteration_trace.append(_trace_entry(
                    iter_num, sql, [], 0,
                    {"compile": "not_evaluated", "run": "not_evaluated",
                     "ontology_collision": "fail", "collisions": collisions},
                    None, None, None, None, bundle,
                    iter_input_tokens=iter_input_tokens, iter_output_tokens=iter_output_tokens,
                    iter_cache_read_tokens=iter_cache_read, iter_cache_creation_tokens=iter_cache_creation,
                    iter_cost_usd=iter_cost_usd,
                semantic_model_consumed=iter_sm_consumed,
                dbt_semantic_model_consumed=iter_dsm_consumed,
                response_attestation=iter_response_attestation,
                ))
                break

            # Option B Phase 2 — bridge_coverage gate (Component 2). Refuses
            # SQL filtering on values empirically unreachable through chosen
            # joins per bridge_coverage_by_filter DARs. Soft fall-through
            # when DARs absent / SQL unparseable (status="skipped_*").
            from _bridge_coverage_gate import bridge_coverage_gate
            bc_passed, bc_violations, bc_status = bridge_coverage_gate(
                sql, scope_tables, conn,
            )
            if not bc_passed:
                convergence_reason = "hard_stop_bridge_unreachable"
                iteration_trace.append(_trace_entry(
                    iter_num, sql, [], 0,
                    {"compile": "not_evaluated", "run": "not_evaluated",
                     "bridge_coverage": bc_status,
                     "bridge_violations": bc_violations},
                    None, None, None, None, bundle,
                    iter_input_tokens=iter_input_tokens, iter_output_tokens=iter_output_tokens,
                    iter_cache_read_tokens=iter_cache_read, iter_cache_creation_tokens=iter_cache_creation,
                    iter_cost_usd=iter_cost_usd,
                semantic_model_consumed=iter_sm_consumed,
                dbt_semantic_model_consumed=iter_dsm_consumed,
                response_attestation=iter_response_attestation,
                ))
                break

            # Step 7: mechanical gate (DuckDB execute; compile+run collapsed)
            run_result = None
            compile_ok = False
            run_ok = False
            row_count = 0
            sample_rows: list = []
            try:
                run_result = conn.execute(sql).fetchall()
                compile_ok = True
                run_ok = True
                row_count = len(run_result) if run_result else 0
                sample_rows = [list(r) for r in (run_result[:3] if run_result else [])]
            except Exception as exc:
                compile_ok = False
                run_ok = False
                print(f"  iter {iter_num} mechanical fail: {exc}")

            gates = {
                "compile": "pass" if compile_ok else "fail",
                "run": "pass" if run_ok else "fail",
                "row_count_ok": None,  # populated post-reflection via justified_zero
                "bridge_coverage": bc_status,  # Option B Phase 2 — audit trail
            }

            # Step 7a/7b: mechanical regression / two-consecutive
            if iter_num >= 2:
                prior_compile = iteration_trace[-1]["gates_result"]["compile"]
                if gates["compile"] == "fail" and prior_compile == "pass":
                    convergence_reason = "hard_stop_mechanical_regression"
                    iteration_trace.append(_trace_entry(iter_num, sql, sample_rows, row_count, gates,
                                                        None, None, None, None, bundle,
                                                        iter_input_tokens=iter_input_tokens,
                                                        iter_output_tokens=iter_output_tokens,
                                                        iter_cache_read_tokens=iter_cache_read,
                                                        iter_cache_creation_tokens=iter_cache_creation,
                                                        iter_cost_usd=iter_cost_usd, semantic_model_consumed=iter_sm_consumed, dbt_semantic_model_consumed=iter_dsm_consumed, response_attestation=iter_response_attestation))
                    break
                if gates["compile"] == "fail" and prior_compile == "fail":
                    convergence_reason = "hard_stop_two_consecutive_mechanical"
                    iteration_trace.append(_trace_entry(iter_num, sql, sample_rows, row_count, gates,
                                                        None, None, None, None, bundle,
                                                        iter_input_tokens=iter_input_tokens,
                                                        iter_output_tokens=iter_output_tokens,
                                                        iter_cache_read_tokens=iter_cache_read,
                                                        iter_cache_creation_tokens=iter_cache_creation,
                                                        iter_cost_usd=iter_cost_usd, semantic_model_consumed=iter_sm_consumed, dbt_semantic_model_consumed=iter_dsm_consumed, response_attestation=iter_response_attestation))
                    break

            # Step 8: reflection
            result_summary = {
                "row_count": row_count,
                "sample_rows": sample_rows,
            }
            refl_result = _call_reflection(bundle, sql, result_summary, gates, term_conditions,
                                           _format_prior_iterations(iteration_trace))
            if refl_result["error"]:
                convergence_reason = "hard_stop_attestation_failure"
                # Append trace before break
                iteration_trace.append(_trace_entry(
                    iter_num, sql, sample_rows, row_count,
                    {**gates, "reflection_llm": "fail", "error": refl_result.get("error")},
                    None, None, None, None, bundle,
                    iter_input_tokens=iter_input_tokens, iter_output_tokens=iter_output_tokens,
                    iter_cache_read_tokens=iter_cache_read, iter_cache_creation_tokens=iter_cache_creation,
                    iter_cost_usd=iter_cost_usd,
                semantic_model_consumed=iter_sm_consumed,
                dbt_semantic_model_consumed=iter_dsm_consumed,
                response_attestation=iter_response_attestation,
                ))
                break

            reflect = refl_result["response"]
            budget_used_usd += refl_result["cost_usd"]
            total_input_tokens += refl_result["input_tokens"]
            total_output_tokens += refl_result["output_tokens"]
            total_cache_read_tokens += refl_result.get("cache_read_input_tokens", 0)
            total_cache_creation_tokens += refl_result.get("cache_creation_input_tokens", 0)
            # Per-iteration cache telemetry (sum iter + reflect for this iteration)
            iter_cache_read += refl_result.get("cache_read_input_tokens", 0)
            iter_cache_creation += refl_result.get("cache_creation_input_tokens", 0)
            iter_input_tokens += refl_result.get("input_tokens", 0)
            iter_output_tokens += refl_result.get("output_tokens", 0)
            iter_cost_usd += refl_result.get("cost_usd", 0.0)

            if not attestation_complete(reflect, ATTESTATION_FIELDS_FINALIZATION):
                convergence_reason = "hard_stop_attestation_failure"
                # Append trace before break
                iteration_trace.append(_trace_entry(
                    iter_num, sql, sample_rows, row_count,
                    {**gates, "reflection_attestation": "fail"},
                    None, None, None, None, bundle,
                    iter_input_tokens=iter_input_tokens, iter_output_tokens=iter_output_tokens,
                    iter_cache_read_tokens=iter_cache_read, iter_cache_creation_tokens=iter_cache_creation,
                    iter_cost_usd=iter_cost_usd,
                semantic_model_consumed=iter_sm_consumed,
                dbt_semantic_model_consumed=iter_dsm_consumed,
                response_attestation=iter_response_attestation,
                ))
                break

            alignment = int(reflect.get("semantic_alignment_score", 0))
            rubric_emitted = int(reflect.get("shadow_rubric_score", 0))
            rubric_breakdown = reflect.get("shadow_rubric_breakdown", {})
            rubric_recomputed = sum(int(v) for v in rubric_breakdown.values()) if rubric_breakdown else 0
            rubric_drift = abs(rubric_emitted - rubric_recomputed) > 3
            justified_zero = bool(reflect.get("justified_zero", False))
            scope_sanity = reflect.get("scope_sanity_answer", "uncertain")

            # Step 9 v3.1 P9: populate row_count_ok from reflection
            gates["row_count_ok"] = (row_count > 0) or justified_zero

            # Step 9a: scope-sanity detector
            if scope_sanity == "no":
                consecutive_scope_no_count += 1
                if consecutive_scope_no_count >= 2:
                    convergence_reason = "hard_stop_scope_mismatch"
                    iteration_trace.append(_trace_entry(iter_num, sql, sample_rows, row_count, gates,
                                                        alignment, rubric_recomputed, rubric_drift,
                                                        scope_sanity, bundle, reflect,
                                                iter_input_tokens=iter_input_tokens,
                                                iter_output_tokens=iter_output_tokens,
                                                iter_cache_read_tokens=iter_cache_read,
                                                iter_cache_creation_tokens=iter_cache_creation,
                                                iter_cost_usd=iter_cost_usd, semantic_model_consumed=iter_sm_consumed, dbt_semantic_model_consumed=iter_dsm_consumed, response_attestation=iter_response_attestation))
                    break
            else:
                consecutive_scope_no_count = 0

            # Step 9b: alignment regression detector
            if prior_alignment is not None and alignment < prior_alignment - 10:
                convergence_reason = "hard_stop_alignment_regression"
                iteration_trace.append(_trace_entry(iter_num, sql, sample_rows, row_count, gates,
                                                    alignment, rubric_recomputed, rubric_drift,
                                                    scope_sanity, bundle, reflect,
                                                iter_input_tokens=iter_input_tokens,
                                                iter_output_tokens=iter_output_tokens,
                                                iter_cache_read_tokens=iter_cache_read,
                                                iter_cache_creation_tokens=iter_cache_creation,
                                                iter_cost_usd=iter_cost_usd, semantic_model_consumed=iter_sm_consumed, dbt_semantic_model_consumed=iter_dsm_consumed, response_attestation=iter_response_attestation))
                break

            # Step 9c: complexity explosion detector
            if iter_num >= 2:
                prior_sql = iteration_trace[-1]["query_sql"]
                if len(sql) > 2 * len(prior_sql):
                    convergence_reason = "hard_stop_complexity_explosion"
                    iteration_trace.append(_trace_entry(iter_num, sql, sample_rows, row_count, gates,
                                                        alignment, rubric_recomputed, rubric_drift,
                                                        scope_sanity, bundle, reflect,
                                                iter_input_tokens=iter_input_tokens,
                                                iter_output_tokens=iter_output_tokens,
                                                iter_cache_read_tokens=iter_cache_read,
                                                iter_cache_creation_tokens=iter_cache_creation,
                                                iter_cost_usd=iter_cost_usd, semantic_model_consumed=iter_sm_consumed, dbt_semantic_model_consumed=iter_dsm_consumed, response_attestation=iter_response_attestation))
                    break

            # Step 10: trace append
            iteration_trace.append(_trace_entry(iter_num, sql, sample_rows, row_count, gates,
                                                alignment, rubric_recomputed, rubric_drift,
                                                scope_sanity, bundle, reflect,
                                                iter_input_tokens=iter_input_tokens,
                                                iter_output_tokens=iter_output_tokens,
                                                iter_cache_read_tokens=iter_cache_read,
                                                iter_cache_creation_tokens=iter_cache_creation,
                                                iter_cost_usd=iter_cost_usd, semantic_model_consumed=iter_sm_consumed, dbt_semantic_model_consumed=iter_dsm_consumed, response_attestation=iter_response_attestation))
            prior_sql_hashes.append(sql_hash_cur)
            prior_alignment = alignment

            # Step 10a: budget cap
            if budget_used_usd >= args.budget_cap:
                convergence_reason = "hard_stop_budget"
                break

            # Step 11: soft-stop check (all must hold)
            prior_entry_sql = iteration_trace[-2]["query_sql"] if iter_num >= 2 else None
            stability = _sql_stability(sql, prior_entry_sql)
            if (gates["compile"] == "pass"
                    and gates["run"] == "pass"
                    and gates["row_count_ok"]
                    and alignment >= 80
                    and stability <= 0.05):
                convergence_reason = "converged_soft"
                break

        else:
            convergence_reason = "hard_stop_max_iters"

        # ─── Step 11a: finalization projection ──────────────────
        remaining_budget = args.budget_cap - budget_used_usd
        skip_step_12 = remaining_budget < args.finalization_cost_projection
        if skip_step_12:
            finalize = _synthesize_finalization_from_trace(iteration_trace, convergence_reason)
            print(f"Step 12: synthesized (budget tight; remaining=${remaining_budget:.3f})")
        else:
            # Step 12: finalization LLM call
            fin_result = _call_finalization(bundle, iteration_trace, convergence_reason)
            if fin_result["error"] or not attestation_complete(fin_result["response"], ATTESTATION_FIELDS_FINALIZATION):
                convergence_reason = "hard_stop_finalization_attestation_failure"
                finalize = _synthesize_finalization_from_trace(iteration_trace, convergence_reason)
            else:
                finalize = fin_result["response"]
                budget_used_usd += fin_result["cost_usd"]
                total_input_tokens += fin_result["input_tokens"]
                total_output_tokens += fin_result["output_tokens"]
                total_cache_read_tokens += fin_result.get("cache_read_input_tokens", 0)
                total_cache_creation_tokens += fin_result.get("cache_creation_input_tokens", 0)
            print(f"Step 12: finalization done (budget_used=${budget_used_usd:.3f})")

        # ─── Step 13: confidence mapping ────────────────────────
        conditions_missed = finalize.get("term_conditions_missed", [])
        confidence, analyst_review, analyst_reason = _compute_confidence(
            convergence_reason,
            prior_alignment or 0,
            conditions_missed,
            analyst_flags_triggered=finalize.get("analyst_review_needed", False),
        )
        # Finalizer's own flag wins if stricter
        if finalize.get("analyst_review_needed") and not analyst_review:
            analyst_review = True
            analyst_reason = finalize.get("analyst_review_reason", analyst_reason)

        # ─── Safeguard 5: budget-pressure surfacing ──────────────────
        # If the bundle was trimmed on ANY iteration AND confidence ≤ medium,
        # surface that fact in analyst_review_reason so the analyst knows
        # low confidence may be a bundle-size artifact, not a term-quality one.
        iterations_with_trim = [
            entry["iteration_num"]
            for entry in iteration_trace
            if entry.get("bundle_trimmed_layers")
        ]
        if iterations_with_trim and confidence in ("medium", "low", "failed"):
            trim_note = (
                f" [budget pressure: bundle trimmed on "
                f"iteration(s) {iterations_with_trim} — review may be bundle-size related]"
            )
            analyst_reason = (analyst_reason or "") + trim_note
            analyst_review = True

        # ─── Step 11b: C5 sourcing recommendations ────────────────
        # Fires when the iteration loop terminated via consecutive
        # scope_sanity=no (convergence_reason=hard_stop_scope_mismatch).
        # Surfaces actionable table-extension recommendations to the
        # analyst rather than leaving the term flagged "unanswerable".
        # By design C5 uses no prompt caching (catalog block uncached;
        # per-call cost ~$0.01).
        c5_input_tokens: Optional[int] = None
        c5_output_tokens: Optional[int] = None
        c5_cost_usd: Optional[float] = None
        c5_skipped_reason: Optional[str] = None
        c5_validated_result: Optional[dict] = None
        if _should_fire_c5(iteration_trace, convergence_reason):
            c5_projection = 0.05  # conservative; Q1 measured $0.0105 first-call uncached
            remaining_budget = args.budget_cap - budget_used_usd
            if remaining_budget < c5_projection:
                c5_skipped_reason = "budget_exhausted"
                print(
                    f"Step 11b (C5): skipped — remaining=${remaining_budget:.3f} "
                    f"< projection=${c5_projection:.3f}"
                )
            else:
                grain_row = conn.execute(
                    "SELECT grain FROM main_seeds.business_glossary WHERE id = ?",
                    [term_id],
                ).fetchone()
                term_grain = grain_row[0] if grain_row and grain_row[0] else ""
                last_entry = iteration_trace[-1] if iteration_trace else {}
                # C5 closure 1/4 — populate [REACHABILITY VIOLATIONS] block
                # only when firing via Option B path; empty string for the
                # existing scope_sanity-no path (template emits no section).
                reachability_block = _format_reachability_violations_block(
                    iteration_trace, convergence_reason,
                )
                c5_result = _call_c5_sourcing(
                    term_name=term_name,
                    term_definition=term_def,
                    term_grain=term_grain,
                    term_conditions=term_conditions,
                    confirmed_scope_tables=scope_tables,
                    last_iteration_sql=last_entry.get("query_sql", ""),
                    last_iteration_reflection=last_entry.get("llm_self_reflection", ""),
                    scope_sanity_rationale=last_entry.get("llm_self_reflection", ""),
                    catalog_block=_format_c5_catalog_block(_load_c5_catalog()),
                    reachability_violations=reachability_block,
                )
                c5_input_tokens = c5_result.get("input_tokens", 0)
                c5_output_tokens = c5_result.get("output_tokens", 0)
                c5_cost_usd = c5_result.get("cost_usd", 0.0)
                budget_used_usd += c5_cost_usd
                err = c5_result.get("error")
                if err and "JSON parse" in err:
                    c5_skipped_reason = "llm_response_unparseable"
                    print(f"Step 11b (C5): unparseable response — {err}")
                elif err:
                    c5_skipped_reason = "llm_call_error"
                    print(f"Step 11b (C5): LLM error — {err}")
                else:
                    llm_output = c5_result.get("response") or {}
                    if not isinstance(llm_output, dict) or "recommendations" not in llm_output:
                        c5_skipped_reason = "llm_response_unparseable"
                        print("Step 11b (C5): response missing 'recommendations' key")
                    else:
                        from c5_validation import validate_recommendations
                        result = validate_recommendations(llm_output, duckdb_conn=conn)
                        # Convert dataclasses to dicts for JSON storage in BAR.
                        result["validated_recommendations"] = [
                            v.to_dict() for v in result["validated_recommendations"]
                        ]
                        c5_validated_result = result
                        s = result["summary"]
                        print(
                            f"Step 11b (C5): {s['total_recommendations']} recs "
                            f"(A={s['case_a_count']}, B={s['case_b_count']}, "
                            f"C={s['case_c_count']}, D={s['case_d_count']}); "
                            f"cost=${c5_cost_usd:.4f}"
                        )

        # Step 4: did C5 produce at least one usable recommendation?
        # Usable = grade in {verified, verified_low_priority, divergence_warning}
        # (cases A and C per Component 4 matrix; B/D are not actionable).
        c5_has_usable = False
        if c5_validated_result is not None:
            c5_has_usable = any(
                v["recommendation_grade"] in
                {"verified", "verified_low_priority", "divergence_warning"}
                for v in c5_validated_result["validated_recommendations"]
            )

        # ─── Step 14: conditional UPDATE via _bar_writer ─────────
        if c5_has_usable:
            # Override default mapping; convergence_reason stays
            # hard_stop_scope_mismatch (the underlying verdict didn't
            # change — C5 just added actionable output on top).
            terminal_status = "needs_data_extension"
        else:
            terminal_status = (
                "converged" if convergence_reason.startswith("converged")
                else "failed" if any(x in convergence_reason for x in
                                      ("preflight", "attestation", "orphaned", "glossary_drift"))
                else "hard_stop"
            )

        updates = {
            "status": terminal_status,
            "inprogress_since_utc": None,
            "finished_at_utc": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
            # Persist scope_tables resolved at Step 2
            "scope_tables": json.dumps(scope_tables or []),
            "iterations_count": len(iteration_trace),
            "convergence_reason": convergence_reason,
            "final_query_sql": iteration_trace[-1]["query_sql"] if iteration_trace else None,
            "final_metric_value": finalize.get("final_metric_value"),
            "final_metric_unit": finalize.get("final_metric_unit", ""),
            "final_metric_interpretation": finalize.get("final_metric_interpretation", ""),
            "term_conditions_covered": json.dumps(finalize.get("term_conditions_covered", [])),
            "term_conditions_missed": json.dumps(conditions_missed),
            "confidence": confidence,
            "confidence_rationale": finalize.get("confidence_rationale", ""),
            "analyst_review_needed": analyst_review,
            "analyst_review_reason": analyst_reason,
            "iteration_trace": json.dumps(iteration_trace, default=str),
            "bundle_fingerprint": bundle_fingerprint,
            "bundle_token_count": bundle.token_count,
            "llm_total_input_tokens": total_input_tokens,
            "llm_total_output_tokens": total_output_tokens,
            "llm_total_cost_usd": budget_used_usd,
            "ontology_consumed": json.dumps(finalize.get("ontology_consumed", [])),
            "domain_facts_consumed": json.dumps(finalize.get("domain_facts_consumed", [])),
            "analysis_findings_consumed": json.dumps(finalize.get("analysis_findings_consumed", [])),
            "dar_consumed": json.dumps(finalize.get("dar_consumed", [])),
            "prior_bar_consumed": json.dumps(finalize.get("prior_bar_consumed", [])),
            # Layer A + Layer B attestation persistence.
            # Union finalize + trace gates_result so the BAR
            # column captures per-iteration citations even if the LLM's
            # finalize response drops them. Trace[N].gates_result holds
            # each iteration's echoed attestation (threaded via
            # iter_sm_consumed / iter_dsm_consumed at _trace_entry call
            # sites). BAR value is guaranteed to be a superset of any
            # individual trace[N].gates_result.<field>.
            "semantic_model_consumed": json.dumps(
                _union_attestation_from_trace_and_finalize(
                    iteration_trace, finalize, "semantic_model_consumed")),
            "dbt_semantic_model_consumed": json.dumps(
                _union_attestation_from_trace_and_finalize(
                    iteration_trace, finalize, "dbt_semantic_model_consumed")),
            # Option B Phase 4 — iteration-only attestation. Helper unions
            # finalize.<field> (always [] post-Gap-C; finalization doesn't
            # attest this) + every trace[N].response.<field>, so the BAR
            # captures the iteration LLM's bridge_coverage_consulted echo.
            "bridge_coverage_consulted": json.dumps(
                _union_attestation_from_trace_and_finalize(
                    iteration_trace, finalize, "bridge_coverage_consulted")),
            # C3 (Theme 1 sub-items 1+2) — TAR-NNNNN citation discipline.
            # Same Gap D union pattern as bridge_coverage_consulted:
            # finalize.tars_consulted will typically be [] (FINALIZATION
            # doesn't attest TAR consumption per Gap C iteration/finalization
            # split), but every trace[N].response.tars_consulted contributes
            # to the BAR's union — capturing each iteration's TAR citations
            # even if a later iteration's response drops them.
            "tars_consulted": json.dumps(
                _union_attestation_from_trace_and_finalize(
                    iteration_trace, finalize, "tars_consulted")),
            # C4 (Theme 1 sub-item 5) — Stage A blocker citation discipline.
            # Same Gap D union pattern; iteration-only attestation field
            # (FINALIZATION excluded per Gap C). Captures blocker IDs the
            # iteration LLM consulted from the "## Stage A blockers" bundle
            # section, format "iter{N}.b{I}".
            "stage_a_blockers_consumed": json.dumps(
                _union_attestation_from_trace_and_finalize(
                    iteration_trace, finalize, "stage_a_blockers_consumed")),
            "last_source_ingestion_at": None,  # known_issue #25 — stays NULL
            # C5 sourcing recommendations. All NULL when C5
            # didn't fire; populated when it did (with c5_skipped_reason
            # set when the call was attempted but produced no usable
            # output).
            "sourcing_recommendations": (
                json.dumps(c5_validated_result, default=str)
                if c5_validated_result is not None else None
            ),
            "c5_input_tokens": c5_input_tokens,
            "c5_output_tokens": c5_output_tokens,
            "c5_cost_usd": c5_cost_usd,
            "c5_skipped_reason": c5_skipped_reason,
        }

        affected = update_bar_row(
            conn,
            bar_id=bar_id,
            only_if_status="in_progress",
            **updates,
        )

        if affected == 0:
            # Sweep-race: sibling recovery row
            updates["analyst_review_needed"] = True
            updates["analyst_review_reason"] = (
                f"Legitimate run completed after orphan sweep took original "
                f"placeholder; see BAR id {bar_id} for swept row."
            )
            recovery_id = write_bar_row(
                conn,
                business_term_id=term_id,
                status=terminal_status,
                record_source="piece8_term_injection_sweep_race_recovery",
                **{k: v for k, v in updates.items() if k != "status"},
            )
            print(f"Sweep race detected: swept {bar_id}, recovery {recovery_id}")
            bar_id = recovery_id

        print(f"Step 14: BAR {bar_id} status={terminal_status} "
              f"convergence={convergence_reason} confidence={confidence}")
        print(f"         budget_used=${budget_used_usd:.3f}/${args.budget_cap:.2f} "
              f"iterations={len(iteration_trace)}")

        # known_issue #53 — one parquet sync per runner session at the
        # terminal update (not per-iteration). Dashboard sees fresh BAR
        # state on next reload.
        # KI-103 fix: pass seed_name to route through the in-process
        # per-seed branch (bulk-subprocess branch fails silently on
        # Windows file-lock contention; same fix-class as KI-105).
        from _parquet_sync import sync_parquet_and_invalidate  # noqa: E402
        sync_warning = sync_parquet_and_invalidate(
            project_root=_PROJECT_ROOT,
            seed_name="business_term_analysis_results",
            source="run_term_injection.step_14",
        )
        if sync_warning:
            print(
                f"WARN: BAR parquet sync incomplete; dashboard may be "
                f"stale until next sync: {sync_warning}",
                file=sys.stderr,
            )

        return 0 if terminal_status == "converged" else 1

    except duckdb.Error as e:
        print(f"INFRASTRUCTURE ERROR: DuckDB: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"OPERATIONAL ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _finalize_failed(
    conn: duckdb.DuckDBPyConnection,
    bar_id: str,
    term_id: str,
    *,
    convergence_reason: str,
    rationale: str,
) -> None:
    """Helper for preflight-phase failures (steps 2, 3, 5a) — writes
    terminal state to placeholder without requiring iteration loop state.

    Also a terminal BAR write path: triggers parquet sync so dashboard
    sees the failed state immediately (known_issue #53)."""
    update_bar_row(
        conn,
        bar_id=bar_id,
        only_if_status="in_progress",
        status="failed",
        inprogress_since_utc=None,
        finished_at_utc=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
        iterations_count=0,
        convergence_reason=convergence_reason,
        confidence="failed",
        confidence_rationale=rationale,
        analyst_review_needed=True,
        analyst_review_reason=rationale[:120],
    )
    # KI-103 fix: pass seed_name (see step_14 above for rationale).
    from _parquet_sync import sync_parquet_and_invalidate  # noqa: E402
    sync_warning = sync_parquet_and_invalidate(
        project_root=_PROJECT_ROOT,
        seed_name="business_term_analysis_results",
        source="run_term_injection._finalize_failed",
    )
    if sync_warning:
        print(
            f"WARN: BAR parquet sync incomplete; dashboard may be "
            f"stale until next sync: {sync_warning}",
            file=sys.stderr,
        )


def _trace_entry(
    iter_num: int,
    sql: str,
    sample_rows: list,
    row_count: int,
    gates: dict,
    alignment: Optional[int],
    rubric: Optional[int],
    rubric_drift: Optional[bool],
    scope_sanity: Optional[str],
    bundle: Any,
    reflect: Optional[dict] = None,
    # Per-iteration cache telemetry (sum across iter + reflect calls)
    iter_input_tokens: int = 0,
    iter_output_tokens: int = 0,
    iter_cache_read_tokens: int = 0,
    iter_cache_creation_tokens: int = 0,
    iter_cost_usd: float = 0.0,
    # Per-iteration attestation persistence (companion to
    # the BAR schema fix). Both fields are injected into gates_result
    # so post-hoc analysis can determine which Layer A/B rows the LLM
    # cited in iteration N specifically, not just the BAR-level union.
    # Hard-stop paths may pass empty lists; happy path pulls from the
    # iteration response's attestation echo.
    semantic_model_consumed: Optional[list] = None,
    dbt_semantic_model_consumed: Optional[list] = None,
    # Fix: preserve the iteration LLM's full attestation echo (all 7
    # fields) under a "response" key so _synthesize_finalization_from_trace's
    # _union_field finds them via it.get("response") for all 7 fields, not
    # just the 2 currently threaded through gates_result. Without this,
    # the finalization-attestation gate fires whenever the finalization
    # LLM call is skipped (budget projection), even though the iteration
    # LLM emitted complete attestation that passed attestation_complete().
    response_attestation: Optional[dict] = None,
) -> dict:
    return {
        "iteration_num": iter_num,
        "query_sql": sql,
        "result_summary": {"row_count": row_count, "sample_rows": sample_rows[:3]},
        "llm_self_reflection": (reflect or {}).get("reasoning_summary") or (reflect or {}).get("reflection_text", ""),
        "term_condition_assessment": (reflect or {}).get("term_condition_assessment", []),
        "gates_result": {
            **gates,
            "semantic_alignment": alignment,
            "shadow_rubric_score": rubric,
            # Telemetry — 4 token fields + cost
            "input_tokens": iter_input_tokens,
            "output_tokens": iter_output_tokens,
            "cache_read_input_tokens": iter_cache_read_tokens,
            "cache_creation_input_tokens": iter_cache_creation_tokens,
            "budget_used_usd": iter_cost_usd,
            # Per-iteration attestation echo in gates_result.
            "semantic_model_consumed": list(semantic_model_consumed or []),
            "dbt_semantic_model_consumed": list(dbt_semantic_model_consumed or []),
        },
        "semantic_alignment_score": alignment,
        "shadow_rubric_score": rubric,
        "shadow_rubric_arithmetic_drift_flag": bool(rubric_drift),
        "scope_sanity_answer": scope_sanity,
        "bundle_trimmed_layers": (bundle.debug.get("dropped_rows_by_layer", []) if bundle and bundle.debug else []),
        # Fix: full LLM attestation echo for synthesis-path consumption.
        "response": dict(response_attestation or {}),
    }


if __name__ == "__main__":
    sys.exit(main())
