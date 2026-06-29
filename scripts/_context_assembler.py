"""Phase 15a piece 5 Gate A - Context assembly helper.

See context/phase_15a_piece_4_context_assembly_helper.md for the design.

Gate A scope (this file):
- Six-layer architecture (static/dynamic/ontology/examples/business/archived)
- Purpose weight matrix (module-level dict)
- Linear-normalization budget math per DESIGN §3d
- Scope resolution cascade Strategies 1, 2, 3, 5 (S4 stubbed as NotImplementedError)
- Per-layer empty definitions per §3j table
- Strict mode raises ContextDegradedError on empty HEAVY
- Content-hash fingerprint per §3f using _sidecar.compute_file_hash
- Tokenizer stub (len // 4); Gate A-2 swap to Anthropic count_tokens + cache

Out of scope for Gate A:
- LLM calls of any kind (Strategy 4 raises NotImplementedError)
- Completeness / Dimensions analyses (Gate B)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import duckdb

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "dbt" / "seeds"
DB_PATH = ROOT / "cpe_analytics.duckdb"
TELEMETRY_LOG = ROOT / "logs" / "assemble_context.jsonl"
TOKENIZER_CACHE = ROOT / ".tokenizer_cache.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sidecar import compute_file_hash, now_iso_utc  # noqa: E402

# =========================================================================
# Constants (§3c weight matrix, §3d units)
# =========================================================================

PURPOSES = (
    "create_s2t", "eda_classification", "eda_sql_generation",
    "storytelling", "chat_followup", "pre_s2t_reasoning",
)

LAYERS = ("static", "dynamic", "ontology", "examples", "business", "archived")

PURPOSE_WEIGHTS: dict[str, dict[str, str]] = {
    "create_s2t": {
        "static": "heavy", "dynamic": "heavy", "ontology": "HEAVY",
        "examples": "heavy", "business": "heavy", "archived": "light",
    },
    "eda_classification": {
        "static": "HEAVY", "dynamic": "off", "ontology": "off",
        "examples": "heavy", "business": "light", "archived": "off",
    },
    "eda_sql_generation": {
        "static": "HEAVY", "dynamic": "HEAVY", "ontology": "off",
        "examples": "light", "business": "light", "archived": "off",
    },
    "storytelling": {
        "static": "light", "dynamic": "HEAVY", "ontology": "off",
        "examples": "off", "business": "heavy", "archived": "off",
    },
    "chat_followup": {
        "static": "heavy", "dynamic": "HEAVY", "ontology": "light",
        "examples": "light", "business": "heavy", "archived": "light",
    },
    # Phase 15b piece 8 §2a (bootstrap added in 8.2; 8.3 adds safeguards).
    # Dynamic HEAVY — DAR reservoir projection onto term is the core input.
    # Business HEAVY — term definition and business context anchor reasoning.
    # Ontology heavy — existing models inform ref() collision avoidance (#29).
    # Static heavy — SAP column semantics for grain/join/type interpretation.
    # Examples light — ABAP signal narrowly relevant at single-term scope.
    # Archived light — prior-art advisory only; archive is final (#67).
    "pre_s2t_reasoning": {
        # Fix-up (2026-04-26): demoted business HEAVY→heavy. BG027
        # measurement showed business uses 762 of its 12K budget (6.4%)
        # while dynamic exceeds its 12K budget by ~1.5K post-#95
        # schema_discovery fix. Redistribution gives dynamic +1333 tokens
        # (12000→13333) without raising overall max_tokens. Business
        # retains 8333 tokens, still ~11x its observed BG027 usage.
        "static": "heavy", "dynamic": "HEAVY", "ontology": "heavy",
        "examples": "light", "business": "heavy", "archived": "light",
    },
}

WEIGHT_UNITS = {"HEAVY": 40, "heavy": 25, "light": 10, "off": 0}
OVERHEAD_RATIO = 0.10

MODEL = "claude-sonnet-4-6"

# Layer source CSVs used for content-hash fingerprint (§3f)
LAYER_SOURCE_CSVS: dict[str, list[str]] = {
    "static": [
        "sap_data_dictionary.csv", "source_column_roles.csv",
        # movement_type_mapping.csv decommissioned 2026-05-05 (Phase α):
        # BWART decode now sourced via main_marts.dim_movement_type which
        # reads vault sat_movement_type + sat_movement_type_text (SAP-native
        # T156 + T156T). Static-layer fingerprint covers raw_sap drift.
        "z_tables_catalog.csv",
        # v3.6 §22.6 — Layer A compiled semantic model. Included in the
        # fingerprint so mid-session recompile invalidates the drift probe
        # per §7b (catches the case where compile_semantic_model.py runs
        # while a piece 8 session is in flight on the same term).
        "semantic_model.csv",
        # v3.7 §23.6 — Layer B dbt-manifest-compiled model conventions.
        # Fingerprint inclusion ensures mid-session recompile (via
        # compile_dbt_semantic_model.py) invalidates the drift probe.
        "dbt_semantic_model.csv",
    ],
    "dynamic": [
        "analysis_findings.csv",
        # Piece 2 OBTs: added to fingerprint source list post Gate C review.
        # Before this fix the fingerprint was DAR-content-blind — 4 calls
        # across different DAR states (empty, 1 row, 3 rows) all hashed to
        # the same value, masking the dynamic layer's content churn.
        "domain_analysis_results.csv",
        "business_term_analysis_results.csv",
        # Stage C (Piece 9 §28.11.7): TAR churn must invalidate the
        # fingerprint so Piece 8's drift probe catches Term EDA re-runs.
        "term_analysis_results.csv",
    ],
    "ontology": ["dbt_column_lineage.csv", "s2t_mapping.csv"],
    "examples": ["abap_logic_catalog.csv"],
    "business": [
        # vendor_catalog.csv (2026-05-05) and cpe_catalog.csv (2026-05-05)
        # deprecated — enrichment now reads from main_vault.sat_vendor_business
        # and main_vault.sat_material_business so the LLM can JOIN against
        # SAP master data. Vault-side changes are caught by the
        # static/dynamic-layer fingerprints.
        "procurement_rules.csv",
        "org_structure.csv", "domain_facts.csv", "business_glossary.csv",
    ],
    "archived": ["archive_log.csv"],
}

# Per-layer "non-empty" sources (§3j) — any one producing rows = layer non-empty
EMPTY_SOURCES: dict[str, tuple[str, ...]] = {
    # v3.6 §22.6 note — 'semantic_model' appears here so it contributes to
    # _layer_is_empty's any-source-non-zero logic. But Layer A being empty
    # for a scope is legitimately correct when all scope_tables have dbt
    # ontology coverage (§22.9(b)). The actual "uncovered-table-missing-
    # Layer-A" signal is surfaced separately via the
    # 'semantic_model_coverage_gap' detail key set by _load_static — callers
    # (piece 8 runner, analyst via debug metadata) read that boolean rather
    # than inferring from layer-empty. See Q2 resolution nuance in §22.10.
    # v3.7 §23.6 note — 'dbt_semantic_model' added. Same conditional-empty
    # pattern as Layer A's 'semantic_model': empty Layer B for a scope
    # where all scope_tables are raw-only (no dbt coverage) is legitimately
    # correct. The 'dbt_semantic_model_coverage_gap' detail key (set by
    # _load_static when empty AND some scope_table has dbt coverage) is
    # the specific signal callers read for a real gap.
    "static": ("sap_data_dictionary", "source_column_roles",
               "dim_movement_type", "z_tables_catalog",
               "information_schema", "semantic_model", "dbt_semantic_model"),
    "dynamic": ("domain_analysis_results", "business_term_analysis_results",
                "analysis_findings", "term_analysis_results"),
    "ontology": ("dbt_column_lineage", "existing_models", "s2t_mapping"),
    "examples": ("abap_logic_catalog",),
    "business": ("domain_facts", "business_glossary", "sat_vendor_business",
                 "sat_material_business", "procurement_rules", "org_structure"),
    "archived": ("archive_log",),
}


# =========================================================================
# Exceptions
# =========================================================================

class ContextScopeError(RuntimeError):
    """Scope resolution could not determine any tables."""


class ContextDegradedError(RuntimeError):
    """A HEAVY layer returned zero rows in strict mode."""

    def __init__(self, layer: str, reason: str, scope: list,
                 weights: dict):
        self.layer = layer
        self.reason = reason
        self.scope = scope
        self.weights = weights
        super().__init__(
            f"HEAVY layer '{layer}' is empty. Reason: {reason}. "
            f"Scope: {scope}."
        )


class ContextOverflowError(RuntimeError):
    """Bundle would exceed max_tokens ceiling."""


# =========================================================================
# Bundle data type (§3a)
# =========================================================================

@dataclass
class ContextBundle:
    formatted_prompt: str
    token_count: int
    layer_summary: dict  # layer_name -> token_count
    scope_resolution: dict  # {strategy_used, resolved_tables}
    debug: Optional[dict] = None
    # v3.4 §20d — per-layer text surfaces for Piece 8 caching breakpoints.
    # Backward-compatible: formatted_prompt is unchanged; new attributes
    # expose the same content granularly so piece 8's _call_piece8_prompt
    # can group layers into BP1/BP2/BP3/BP4 cache blocks.
    static_layer_text: str = ""
    dynamic_layer_text: str = ""
    ontology_layer_text: str = ""
    examples_layer_text: str = ""
    business_layer_text: str = ""
    archived_layer_text: str = ""


# =========================================================================
# Budget math (§3d linear normalization)
# =========================================================================

def compute_layer_budgets(purpose: str, max_tokens: int) -> dict[str, int]:
    """Per §3d: overhead reserve + linear normalization by weight units.

    Worked example create_s2t@50K matches: ontology=12000, 4 heavies=7500,
    archived=3000, sum=45000.
    """
    if purpose not in PURPOSE_WEIGHTS:
        raise ValueError(f"unknown purpose: {purpose!r}")
    weights = PURPOSE_WEIGHTS[purpose]
    overhead = int(max_tokens * OVERHEAD_RATIO)
    remaining = max_tokens - overhead
    total_units = sum(WEIGHT_UNITS[weights[layer]] for layer in LAYERS)
    if total_units == 0:
        return {layer: 0 for layer in LAYERS}
    # Integer-divide per layer; distribute remainder to HEAVY layers first
    # to keep sum exact on representative purposes.
    budgets = {
        layer: (WEIGHT_UNITS[weights[layer]] * remaining) // total_units
        for layer in LAYERS
    }
    # Any integer-division leftover stays unallocated (acceptable; design
    # is order-of-magnitude). For the design's two worked examples the
    # math reconciles exactly with no leftover.
    return budgets


# =========================================================================
# Fingerprint (§3f content-hash, not mtime)
# =========================================================================

def compute_fingerprint(scope_tables: list[str], purpose: str,
                        max_tokens: int) -> str:
    """sha256(scope + purpose + max_tokens + each layer source CSV sha256
    + dbt manifest.json hash if present)[:16].

    v3.7 §23.6 — includes dbt/target/manifest.json hash alongside the
    CSV hashes so a `dbt parse` that regenerates manifest between a
    compile and a session invalidates the drift probe even before the
    analyst re-runs compile_dbt_semantic_model.py. Redundant with the
    dbt_semantic_model.csv hash after compile, but catches the brief
    window where manifest is fresh and compile is stale.

    Uses _sidecar.compute_file_hash — rejects mtime per v3 review-round 2.
    """
    parts: list[str] = [
        json.dumps(sorted(scope_tables)),
        purpose,
        str(max_tokens),
    ]
    # v3.7 §23.6 — manifest.json hash as an independent drift signal.
    manifest_path = ROOT / "dbt" / "target" / "manifest.json"
    if manifest_path.exists():
        parts.append(f"manifest.json:{compute_file_hash(manifest_path)}")
    for layer in LAYERS:
        for csv_name in LAYER_SOURCE_CSVS.get(layer, []):
            p = SEED_DIR / csv_name
            if p.exists():
                parts.append(f"{csv_name}:{compute_file_hash(p)}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# =========================================================================
# Tokenizer — Gate A stub, Gate A-2 Anthropic (opt-in via env var)
# =========================================================================

def count_tokens_stub(text: str) -> int:
    """Gate A approximation: 1 token ~ 4 chars."""
    return len(text) // 4


_tokenizer_cache_stats = {"hits": 0, "misses": 0}


def _load_tokenizer_cache() -> dict[str, int]:
    if not TOKENIZER_CACHE.exists():
        return {}
    try:
        return json.loads(TOKENIZER_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_tokenizer_cache(cache: dict[str, int]) -> None:
    tmp = TOKENIZER_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
    os.replace(tmp, TOKENIZER_CACHE)


def count_tokens_anthropic(text: str) -> int:
    """Gate A-2: Anthropic count_tokens endpoint + per-content-hash cache.

    Activated when env var CONTEXT_ASSEMBLER_TOKENIZER=anthropic is set.
    Falls back to stub when ANTHROPIC_API_KEY is missing so tests + offline
    runs still work without surprises.
    """
    key = hashlib.sha256((text + "|" + MODEL).encode("utf-8")).hexdigest()
    cache = _load_tokenizer_cache()
    if key in cache:
        _tokenizer_cache_stats["hits"] += 1
        return cache[key]
    _tokenizer_cache_stats["misses"] += 1
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return count_tokens_stub(text)
    import requests
    r = requests.post(
        "https://api.anthropic.com/v1/messages/count_tokens",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": MODEL, "messages": [{"role": "user", "content": text}]},
        timeout=30,
    )
    r.raise_for_status()
    n = int(r.json().get("input_tokens", 0))
    cache[key] = n
    _save_tokenizer_cache(cache)
    return n


def _count_tokens(text: str) -> int:
    """Dispatch — anthropic by default (per Gate A-2), stub when env-forced
    OR when ANTHROPIC_API_KEY is missing (anthropic impl falls back to stub
    automatically, preserving offline / test hermeticity)."""
    if not text:
        return 0
    mode = os.environ.get("CONTEXT_ASSEMBLER_TOKENIZER", "anthropic").lower()
    if mode == "stub":
        return count_tokens_stub(text)
    return count_tokens_anthropic(text)


# =========================================================================
# Scope resolution (§3e) — S1, S2, S3, S5; S4 = NotImplementedError
# =========================================================================

def _raw_sap_tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT LOWER(table_name) FROM information_schema.tables "
        "WHERE table_schema='raw_sap'"
    ).fetchall()
    return {r[0] for r in rows}


def _split_csv_col(value) -> list[str]:
    if not value:
        return []
    return [t.strip().lower() for t in str(value).split(",") if t.strip()]


def _strategy_1(conn, term_id: str) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT tables_explored FROM main_seeds.analysis_findings "
            "WHERE business_term_id = ?", [term_id]
        ).fetchall()
    except Exception:
        return []
    tables: set[str] = set()
    for (csv_list,) in rows:
        tables.update(_split_csv_col(csv_list))
    return sorted(tables)


def _strategy_2(conn, term_id: str) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_table FROM main_seeds.s2t_mapping "
            "WHERE business_term_id = ?", [term_id]
        ).fetchall()
    except Exception:
        return []
    return sorted({str(r[0]).strip().lower() for r in rows if r[0]})


def _strategy_3(conn, term_id: str) -> list[str]:
    """business_term_analysis_results only (v3 narrowed; domain OBT removed)."""
    try:
        rows = conn.execute(
            "SELECT source_tables "
            "FROM main_seeds.business_term_analysis_results "
            "WHERE business_term_id = ?", [term_id]
        ).fetchall()
    except Exception:
        return []
    tables: set[str] = set()
    for (csv_list,) in rows:
        tables.update(_split_csv_col(csv_list))
    return sorted(tables)


def _strategy_4(conn, term_id: str) -> list[str]:
    """Gate A stub per instructions — LLM extraction not yet wired."""
    raise NotImplementedError(
        "Strategy 4 (LLM scope extraction) is not implemented in Gate A. "
        "Design §3e specifies a _post_claude call; add in a later gate."
    )


def has_prior_dars_for_scope(scope: list[str]) -> bool:
    """Returns True if at least one current DAR exists for any table in scope.

    Used by LLM analyzers (completeness, dimensions, magnitude, code_tables)
    to decide whether to pass strict=True vs strict=False to
    assemble_context. strict=True raises ContextDegradedError when the HEAVY
    'dynamic' layer is empty; for a brand-new raw table with zero prior
    DARs, that's expected — the analyzer should still proceed with a
    gracefully-degraded bundle (Layer A/B + ontology signals still
    populate). known_issue #75.

    Opens its own read-only DuckDB connection; fails closed (returns True)
    on any error so callers default to strict=True on probe failure.
    """
    if not scope:
        return False
    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            placeholders = ",".join(["?"] * len(scope))
            rows = conn.execute(
                f"SELECT COUNT(*) FROM main_seeds.domain_analysis_results "
                f"WHERE LOWER(source_tables) IN ({placeholders}) "
                f"  AND (superseded_by IS NULL OR superseded_by = '')",
                [t.lower() for t in scope],
            ).fetchone()
            return (rows[0] or 0) > 0
        finally:
            conn.close()
    except Exception:
        # Probe failure: default to strict=True (prior behavior preserved).
        return True


def resolve_scope(conn, term_id: Optional[str],
                  scope_tables: Optional[list[str]]) -> dict:
    """Returns {strategy_used, resolved_tables}.

    Precedence: explicit scope_tables > S1 > S2 > S3 > (S4 NOT WIRED IN GATE A)
    > S5 (empty fallback). ContextScopeError when neither input given.
    """
    live = _raw_sap_tables(conn)

    def _keep_live(ts: list[str]) -> list[str]:
        return sorted([t.lower() for t in ts if t.lower() in live])

    if scope_tables is not None:
        return {"strategy_used": "explicit",
                "resolved_tables": _keep_live(scope_tables)}
    if term_id is None:
        raise ContextScopeError(
            "Either term_id or scope_tables must be provided"
        )

    for name, fn in (("s1", _strategy_1), ("s2", _strategy_2),
                     ("s3", _strategy_3)):
        ts = fn(conn, term_id)
        if ts:
            kept = _keep_live(ts)
            if kept:
                return {"strategy_used": name, "resolved_tables": kept}

    # S4 not wired in Gate A; proceed to S5 (unscoped fallback)
    return {"strategy_used": "s5", "resolved_tables": []}


# =========================================================================
# Layer loaders — each returns (content_str, token_count, per_source_counts)
# =========================================================================

def _csv_fmt(header: list[str], rows: list) -> str:
    def q(v):
        s = "" if v is None else str(v)
        if "," in s or '"' in s or "\n" in s:
            return '"' + s.replace('"', '""') + '"'
        return s
    lines = [",".join(header)]
    for r in rows:
        lines.append(",".join(q(c) for c in r))
    return "\n".join(lines)


def _truncate(content: str, budget: int) -> str:
    """Truncate content to fit a token budget. Uses a heuristic
    chars-per-token estimate (4 chars/token) to bound the cut size.

    #96 minimal — when truncation actually fires, log to stderr (so the
    runner sees the budget pressure) AND append a visible footer to the
    rendered content (so the LLM knows it's operating on incomplete
    evidence rather than silently consuming a tail-cut bundle). The
    fuller #96 design pass (budget-aware section ordering + budget-aware
    row count in loaders) stays deferred; this is the minimal
    observability pieces that the schema_discovery fix-up smoke needed.
    Centralising in `_truncate` covers all 7 layer call sites for free.
    """
    tok = _count_tokens(content)
    if tok <= budget:
        return content
    max_chars = max(budget * 4, 0)
    if len(content) <= max_chars:
        return content
    n_truncated = len(content) - max_chars
    sys.stderr.write(
        f"[context_assembler WARNING] _truncate dropped {n_truncated} "
        f"chars (~{tok - budget} tokens over) from content of {tok} "
        f"tokens to fit {budget}-token budget\n"
    )
    return content[:max_chars] + (
        f"\n[BUNDLE WARNING: {n_truncated} chars truncated to fit "
        f"{budget}-token budget; LLM operating on incomplete evidence]"
    )


# v3.7 §23.10 — dbt_layer → schema mapping parsed from dbt_project.yml.
# Used by Layer B's dual-rendering rewrite for iteration consumers.
# Cached at module load (one-time cost) with a `main_<layer>` fallback
# consistent with RULE 11 (main_ prefix convention).

_DBT_LAYER_SCHEMA_CACHE: Optional[dict[str, str]] = None
_DBT_LAYER_FALLBACK: dict[str, str] = {
    "staging": "main_staging",
    "vault_hub": "main_vault",
    "vault_link": "main_vault",
    "vault_satellite": "main_vault",
    "mart_fact": "main_marts",
    "mart_dim": "main_marts",
    "obt": "main_obt",
    "knowledge": "main_knowledge",
    "other": "main",
}
# yaml-subkey → dbt_layer mapping (§23.10 semantics).
_YAML_KEY_TO_LAYER: dict[str, tuple[str, ...]] = {
    "staging": ("staging",),
    "vault": ("vault_hub", "vault_link", "vault_satellite"),
    "marts": ("mart_fact", "mart_dim"),
    "obt": ("obt",),
    "knowledge": ("knowledge",),
}


def _load_dbt_layer_schemas() -> dict[str, str]:
    """Parse dbt_project.yml's models section for per-layer +schema
    suffixes, combine with the target.schema ('main' in this project) to
    produce fully-qualified schema names. Falls back to _DBT_LAYER_FALLBACK
    for layers without an explicit config. Cached after first call.
    """
    global _DBT_LAYER_SCHEMA_CACHE
    if _DBT_LAYER_SCHEMA_CACHE is not None:
        return _DBT_LAYER_SCHEMA_CACHE
    mapping = dict(_DBT_LAYER_FALLBACK)
    project_yml = ROOT / "dbt" / "dbt_project.yml"
    if not project_yml.exists():
        _DBT_LAYER_SCHEMA_CACHE = mapping
        return mapping
    try:
        import yaml  # type: ignore
        config = yaml.safe_load(project_yml.read_text(encoding="utf-8")) or {}
        models_section = (config.get("models") or {})
        # Walk one level down: models: <project_name>: <layer_key>: +schema
        for project_block in models_section.values():
            if not isinstance(project_block, dict):
                continue
            for yaml_key, block in project_block.items():
                if not isinstance(block, dict):
                    continue
                suffix = block.get("+schema")
                if not suffix:
                    continue
                # target.schema 'main' per project convention (RULE 11)
                fqn = f"main_{suffix}"
                for layer in _YAML_KEY_TO_LAYER.get(yaml_key, (yaml_key,)):
                    mapping[layer] = fqn
    except Exception:
        # If PyYAML missing or parsing fails, keep the RULE 11 fallback.
        pass
    _DBT_LAYER_SCHEMA_CACHE = mapping
    return mapping


# Regex for rewriting {{ ref('<model>') }} → <schema>.<model> in
# reference_sql for iteration consumers. Handles single or double quotes
# + optional surrounding whitespace.
import re as _re  # local alias; top-level re already imported elsewhere in runner
_REF_PATTERN = _re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")


def _rewrite_ref_to_literal(
    reference_sql: str,
    model_layer_map: dict[str, str],
    schema_map: dict[str, str],
) -> str:
    """For iteration consumers (purpose='pre_s2t_reasoning'), substitute
    every `{{ ref('<model>') }}` occurrence with `<schema>.<model>` using
    model_layer_map (model_name → dbt_layer) and schema_map (dbt_layer →
    schema). Unknown models fall back to `main.<model>`.
    """
    def _sub(match):
        name = match.group(1).lower()
        layer = model_layer_map.get(name, "other")
        schema = schema_map.get(layer, "main")
        return f"{schema}.{match.group(1)}"
    return _REF_PATTERN.sub(_sub, reference_sql)


def _in_list(n: int) -> str:
    return ",".join(["?"] * n)


# 8.4.8 Part 5 — loud-fail on schema-mismatch errors that were silently
# swallowed pre-fix. Known_issue #26 root cause: try/except in _load_static
# produced {seed_name: 0} without warning, hiding query-vs-schema drift
# (e.g. a column rename in a seed that breaks the hardcoded SELECT).
# This helper distinguishes schema-class errors (bug signal — WARN loud)
# from IO/missing-seed errors (expected-quiet, return 0 as today).
# Diagnostic env var PIECE8_STATIC_LOADER_DIAGNOSTIC=1 raises instead
# of swallowing for testing / reproduction.
_SCHEMA_ERROR_SIGNATURES = (
    "Binder Error",
    "Catalog Error",
    "Parser Error",
    "does not have a column",
    "referenced column",
    "not found in FROM clause",
)


def _loud_fail_or_swallow(exc: Exception, seed_name: str) -> None:
    """Part 5 loud-fail helper. No return value — caller continues with
    swallowed 0-row behavior. If PIECE8_STATIC_LOADER_DIAGNOSTIC is set,
    schema-class errors raise instead of just warning.
    """
    msg = str(exc)
    is_schema_bug = any(sig in msg for sig in _SCHEMA_ERROR_SIGNATURES)
    if is_schema_bug:
        warning = (
            f"[WARN] static-layer loader: seed '{seed_name}' failed with "
            f"schema mismatch: {exc}. Returning 0 rows. THIS LIKELY "
            f"INDICATES A BUG (known_issue #26 resolution pattern)."
        )
        print(warning, file=sys.stderr)
        if os.environ.get("PIECE8_STATIC_LOADER_DIAGNOSTIC") == "1":
            raise exc


def _load_static(conn, scope: list[str], term_id: Optional[str],
                 budget: int, purpose: Optional[str] = None) -> tuple[str, int, dict]:
    details = {"sap_data_dictionary": 0, "source_column_roles": 0,
               "dim_movement_type": 0, "z_tables_catalog": 0,
               "information_schema": 0, "semantic_model": 0,
               # v3.6 §22.6 — conditional coverage-gap signal (Q2 nuance).
               # True when: filtered-to-scope semantic_model rows = 0 AND at
               # least one scope_table lacks ontology coverage. Callers use
               # this to distinguish "Layer A legitimately empty for an
               # ontology-complete scope" (silent pass) from "Layer A
               # missing for an uncovered raw table" (real gap).
               "semantic_model_coverage_gap": False,
               # v3.7 §23.6 — Layer B (dbt_semantic_model) details. Same
               # conditional-empty semantics as semantic_model: an empty
               # Layer B for a raw-only scope is legitimate (Layer A
               # should populate); empty with dbt-covered scope is a gap.
               "dbt_semantic_model": 0,
               "dbt_semantic_model_coverage_gap": False}
    parts: list[str] = []

    if scope:
        try:
            rows = conn.execute(
                f"SELECT table_name, field_name, data_type, description_en, business_meaning "
                f"FROM main_seeds.sap_data_dictionary "
                f"WHERE LOWER(table_name) IN ({_in_list(len(scope))})",
                scope,
            ).fetchall()
            details["sap_data_dictionary"] = len(rows)
            if rows:
                parts += ["## sap_data_dictionary",
                          _csv_fmt(["table", "field", "sap_type",
                                    "description", "meaning"], rows)]
        except Exception as _e:
            _loud_fail_or_swallow(_e, "sap_data_dictionary")

        try:
            rows = conn.execute(
                f"SELECT table_name, column_name, role, role_confidence, role_rationale "
                f"FROM main_seeds.source_column_roles "
                f"WHERE LOWER(table_name) IN ({_in_list(len(scope))}) "
                f"ORDER BY table_name, column_name",
                scope,
            ).fetchall()
            details["source_column_roles"] = len(rows)
            if rows:
                parts += ["## source_column_roles",
                          _csv_fmt(["table", "column", "role", "confidence",
                                    "rationale"], rows)]
        except Exception as _e:
            _loud_fail_or_swallow(_e, "source_column_roles")

        if any(t in ("mseg", "mkpf", "ekbe") for t in scope):
            # Phase α 2026-05-05: movement_type_mapping seed decommissioned.
            # BWART decode now comes from main_marts.dim_movement_type which
            # joins hub_movement_type + sat_movement_type + sat_movement_type_text
            # (SAP-native T156 + T156T). Same columns the LLM had before;
            # just sourced through the proper SAP path instead of a hand
            # -curated seed.
            try:
                rows = conn.execute(
                    "SELECT movement_type, description_en, direction, "
                    "process_step, stock_impact_description "
                    "FROM main_marts.dim_movement_type "
                    "ORDER BY movement_type"
                ).fetchall()
                details["dim_movement_type"] = len(rows)
                if rows:
                    parts += [
                        "## dim_movement_type "
                        "(vault-sourced from T156 + T156T; BWART -> "
                        "description / direction / process_step / "
                        "stock_impact; join key: movement_type = <table>.BWART)",
                        _csv_fmt(
                            ["movement_type", "description_en", "direction",
                             "process_step", "stock_impact"], rows,
                        ),
                    ]
            except Exception as _e:
                _loud_fail_or_swallow(_e, "dim_movement_type")

        try:
            # 8.5.1 Part 4 — seed has no business_purpose column (actual cols:
            # table_name, description, important_fields, maintenance_transaction,
            # rows_estimate). Dropping the third column — description already
            # captures the table's purpose for Create S2T context.
            rows = conn.execute(
                f"SELECT table_name, description "
                f"FROM main_seeds.z_tables_catalog "
                f"WHERE LOWER(table_name) IN ({_in_list(len(scope))})",
                scope,
            ).fetchall()
            details["z_tables_catalog"] = len(rows)
            if rows:
                parts += ["## z_tables_catalog",
                          _csv_fmt(["table", "description"], rows)]
        except Exception as _e:
            _loud_fail_or_swallow(_e, "z_tables_catalog")

        # v3.6 §22.6 — Layer A (semantic_model) scope-filtered rendering.
        # Consumer priority: ontology first (dbt_column_lineage), Layer A
        # second. Only compiled rows for raw tables lacking ontology
        # coverage will be present; rows for covered tables aren't emitted
        # by compile_semantic_model.py (§22.5 step 3).
        try:
            sem_rows = conn.execute(
                f"SELECT table_name, canonical_alias, entity_class, "
                f"primary_key_cols, typical_join_keys_json, "
                f"code_column_refs_json, typical_filters, common_traps, "
                f"reference_sql, source_dar_ids "
                f"FROM main_seeds.semantic_model "
                f"WHERE LOWER(table_name) IN ({_in_list(len(scope))}) "
                f"ORDER BY table_name",
                scope,
            ).fetchall()
            details["semantic_model"] = len(sem_rows)
            if sem_rows:
                parts += [
                    "## Semantic Model (Layer A) "
                    "(per-table SQL conventions; cite table_name in "
                    "semantic_model_consumed attestation when you use a row)",
                    _csv_fmt(
                        ["table", "alias", "class", "pk", "joins_json",
                         "code_refs_json", "filters", "traps",
                         "reference_sql", "source_dar_ids"],
                        sem_rows,
                    ),
                ]
        except Exception:
            # Binder error (seed not yet created) or connection issue —
            # leave count at 0. coverage-gap detection below handles.
            pass

        # v3.7 §23.6 — Layer B (dbt_semantic_model) scope-filtered rendering
        # with dual-rendering per §23.10: for purpose='pre_s2t_reasoning'
        # rewrite {{ ref('<m>') }} → <schema>.<m>; for purpose='create_s2t'
        # preserve Jinja.
        try:
            # Full Layer B read (all models) — needed for the model→layer
            # map used by the ref-rewrite. Small (~90 rows).
            all_rows = conn.execute(
                "SELECT model_name, dbt_layer FROM main_seeds.dbt_semantic_model"
            ).fetchall()
            model_layer_map = {r[0].lower(): r[1] for r in all_rows}
            schema_map = _load_dbt_layer_schemas()

            # Scope-filter: match rows whose upstream_models reference any
            # raw_sap.<t>. Use LIKE patterns because upstream_models is a
            # comma-separated string stored verbatim from depends_on.nodes.
            like_exprs = " OR ".join(
                ["LOWER(upstream_models) LIKE ?"] * len(scope)
            )
            params = [f"%raw_sap.{t.lower()}%" for t in scope]
            dbt_sem_rows = conn.execute(
                f"SELECT model_name, dbt_layer, materialized, upstream_models, "
                f"downstream_models, exposed_columns_json, primary_key_cols, "
                f"canonical_alias, typical_join_keys_json, reference_sql, "
                f"model_description "
                f"FROM main_seeds.dbt_semantic_model "
                f"WHERE {like_exprs} "
                f"ORDER BY dbt_layer, model_name",
                params,
            ).fetchall()
            details["dbt_semantic_model"] = len(dbt_sem_rows)

            if dbt_sem_rows:
                # Dual-render per §23.10. For iteration consumer (purpose
                # ='pre_s2t_reasoning'), rewrite reference_sql. For
                # Create S2T (purpose='create_s2t'), preserve Jinja.
                rewrite = (purpose == "pre_s2t_reasoning")
                rendered_rows = []
                for r in dbt_sem_rows:
                    ref_sql = r[9] or ""
                    if rewrite and ref_sql:
                        ref_sql = _rewrite_ref_to_literal(
                            ref_sql, model_layer_map, schema_map
                        )
                    rendered_rows.append((
                        r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                        ref_sql, r[10],
                    ))
                render_mode = "literal_refs" if rewrite else "jinja_refs"
                parts += [
                    f"## dbt Semantic Model (Layer B) "
                    f"(per-model conventions; reference_sql rendered as "
                    f"{render_mode} for purpose={purpose or 'unknown'}; "
                    f"cite model_name in dbt_semantic_model_consumed attestation)",
                    _csv_fmt(
                        ["model", "layer", "materialized", "upstream_models",
                         "downstream_models", "exposed_columns_json", "pk",
                         "alias", "joins_json", "reference_sql", "description"],
                        rendered_rows,
                    ),
                ]
        except Exception:
            # Binder error (seed not yet created) or parse error — leave
            # count at 0; the gap probe below handles the signal.
            pass

        # Conditional dbt_semantic_model_coverage_gap per §23.9(b).
        # Gap when: Layer B empty AND some scope table HAS dbt ontology
        # coverage (i.e. a staging/vault/mart model traces back to it,
        # per dbt_column_lineage origin_table match). Inverse of Layer A's
        # gap condition.
        if details["dbt_semantic_model"] == 0:
            try:
                covered = conn.execute(
                    "SELECT t FROM (SELECT LOWER(UNNEST(?)) AS t) s "
                    "WHERE EXISTS ("
                    "  SELECT 1 FROM main_seeds.dbt_column_lineage l "
                    "  WHERE LOWER(l.origin_table) = s.t "
                    "     OR LOWER(l.origin_table) = 'raw_sap.' || s.t "
                    "     OR LOWER(l.origin_table) LIKE '%.' || s.t"
                    ")",
                    [scope],
                ).fetchall()
                if covered:
                    details["dbt_semantic_model_coverage_gap"] = True
                    details["dbt_semantic_model_coverage_gap_tables"] = \
                        sorted({r[0] for r in covered})
            except Exception as _e:
                _loud_fail_or_swallow(_e, "dbt_column_lineage")

        # Conditional coverage-gap signal per Q2 resolution §22.10.
        # dbt_column_lineage.origin_table stores raw tables as
        # 'raw_sap.<table>' (dominant) or bare '<table>' (less common);
        # match both forms + '%.t' suffix for defense-in-depth.
        if details["semantic_model"] == 0:
            try:
                uncovered = conn.execute(
                    "SELECT t FROM (SELECT LOWER(UNNEST(?)) AS t) s "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM main_seeds.dbt_column_lineage l "
                    "  WHERE LOWER(l.origin_table) = s.t "
                    "     OR LOWER(l.origin_table) = 'raw_sap.' || s.t "
                    "     OR LOWER(l.origin_table) LIKE '%.' || s.t"
                    ")",
                    [scope],
                ).fetchall()
                if uncovered:
                    details["semantic_model_coverage_gap"] = True
                    details["semantic_model_coverage_gap_tables"] = \
                        sorted({r[0] for r in uncovered})
            except Exception:
                # Don't mask the rest of the static layer on a probe glitch.
                pass

        try:
            rows = conn.execute(
                f"SELECT table_name, column_name, data_type "
                f"FROM information_schema.columns "
                f"WHERE table_schema='raw_sap' "
                f"AND LOWER(table_name) IN ({_in_list(len(scope))}) "
                f"ORDER BY table_name, ordinal_position",
                scope,
            ).fetchall()
            details["information_schema"] = len(rows)
            # Emit info_schema only if dict-style sources returned nothing
            if rows and details["sap_data_dictionary"] == 0 and \
                    details["source_column_roles"] == 0:
                parts += ["## information_schema.columns (fallback)",
                          _csv_fmt(["table", "column", "type"], rows)]
        except Exception as _e:
            _loud_fail_or_swallow(_e, "information_schema.columns")
    else:
        # Strategy 5 — no scope: include a small info_schema sample
        try:
            rows = conn.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns WHERE table_schema='raw_sap' "
                "ORDER BY table_name, ordinal_position LIMIT 300"
            ).fetchall()
            details["information_schema"] = len(rows)
            if rows:
                parts += ["## information_schema.columns (unscoped sample)",
                          _csv_fmt(["table", "column", "type"], rows)]
        except Exception as _e:
            _loud_fail_or_swallow(_e, "information_schema.columns")

    content = _truncate("\n\n".join(parts), budget)
    return content, _count_tokens(content), details


def _load_create_s2t_cardinality(scope: list[str], conn) -> str:
    """Direction F.1.1 — render the cardinality block for the Create S2T
    (and pre_s2t_reasoning) bundle.

    Reuses `_render_join_cardinality_block` from `_scope_derivation.py`,
    which applies the per_record_key > header_detail > catastrophic > no_signal
    prioritization spec'd in Direction D §6.1. Loads non-superseded
    `join_cardinality` DARs scoped to `scope_tables` via the F10-aware
    `list_contains(string_split(...))` lookup baked into the helper's
    own query.

    Returns the rendered markdown block with the brief's header prefix
    when content is non-trivial; empty string when no cardinality
    evidence applies (so `_load_dynamic` can skip cleanly without
    introducing a stray empty section).
    """
    if not scope:
        return ""
    try:
        from _scope_derivation import _render_join_cardinality_block
    except ImportError:
        return ""
    block = _render_join_cardinality_block(conn, list(scope))
    if not block or block.startswith("("):
        return ""
    body = block
    if body.startswith("## Join cardinality evidence"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.lstrip("\n")
    header = "## join_cardinality (scope-filtered, evidence-prioritized)"
    return header + "\n\n" + body


_SHAPE_BRIDGE_CAP = 5  # per-DAR cap on shapes and bridges (locked Step 4d)


def _compact_schema_discovery_result(raw_json: str, dar_id: str = "") -> str:
    """#95 + fix-up — schema_discovery result_json carries verbose evidence
    strings (~11.6K chars per row in BG027) that the LLM doesn't consume.
    Compacts to structural-facts-only:
      PK: <cols> (confidence=<v>)
      FK: <from> -> <to_table>.<to_cols> (RI=<pct>%, confidence=<bucket>)
      SHAPE: <shape> [<cardinality>] <pair[0]>↔<pair[1]> via <cols>
             (confidence=<v>[, sum_match=<int_pct>%])
      BRIDGE: <min(between)>↔<max(between)> via <via> [<path>]

    Caps shapes and bridges at _SHAPE_BRIDGE_CAP per DAR (alphabetical
    sort, deterministic — Step 4d Q1 confirmed both are uniform-confidence
    so no signal-based ordering is possible). When a cap fires, an
    explicit footer line surfaces the truncation count to the LLM
    (anti-pattern of #96 silent truncation).

    Fix-up history:
      - #95 (commit 819d3fe): initial compaction; SHAPE used wrong keys
        (kind / columns / key_columns) so all SHAPE lines rendered empty;
        bridge_tables had no branch and was silently dropped.
      - This fix: SHAPE uses correct live-data keys (shape, cardinality,
        pair, via_columns, sum_match_pct optional). bridge_tables now
        renders. Both capped per Step 4d's locked rendering policy.

    Falls back to raw JSON blob when result_json is malformed or has no
    recognized structural fields. dar_id is passed through to footers for
    the LLM to cite the source DAR if it wants the full list.
    """
    try:
        d = json.loads(raw_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return raw_json or ""
    if not isinstance(d, dict):
        return raw_json or ""
    lines: list[str] = []
    for pk in d.get("pk_candidates") or []:
        if not isinstance(pk, dict):
            continue
        cols = "+".join(str(c) for c in (pk.get("columns") or []))
        lines.append(f"PK: {cols} (confidence={pk.get('confidence')})")
    for fk in d.get("fk_candidates") or []:
        if not isinstance(fk, dict):
            continue
        from_cols = "+".join(str(c) for c in (fk.get("from_columns") or []))
        to_cols = "+".join(str(c) for c in (fk.get("to_columns") or []))
        lines.append(
            f"FK: {from_cols} -> {fk.get('to_table') or ''}.{to_cols} "
            f"(RI={fk.get('referential_integrity_pct')}%, "
            f"confidence={fk.get('confidence')})"
        )
    # SHAPE: live-data keys (shape, cardinality, pair, via_columns,
    # sum_match_pct optional). Cap + footer at _SHAPE_BRIDGE_CAP per DAR.
    shape_lines: list[str] = []
    for sh in d.get("relationship_shapes") or []:
        if not isinstance(sh, dict):
            continue
        pair = sh.get("pair") or []
        p0 = pair[0] if len(pair) > 0 else ""
        p1 = pair[1] if len(pair) > 1 else ""
        via = "+".join(str(c) for c in (sh.get("via_columns") or []))
        extras = ""
        if "sum_match_pct" in sh:
            try:
                pct_int = round(float(sh.get("sum_match_pct") or 0) * 100)
                extras = f", sum_match={pct_int}%"
            except (TypeError, ValueError):
                pass
        shape_lines.append(
            f"SHAPE: {sh.get('shape') or ''} [{sh.get('cardinality') or ''}] "
            f"{p0}↔{p1} via {via} "
            f"(confidence={sh.get('confidence') or ''}{extras})"
        )
    shape_lines.sort()
    if len(shape_lines) > _SHAPE_BRIDGE_CAP:
        kept = shape_lines[:_SHAPE_BRIDGE_CAP]
        n_more = len(shape_lines) - _SHAPE_BRIDGE_CAP
        kept.append(
            f"({n_more} additional shapes; "
            f"see {dar_id or 'source DAR'} for full list)"
        )
        lines.extend(kept)
    else:
        lines.extend(shape_lines)
    # BRIDGE: render between as alphabetically-sorted pair so output is
    # deterministic regardless of stored order. Path verbatim.
    bridge_lines: list[str] = []
    for b in d.get("bridge_tables") or []:
        if not isinstance(b, dict):
            continue
        between = sorted(str(x) for x in (b.get("between") or []))
        b0 = between[0] if len(between) > 0 else ""
        b1 = between[1] if len(between) > 1 else ""
        bridge_lines.append(
            f"BRIDGE: {b0}↔{b1} via {b.get('via') or ''} "
            f"[{b.get('path') or ''}]"
        )
    bridge_lines.sort()
    if len(bridge_lines) > _SHAPE_BRIDGE_CAP:
        kept = bridge_lines[:_SHAPE_BRIDGE_CAP]
        n_more = len(bridge_lines) - _SHAPE_BRIDGE_CAP
        kept.append(
            f"({n_more} additional bridges; "
            f"see {dar_id or 'source DAR'} for full list)"
        )
        lines.extend(kept)
    else:
        lines.extend(bridge_lines)
    if not lines:
        return raw_json or ""
    return "\n".join(lines)


def _compact_bridge_coverage_result(raw_json: str, dar_id: str = "") -> str:
    """Option B Phase 3 — compact renderer for bridge_coverage_by_filter
    DARs. Mirrors _compact_schema_discovery_result's voice + cap pattern.

    Output line format (per design doc Component 4):
      BRIDGE-COVERAGE [DAR-XXXXX]: <from>-><via>-><to> | filter: <t>.<col>
        reachable: ['v1', 'v2', ...]
        unreachable: ['v1', 'v2', ...] (+N more)

    For Phase 1's FK-pair structure (via_table=null), via collapses to
    just <from>-><to>. unreachable_values is capped at 5 with overflow
    indicator. status='skipped' DARs (high-cardinality cases) render
    as a one-line note since their reach/unreach lists are empty.

    Falls back to raw JSON when result_json is malformed.
    """
    if not raw_json:
        return ""
    try:
        d = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return raw_json
    if not isinstance(d, dict) or not d:
        return raw_json
    bridge = d.get("bridge") or {}
    fcol = d.get("filter_column") or {}
    from_t = bridge.get("from_table") or ""
    via_t = bridge.get("via_table")
    to_t = bridge.get("to_table") or ""
    path = (
        f"{from_t}->{via_t}->{to_t}"
        if via_t else f"{from_t}->{to_t}"
    )
    fcol_t = fcol.get("table") or ""
    fcol_c = fcol.get("column") or ""
    header = (
        f"BRIDGE-COVERAGE [{dar_id or '?'}]: {path} | "
        f"filter: {fcol_t}.{fcol_c}"
    )
    skip_reason = d.get("skip_reason") or ""
    if skip_reason:
        return f"{header}\n  skipped: {skip_reason}"
    reach = [r.get("value") for r in (d.get("reachable_values") or [])]
    unreach = list(d.get("unreachable_values") or [])
    if not reach and not unreach:
        # Malformed / empty body — fall back.
        return raw_json or header
    cap = _SHAPE_BRIDGE_CAP
    unreach_disp = unreach[:cap]
    extra = len(unreach) - cap
    suffix = f" (+{extra} more)" if extra > 0 else " (+0 more)"
    return (
        f"{header}\n"
        f"  reachable: {reach}\n"
        f"  unreachable: {unreach_disp}{suffix}"
    )


def _load_dynamic(conn, scope: list[str], term_id: Optional[str],
                  budget: int,
                  purpose: Optional[str] = None) -> tuple[str, int, dict]:
    details = {"domain_analysis_results": 0,
               "business_term_analysis_results": 0,
               "analysis_findings": 0,
               "term_analysis_results": 0,
               "term_analysis_results_cited": 0,
               "join_cardinality_rendered": False}
    parts: list[str] = []

    # Direction F.1.2 — render cardinality block FIRST so it lands before
    # the generic DAR dump and gets prominent placement in the LLM's
    # context. Only for purposes that consume cardinality evidence.
    if purpose in ("create_s2t", "pre_s2t_reasoning") and scope:
        cardinality_block = _load_create_s2t_cardinality(scope, conn)
        if cardinality_block:
            parts.append(cardinality_block)
            details["join_cardinality_rendered"] = True

    # P7.4 Fix 3 precondition: emit primary-key IDs so the LLM can cite them
    # verbatim. Prior loaders dropped id/fact_id from SELECT, making the
    # "copy the ID from bundle" directive architecturally impossible.
    if scope:
        # #93 — when the dedicated cardinality block runs (purpose in
        # create_s2t / pre_s2t_reasoning, gate at L962-966), exclude
        # join_cardinality from the generic DAR dump. Cardinality
        # evidence is rendered separately as structured per-pair entries
        # via _load_create_s2t_cardinality / _render_join_cardinality_block;
        # leaving raw blobs in the generic dump only duplicates content.
        # For other purposes (no dedicated block runs), keep the generic
        # dump unchanged so cardinality evidence isn't lost entirely.
        if purpose in ("create_s2t", "pre_s2t_reasoning"):
            cardinality_filter = (
                "  AND analysis_type != 'join_cardinality' "
            )
        else:
            cardinality_filter = ""
        # #95 — per-(analysis_type, source_tables, col_name)-pair
        # surfacing via DuckDB QUALIFY ROW_NUMBER. Guarantees one row per
        # partition so per-table EDA evidence isn't crowded out when newer
        # batches run on a subset of scope (e.g. mard's 2026-04-24 rows
        # losing to a 2026-04-25 equi/mseg/mkpf/objk re-run under the old
        # recency-only LIMIT 50). JSON-extracted col_name keeps
        # performance_baseline multi-column rows intact (one row per
        # numeric column). Scope filter pulled into SQL via list_intersect
        # — eliminates the Python post-filter `matching = [...]` pass.
        # LIMIT raised 50→100 per Step 4c R1/R2: BG027's 58 partitions
        # is the current max across 27 measured terms (median 14, p75 29);
        # 100 provides headroom for future Stage A scope-derivation growth
        # without measurable token cost today (no term currently
        # approaches 75 partitions).
        scope_param = [t.lower() for t in scope]
        try:
            try:
                rows = conn.execute(
                    "SELECT id, analysis_type, source_tables, status, "
                    "result_json "
                    "FROM main_seeds.domain_analysis_results "
                    "WHERE (superseded_by IS NULL OR superseded_by = '') "
                    f"{cardinality_filter}"
                    "  AND len(list_intersect("
                    "        string_split(LOWER(source_tables), ','), "
                    "        ?::VARCHAR[]"
                    "  )) > 0 "
                    "QUALIFY ROW_NUMBER() OVER ("
                    "    PARTITION BY analysis_type, source_tables, "
                    "      COALESCE(json_extract_string(result_json, "
                    "                                   '$.col_name'), '') "
                    "    ORDER BY executed_at_utc DESC"
                    ") = 1 "
                    "ORDER BY executed_at_utc DESC LIMIT 100",
                    [scope_param],
                ).fetchall()
                has_id = True
            except Exception:
                rows = conn.execute(
                    "SELECT analysis_type, source_tables, status, result_json "
                    "FROM main_seeds.domain_analysis_results "
                    "WHERE (superseded_by IS NULL OR superseded_by = '') "
                    f"{cardinality_filter}"
                    "  AND len(list_intersect("
                    "        string_split(LOWER(source_tables), ','), "
                    "        ?::VARCHAR[]"
                    "  )) > 0 "
                    "QUALIFY ROW_NUMBER() OVER ("
                    "    PARTITION BY analysis_type, source_tables, "
                    "      COALESCE(json_extract_string(result_json, "
                    "                                   '$.col_name'), '') "
                    "    ORDER BY executed_at_utc DESC"
                    ") = 1 "
                    "ORDER BY executed_at_utc DESC LIMIT 100",
                    [scope_param],
                ).fetchall()
                rows = [("",) + r for r in rows]  # prepend blank id
                has_id = False
            # #95 — scope filter is in SQL now (list_intersect WHERE
            # clause); the Python-side `matching = [r for r in rows if
            # any(t in scope_set for t in _split_csv_col(r[2]))]` pass
            # is no longer needed. Variable name preserved for the
            # downstream renderer.
            matching = rows
            details["domain_analysis_results"] = len(matching)
            if matching:
                header = (
                    "## domain_analysis_results (scoped — ID column is first; "
                    "format DAR-NNNNN)"
                )
                # C1 — sub-item 4: success rows render as plain CSV
                # (unchanged byte-shape vs pre-C1); non-success rows get a
                # prepended STATUS=SKIPPED / STATUS=ERROR header so the LLM
                # can distinguish "analyzer ruled inapplicable" from "no
                # evidence found." status column drives rendering but is
                # not surfaced in the CSV body (kept at 4 cols to preserve
                # token shape on the dominant success path; 207/248 today).
                def _q(v):
                    s = "" if v is None else str(v)
                    if "," in s or '"' in s or "\n" in s:
                        return '"' + s.replace('"', '""') + '"'
                    return s
                csv_cols = ["id", "analysis_type", "source_tables",
                            "result_json"]
                body_lines: list[str] = [",".join(csv_cols)]
                for r in matching:
                    status_val = str(r[3] or "").lower()
                    if status_val == "skipped":
                        try:
                            rj = json.loads(r[4] or "{}")
                            skip_reason = (
                                rj.get("skip_reason")
                                if isinstance(rj, dict) else None
                            )
                        except (json.JSONDecodeError, TypeError):
                            skip_reason = None
                        if not skip_reason:
                            skip_reason = "(not provided)"
                        body_lines.append(
                            "STATUS=SKIPPED — analyzer could not apply."
                        )
                        body_lines.append(f"skip_reason: {skip_reason}")
                    elif status_val == "error":
                        body_lines.append(
                            "STATUS=ERROR — analyzer raised an exception."
                        )
                        body_lines.append("(see result_json for trace)")
                    # #95 — schema_discovery rows render in compact form
                    # (PK/FK/SHAPE/BRIDGE one per line, evidence prose
                    # dropped). All other analysis_types keep raw
                    # result_json blob. Fix-up: helper takes DAR id so
                    # cap footers can cite the source DAR.
                    if str(r[1] or "").lower() == "schema_discovery":
                        rendered_rj = _compact_schema_discovery_result(
                            r[4], dar_id=str(r[0] or "")
                        )
                    elif str(r[1] or "").lower() == "bridge_coverage_by_filter":
                        # Option B Phase 3 — compact renderer for the
                        # Phase-1 analyzer's DARs (Component 4).
                        rendered_rj = _compact_bridge_coverage_result(
                            r[4], dar_id=str(r[0] or "")
                        )
                    else:
                        rendered_rj = r[4]
                    body_lines.append(
                        ",".join(_q(c) for c in (r[0], r[1], r[2], rendered_rj))
                    )
                parts += [header, "\n".join(body_lines)]
        except Exception:
            pass

    if term_id:
        try:
            try:
                rows = conn.execute(
                    "SELECT id, analysis_type, source_tables, result_json "
                    "FROM main_seeds.business_term_analysis_results "
                    "WHERE business_term_id = ? "
                    "ORDER BY executed_at_utc DESC LIMIT 50",
                    [term_id],
                ).fetchall()
            except Exception:
                _rows = conn.execute(
                    "SELECT analysis_type, source_tables, result_json "
                    "FROM main_seeds.business_term_analysis_results "
                    "WHERE business_term_id = ? "
                    "ORDER BY executed_at_utc DESC LIMIT 50",
                    [term_id],
                ).fetchall()
                rows = [("",) + r for r in _rows]
            details["business_term_analysis_results"] = len(rows)
            if rows:
                header = (
                    "## business_term_analysis_results "
                    "(term-scoped — ID column is first; format BAR-NNNNN)"
                )
                parts += [header,
                          _csv_fmt(["id", "analysis_type", "source_tables",
                                    "result_json"], rows)]
        except Exception:
            pass

    # analysis_findings: scope overlap OR term_id match (§3b predicate)
    if scope or term_id:
        try:
            try:
                rows = conn.execute(
                    "SELECT id, finding_type, query_description, result_summary, "
                    "tables_explored, business_term_id "
                    "FROM main_seeds.analysis_findings LIMIT 200"
                ).fetchall()
            except Exception:
                _rows = conn.execute(
                    "SELECT finding_type, query_description, result_summary, "
                    "tables_explored, business_term_id "
                    "FROM main_seeds.analysis_findings LIMIT 200"
                ).fetchall()
                rows = [("",) + r for r in _rows]
            scope_set = set(scope) if scope else set()
            # After include-id, indices shift: tables_explored=4, business_term_id=5
            matching = [
                (r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows
                if (term_id and r[5] == term_id)
                or (scope_set and any(t in scope_set
                                       for t in _split_csv_col(r[4])))
            ]
            details["analysis_findings"] = len(matching)
            if matching:
                header = (
                    "## analysis_findings (scope/term-matched — ID column is "
                    "first; format AFNNN)"
                )
                parts += [header,
                          _csv_fmt(["id", "finding_type", "description",
                                    "summary", "tables", "term"], matching)]
        except Exception:
            pass

    # C4 (Theme 1 sub-item 5): Stage A blockers section. Rendered
    # term-scoped when term_id is given. Inserted BEFORE the TAR section
    # because Stage A is causally upstream of Stage C — the iteration
    # LLM reads the question (blockers) immediately before Stage C's
    # answer (blockers_resolution rendered inside the TAR section).
    # Tolerance: any failure in the loader returns ([], 0); the empty
    # render emits an empty string; bundle assembly continues unaffected.
    if term_id:
        try:
            from _stage_a_blocker_loader import (  # noqa: E402
                load_blockers_for_term,
                render_stage_a_blockers_section,
            )
            sa_entries, sa_trunc = load_blockers_for_term(term_id)
            sa_section = render_stage_a_blockers_section(sa_entries, sa_trunc)
            if sa_section:
                parts.append(sa_section)
        except Exception:
            # Stage A absence is a valid steady state; don't fail the
            # whole bundle if the loader trips on anything unexpected.
            pass

    # Stage C (Piece 9 §28.11.7): Term EDA analytical characterization.
    # Rendered term-scoped when term_id is given. Sub-budget = budget//5
    # (per F1 from preflight) — protects TAR from competing unbounded
    # while not regressing existing sub-loads.
    if term_id:
        try:
            tar_sub_budget = max(budget // 5, 512)
            tar_rows = _load_term_analysis_results(conn, term_id)
            cited = _dereference_cited_tars(conn, tar_rows)
            tar_section, tar_toks, cited_count = _render_tar_section(
                tar_rows, cited, tar_sub_budget,
            )
            details["term_analysis_results"] = len([
                r for r in tar_rows if r.get("row_type") == "query"
            ])
            details["term_analysis_results_cited"] = cited_count
            if tar_section:
                parts.append(tar_section)
        except Exception:
            # Stage C absence is a valid steady state; don't fail the
            # whole bundle if TAR load trips on anything unexpected.
            pass

    content = _truncate("\n\n".join(parts), budget)
    return content, _count_tokens(content), details


# ─── Stage C TAR loader helpers (v5 §28.11.7) ──────────────────────────

def _load_term_analysis_results(
    conn, term_id: str,
) -> list[dict]:
    """Latest-success TAR rows for term_id. Returns sufficiency row first
    (single row at most) followed by query rows sorted by (stage,
    query_index). Returns empty list if no success rows exist (term
    hasn't run Stage C yet, or all prior runs are superseded)."""
    try:
        rows = conn.execute(
            """
            SELECT id, term_id, row_type, analysis_lens, stage, query_index,
                   query_sql, query_result_json, result_row_count,
                   interpretation, grounded_in_tar_ids, sufficiency_json,
                   status, confidence, executed_at_utc, run_id
            FROM main_seeds.term_analysis_results
            WHERE term_id = ? AND status = 'success'
            """,
            [term_id],
        ).fetchall()
    except Exception:
        return []
    cols = ["id", "term_id", "row_type", "analysis_lens", "stage",
            "query_index", "query_sql", "query_result_json",
            "result_row_count", "interpretation", "grounded_in_tar_ids",
            "sufficiency_json", "status", "confidence",
            "executed_at_utc", "run_id"]
    dicts = [dict(zip(cols, r)) for r in rows]

    def sort_key(d: dict) -> tuple:
        if d.get("row_type") == "sufficiency":
            return (0, 0, 0)
        stage_order = {
            "framework_floor": 1,
            "reflection": 2,
            "sufficiency_loop": 3,
            "terminal": 4,
        }
        return (
            1,
            stage_order.get(str(d.get("stage")), 9),
            int(d.get("query_index") or 0),
        )

    dicts.sort(key=sort_key)
    return dicts


def _dereference_cited_tars(
    conn, tar_rows: list[dict],
) -> list[dict]:
    """Follow grounded_in_tar_ids across tar_rows; fetch cited TAR rows.
    Includes superseded rows per v5 Edit 7 (Piece 8 renders staleness
    annotation). Dedupes by id."""
    cited_ids: set[str] = set()
    for row in tar_rows:
        raw = row.get("grounded_in_tar_ids") or ""
        if not raw:
            continue
        try:
            ids = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(ids, list):
            cited_ids.update(str(x) for x in ids if x)
    if not cited_ids:
        return []
    placeholders = ",".join("?" * len(cited_ids))
    try:
        # Stage D.1 — strict archive cascade (§28.11.8). JOIN business_glossary
        # and filter archived source terms so citation chains don't resurrect
        # archived-term evidence into new bundles.
        rows = conn.execute(
            f"""
            SELECT tar.id, tar.term_id, tar.row_type, tar.analysis_lens,
                   tar.stage, tar.query_index, tar.query_sql,
                   tar.query_result_json, tar.result_row_count,
                   tar.interpretation, tar.status, tar.executed_at_utc
            FROM main_seeds.term_analysis_results tar
            JOIN main_seeds.business_glossary bg ON bg.id = tar.term_id
            WHERE tar.id IN ({placeholders})
              AND bg.status != 'archived'
            """,
            sorted(cited_ids),
        ).fetchall()
    except Exception:
        return []
    cols = ["id", "term_id", "row_type", "analysis_lens", "stage",
            "query_index", "query_sql", "query_result_json",
            "result_row_count", "interpretation", "status",
            "executed_at_utc"]
    return [dict(zip(cols, r)) for r in rows]


def _render_tar_section(
    tar_rows: list[dict],
    cited_rows: list[dict],
    budget_tokens: int,
) -> tuple[str, int, int]:
    """Render Term EDA analytical characterization section.

    Returns (content, token_count, cited_rows_rendered_count).
    Empty tar_rows -> empty content. Budget truncation prioritizes
    dropping cited rows > sufficiency rationale > query interpretations.
    """
    if not tar_rows:
        return "", 0, 0

    lines: list[str] = []
    lines.append("## Term EDA analytical characterization")

    # Sufficiency summary first (if present).
    sufficiency = next(
        (r for r in tar_rows if r.get("row_type") == "sufficiency"),
        None,
    )
    if sufficiency:
        try:
            sj = json.loads(sufficiency.get("sufficiency_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            sj = {}
        lines.append(
            f"- Sufficiency: declared_sufficient="
            f"{sj.get('declared_sufficient')!s}; "
            f"confidence={sufficiency.get('confidence') or ''}; "
            f"iterations={sj.get('sufficiency_loop_iterations', 0)}"
        )
        if sj.get("sufficiency_rationale"):
            lines.append(f"  rationale: {sj['sufficiency_rationale']}")
        if sj.get("reflection_summary"):
            lines.append(f"  reflection: {sj['reflection_summary']}")

        lc = sj.get("lens_consideration") or {}
        if lc:
            lines.append("- Lens consideration:")
            for lens, entry in lc.items():
                if not isinstance(entry, dict):
                    continue
                lines.append(
                    f"  - {lens}: {entry.get('decision', '?')} "
                    f"— {entry.get('rationale', '')[:200]}"
                )

        br = sj.get("blockers_resolution") or []
        if br:
            lines.append("- Blockers resolution:")
            for b in br:
                if not isinstance(b, dict):
                    continue
                lines.append(
                    f"  - {b.get('blocker_short_title', '?')}: "
                    f"{b.get('status', '?')} — "
                    f"{(b.get('evidence') or '')[:220]}"
                )

    # Query rows.
    query_rows = [r for r in tar_rows if r.get("row_type") == "query"]
    if query_rows:
        lines.append("- Query rows:")
        for q in query_rows:
            lines.append(
                f"  - {q.get('id')} | lens={q.get('analysis_lens')} | "
                f"stage={q.get('stage')} | rows={q.get('result_row_count') or 0}"
            )
            sql = (q.get("query_sql") or "")[:200]
            if sql:
                lines.append(f"    sql: {sql}")
            interp = (q.get("interpretation") or "")[:220]
            if interp:
                lines.append(f"    interpretation: {interp}")

    # Cited cross-term rows with staleness annotation.
    cited_rendered = 0
    if cited_rows:
        lines.append("- Cited prior TARs (grounding evidence from other terms):")
        for c in cited_rows:
            status = c.get("status") or ""
            note = ""
            if status == "superseded":
                note = (
                    f"  [CITATION NOTE: TAR {c.get('id')} is superseded; "
                    f"historical evidence only.]"
                )
            lines.append(
                f"  - {c.get('id')} | term={c.get('term_id')} | "
                f"lens={c.get('analysis_lens')} | rows={c.get('result_row_count') or 0}"
            )
            if note:
                lines.append(note)
            interp = (c.get("interpretation") or "")[:200]
            if interp:
                lines.append(f"    interpretation: {interp}")
            cited_rendered += 1

    content = "\n".join(lines)
    toks = _count_tokens(content)
    if toks > budget_tokens:
        # Simple truncation when over-budget: keep the summary, drop
        # cited rows first, then query interpretations.
        content = _truncate(content, budget_tokens)
        toks = _count_tokens(content)
    return content, toks, cited_rendered


def _load_ontology(conn, scope: list[str], term_id: Optional[str],
                   budget: int) -> tuple[str, int, dict]:
    details = {"dbt_column_lineage": 0, "existing_models": 0,
               "s2t_mapping": 0}
    parts: list[str] = []

    if scope:
        try:
            # known_issue #74: origin_table stores raw sources as
            # 'raw_sap.<t>' (dominant) or bare '<t>' (less common); match
            # both forms plus '%.<t>' defense-in-depth, mirroring
            # compile_semantic_model.has_ontology_coverage.
            bare_ph = _in_list(len(scope))
            qual_ph = _in_list(len(scope))
            like_exprs = " OR ".join(
                ["LOWER(origin_table) LIKE ?"] * len(scope)
            )
            params = (
                list(scope)
                + [f"raw_sap.{t}" for t in scope]
                + [f"%.{t}" for t in scope]
            )
            rows = conn.execute(
                f"SELECT model_name, layer, column_name, origin_table, origin_column, transformation_type "
                f"FROM main_seeds.dbt_column_lineage "
                f"WHERE LOWER(origin_table) IN ({bare_ph}) "
                f"   OR LOWER(origin_table) IN ({qual_ph}) "
                f"   OR ({like_exprs}) "
                f"ORDER BY layer, model_name",
                params,
            ).fetchall()
            details["dbt_column_lineage"] = len(rows)
            if rows:
                parts += ["## dbt_column_lineage (scoped by origin)",
                          _csv_fmt(["model", "layer", "column",
                                    "origin_table", "origin_column",
                                    "transform"], rows)]
        except Exception:
            pass

        try:
            rows = conn.execute(
                f"SELECT business_term_id, source_table, source_field, target_model, target_column "
                f"FROM main_seeds.s2t_mapping "
                f"WHERE LOWER(source_table) IN ({_in_list(len(scope))})",
                scope,
            ).fetchall()
            details["s2t_mapping"] = len(rows)
            if rows:
                parts += ["## s2t_mapping (scoped)",
                          _csv_fmt(["term_id", "source_table", "source_field",
                                    "target_model", "target_column"], rows)]
        except Exception:
            pass

    try:
        rows = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema IN ('main_staging','main_vault','main_marts','main_obt') "
            "ORDER BY table_schema, table_name"
        ).fetchall()
        details["existing_models"] = len(rows)
        if rows:
            parts += ["## existing_models",
                      _csv_fmt(["schema", "table"], rows)]
    except Exception:
        pass

    content = _truncate("\n\n".join(parts), budget)
    return content, _count_tokens(content), details


def _load_examples(conn, scope: list[str], term_id: Optional[str],
                   budget: int) -> tuple[str, int, dict]:
    details = {"abap_logic_catalog": 0}
    parts: list[str] = []
    if scope:
        try:
            rows = conn.execute(
                "SELECT program_name, description, tables_read, tables_written, "
                "business_rule_plain, risk_level FROM main_seeds.abap_logic_catalog"
            ).fetchall()
            scope_upper = {t.upper() for t in scope}

            def _hits(row):
                blob = (str(row[2] or "") + " " + str(row[3] or "")).upper()
                return any(t in blob for t in scope_upper)

            matching = [r for r in rows if _hits(r)]
            details["abap_logic_catalog"] = len(matching)
            if matching:
                parts += ["## abap_logic_catalog (scope-filtered)",
                          _csv_fmt(["program", "description", "reads",
                                    "writes", "rule", "risk"], matching)]
        except Exception:
            pass
    content = _truncate("\n\n".join(parts), budget)
    return content, _count_tokens(content), details


def _load_business(conn, scope: list[str], term_id: Optional[str],
                   budget: int) -> tuple[str, int, dict]:
    details = {"domain_facts": 0, "business_glossary": 0,
               "sat_vendor_business": 0, "sat_material_business": 0,
               "procurement_rules": 0, "org_structure": 0}
    parts: list[str] = []

    if scope:
        try:
            # P7.4 Fix 3 precondition: emit fact_id so the LLM can cite DF-NNNN
            # verbatim. Fallback to no-id shape if fixture schema lacks the column.
            try:
                rows = conn.execute(
                    "SELECT fact_id, category, scope_tables, fact_technical "
                    "FROM main_seeds.domain_facts "
                    "WHERE status='active' AND auto_inject=true"
                ).fetchall()
            except Exception:
                _rows = conn.execute(
                    "SELECT category, scope_tables, fact_technical "
                    "FROM main_seeds.domain_facts "
                    "WHERE status='active' AND auto_inject=true"
                ).fetchall()
                rows = [("",) + r for r in _rows]
            scope_set = set(scope)
            # After include-id: scope_tables is at index 2
            matching = [
                r for r in rows
                if any(t in scope_set for t in _split_csv_col(r[2]))
            ]
            details["domain_facts"] = len(matching)
            if matching:
                header = (
                    "## domain_facts (scope-filtered — ID column is "
                    "'fact_id' first; format DF-NNNN)"
                )
                parts += [header,
                          _csv_fmt(["fact_id", "category", "scope", "fact"],
                                   matching)]
        except Exception:
            pass

    if term_id:
        try:
            rows = conn.execute(
                "SELECT term_name, display_name, definition, grain, unit, domain "
                "FROM main_seeds.business_glossary WHERE id = ?",
                [term_id],
            ).fetchall()
            details["business_glossary"] = len(rows)
            if rows:
                parts += ["## business_glossary (term)",
                          _csv_fmt(["term_name", "display_name", "definition",
                                    "grain", "unit", "domain"], rows)]
        except Exception:
            pass

    # Vendor business enrichment — vault-sourced (2026-05-05). Reads
    # hub_vendor + sat_vendor_general + sat_vendor_business so the LLM
    # sees SAP master (vendor_id, name, country) joined to genuinely-HT
    # fields (equipment_types, quality_rating, contract_status) on
    # hk_vendor. Phase β (2026-05-05): lead_time_days dropped from this
    # sat — that data lives in MARC.PLIFZ (per material/plant) and is
    # exposed via sat_material_plant.
    try:
        rows = conn.execute("""
            SELECT
                hv.vendor_id,
                sgen.vendor_name,
                sgen.country_code,
                svb.equipment_types,
                svb.quality_rating,
                svb.contract_status
            FROM main_vault.hub_vendor hv
            JOIN main_vault.sat_vendor_general sgen
                ON hv.hk_vendor = sgen.hk_vendor
            JOIN main_vault.sat_vendor_business svb
                ON hv.hk_vendor = svb.hk_vendor
            WHERE sgen.load_date = (
                SELECT MAX(load_date) FROM main_vault.sat_vendor_general
            )
              AND svb.load_date = (
                SELECT MAX(load_date) FROM main_vault.sat_vendor_business
            )
            LIMIT 30
        """).fetchall()
        details["sat_vendor_business"] = len(rows)
        if rows:
            parts += [
                "## sat_vendor_business (vault join: hub_vendor + "
                "sat_vendor_general + sat_vendor_business)",
                _csv_fmt(
                    ["vendor_id", "name", "country", "equipment_types",
                     "quality", "contract_status"],
                    rows,
                ),
            ]
    except Exception:
        pass

    # Material business enrichment — vault-sourced (2026-05-05). Reads
    # hub_material + sat_material_description + sat_material_business +
    # sat_vendor_general so the LLM sees SAP material master joined to
    # genuinely-HT fields (lifecycle_months, primary_vendor resolved via
    # hk_vendor). Phase β (2026-05-05): avg_unit_cost_eur dropped — unit
    # prices live in sat_po_item.unit_price (EKPO.NETPR) and standard
    # costs in MBEW.STPRS.
    try:
        rows = conn.execute("""
            SELECT
                hm.material_number,
                des.material_description,
                smb.lifecycle_months,
                smb.primary_vendor_id,
                vgen.vendor_name AS primary_vendor_name,
                smb.notes
            FROM main_vault.hub_material hm
            JOIN main_vault.sat_material_business smb
                ON hm.hk_material = smb.hk_material
            LEFT JOIN main_vault.sat_material_description des
                ON hm.hk_material = des.hk_material
                AND des.load_date = (
                    SELECT MAX(load_date)
                    FROM main_vault.sat_material_description
                )
            LEFT JOIN main_vault.hub_vendor hv
                ON smb.primary_vendor_id = hv.vendor_id
            LEFT JOIN main_vault.sat_vendor_general vgen
                ON hv.hk_vendor = vgen.hk_vendor
                AND vgen.load_date = (
                    SELECT MAX(load_date)
                    FROM main_vault.sat_vendor_general
                )
            WHERE smb.load_date = (
                SELECT MAX(load_date) FROM main_vault.sat_material_business
            )
            LIMIT 30
        """).fetchall()
        details["sat_material_business"] = len(rows)
        if rows:
            parts += [
                "## sat_material_business (vault join: hub_material + "
                "sat_material_description + sat_material_business + "
                "primary vendor resolved via hub_vendor)",
                _csv_fmt(
                    ["material_number", "description",
                     "lifecycle_months", "primary_vendor_id",
                     "primary_vendor_name", "notes"],
                    rows,
                ),
            ]
    except Exception:
        pass

    for seed, cols, header in (
        ("procurement_rules", "rule_id, rule_name, rule_category",
         ["rule_id", "name", "category"]),
        ("org_structure", "entity_type, entity_code, description",
         ["entity_type", "code", "description"]),
    ):
        try:
            rows = conn.execute(
                f"SELECT {cols} FROM main_seeds.{seed} LIMIT 30"
            ).fetchall()
            details[seed] = len(rows)
            if rows:
                parts += [f"## {seed}", _csv_fmt(header, rows)]
        except Exception:
            pass

    content = _truncate("\n\n".join(parts), budget)
    return content, _count_tokens(content), details


def _load_archived(conn, scope: list[str], term_id: Optional[str],
                   budget: int) -> tuple[str, int, dict]:
    details = {"archive_log": 0}
    parts: list[str] = []
    if term_id:
        try:
            g = conn.execute(
                "SELECT term_name FROM main_seeds.business_glossary WHERE id = ?",
                [term_id],
            ).fetchone()
            if g:
                rows = conn.execute(
                    "SELECT archive_id, term_name, archived_reason_code, archived_reason_text, archived_at_utc "
                    "FROM main_seeds.archive_log "
                    "WHERE term_name = ? AND CAST(learning_signal AS VARCHAR) ILIKE 'true' "
                    "ORDER BY archived_at_utc DESC LIMIT 3",
                    [g[0]],
                ).fetchall()
                details["archive_log"] = len(rows)
                if rows:
                    parts += ["## archive_log (prior attempts)",
                              _csv_fmt(["archive_id", "term_name", "reason_code",
                                        "reason_text", "archived_at"], rows)]
        except Exception:
            pass
    content = _truncate("\n\n".join(parts), budget)
    return content, _count_tokens(content), details


LAYER_LOADERS = {
    "static": _load_static,
    "dynamic": _load_dynamic,
    "ontology": _load_ontology,
    "examples": _load_examples,
    "business": _load_business,
    "archived": _load_archived,
}


def _layer_is_empty(details: dict, layer: str) -> bool:
    sources = EMPTY_SOURCES[layer]
    return not any(details.get(s, 0) > 0 for s in sources)


# =========================================================================
# Telemetry (§3l)
# =========================================================================

def _write_telemetry(record: dict) -> None:
    TELEMETRY_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str) + "\n"
    with TELEMETRY_LOG.open("a", encoding="utf-8", newline="") as f:
        f.write(line)


# =========================================================================
# Main entry (§3a)
# =========================================================================

def assemble_context(
    *,
    purpose: str,
    term_id: Optional[str] = None,
    scope_tables: Optional[list[str]] = None,
    max_tokens: int = 50_000,
    strict: bool = True,
    include_debug_metadata: bool = False,
    conn: Optional["duckdb.DuckDBPyConnection"] = None,
) -> ContextBundle:
    """See context/phase_15a_piece_4_context_assembly_helper.md §3a.

    v3.5 bugfix (8.3.1 Bug 1): accepts optional conn parameter.
    When provided, uses it (caller owns lifecycle — we do NOT close).
    When None, opens a local read-only connection and closes in finally.
    Fixes the DuckDB connection-config mismatch when a caller has already
    opened a read-write connection in the same process (e.g. piece 8's
    runner, which needs read-write for BAR writes).
    """
    t0 = time.perf_counter()
    if purpose not in PURPOSE_WEIGHTS:
        raise ValueError(f"unknown purpose: {purpose!r}")
    if not DB_PATH.exists():
        raise RuntimeError(f"DuckDB not found at {DB_PATH}")

    weights = PURPOSE_WEIGHTS[purpose]
    budgets = compute_layer_budgets(purpose, max_tokens)

    # Reset per-call tokenizer cache counters for telemetry
    _tokenizer_cache_stats["hits"] = 0
    _tokenizer_cache_stats["misses"] = 0

    _owned_conn = conn is None
    if _owned_conn:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
    scope_info: dict = {"strategy_used": "unknown", "resolved_tables": []}
    try:
        scope_info = resolve_scope(conn, term_id, scope_tables)
        scope = scope_info["resolved_tables"]

        layer_contents: dict[str, str] = {l: "" for l in LAYERS}
        layer_tokens: dict[str, int] = {l: 0 for l in LAYERS}
        layer_details: dict[str, dict] = {l: {} for l in LAYERS}

        for layer in LAYERS:
            if weights[layer] == "off":
                continue
            loader = LAYER_LOADERS[layer]
            # v3.7 §23.10 — static loader receives purpose so Layer B
            # dual-rendering can pick literal-ref (pre_s2t_reasoning) vs
            # Jinja (create_s2t) form. Other loaders don't need it and
            # accept the kwarg via default (None).
            if layer == "static":
                content, toks, details = loader(
                    conn, scope, term_id, budgets[layer], purpose=purpose
                )
            elif layer == "dynamic":
                # Direction F.1.2 — dynamic loader receives purpose so the
                # cardinality sub-block can render for create_s2t /
                # pre_s2t_reasoning only. Other purposes get the legacy
                # generic DAR dump unchanged.
                content, toks, details = loader(
                    conn, scope, term_id, budgets[layer], purpose=purpose
                )
            else:
                content, toks, details = loader(
                    conn, scope, term_id, budgets[layer]
                )
            layer_contents[layer] = content
            layer_tokens[layer] = toks
            layer_details[layer] = details

        # Strict-mode: raise on empty HEAVY (§3j)
        for layer in LAYERS:
            if weights[layer] == "HEAVY" and _layer_is_empty(layer_details[layer], layer):
                err = ContextDegradedError(
                    layer=layer,
                    reason=f"all sources empty: {layer_details[layer]}",
                    scope=scope,
                    weights=weights,
                )
                if strict:
                    raise err
                # strict=False: warn-only (record in details, continue)
                layer_details[layer]["_warning"] = str(err)

        header = (f"# Context bundle for purpose='{purpose}' "
                  f"(scope={scope}, strategy={scope_info['strategy_used']})")
        body: list[str] = [header]
        for layer in LAYERS:
            if layer_contents[layer]:
                body.append(
                    f"\n### Layer: {layer} "
                    f"(weight={weights[layer]}, tokens={layer_tokens[layer]})"
                )
                body.append(layer_contents[layer])
        formatted = "\n".join(body)
        total_tokens = _count_tokens(formatted)
        if total_tokens > max_tokens:
            raise ContextOverflowError(
                f"total_tokens {total_tokens} > max_tokens {max_tokens}; "
                f"per_layer={layer_tokens}"
            )

        fingerprint = compute_fingerprint(scope, purpose, max_tokens)

        debug = None
        if include_debug_metadata:
            debug = {
                "weights": weights,
                "budgets": budgets,
                "layer_details": layer_details,
                "fingerprint": fingerprint,
            }

        bundle = ContextBundle(
            formatted_prompt=formatted,
            token_count=total_tokens,
            layer_summary=layer_tokens,
            scope_resolution=scope_info,
            debug=debug,
            # v3.4 §20d: per-layer text exposure for Piece 8 caching.
            static_layer_text=layer_contents.get("static", ""),
            dynamic_layer_text=layer_contents.get("dynamic", ""),
            ontology_layer_text=layer_contents.get("ontology", ""),
            examples_layer_text=layer_contents.get("examples", ""),
            business_layer_text=layer_contents.get("business", ""),
            archived_layer_text=layer_contents.get("archived", ""),
        )

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        _write_telemetry({
            "timestamp_utc": now_iso_utc(),
            "purpose": purpose,
            "scope_tables": scope,
            "scope_strategy": scope_info["strategy_used"],
            "term_id": term_id,
            "strict": strict,
            "max_tokens": max_tokens,
            "per_layer_budget": budgets,
            "per_layer_actual": layer_tokens,
            "per_layer_row_count": {
                l: sum(v for v in layer_details[l].values()
                       if isinstance(v, int)) if layer_details[l] else 0
                for l in LAYERS
            },
            "tokenizer_cache_hits": _tokenizer_cache_stats["hits"],
            "tokenizer_cache_misses": _tokenizer_cache_stats["misses"],
            "schema_fingerprint": fingerprint,
            "total_tokens": total_tokens,
            "elapsed_ms": elapsed_ms,
            "result": "success",
        })
        return bundle
    except ContextDegradedError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        _write_telemetry({
            "timestamp_utc": now_iso_utc(),
            "purpose": purpose,
            "scope_tables": scope_info.get("resolved_tables", []),
            "scope_strategy": scope_info.get("strategy_used", "unknown"),
            "term_id": term_id,
            "strict": strict,
            "max_tokens": max_tokens,
            "elapsed_ms": elapsed_ms,
            "result": "error",
            "error_type": "ContextDegradedError",
            "error_details": str(e),
        })
        raise
    finally:
        if _owned_conn:
            conn.close()
