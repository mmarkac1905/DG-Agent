"""Phase 15b piece 8 §22.5 (v3.6) — Layer A semantic model compiler.

Compiles `dbt/seeds/semantic_model.csv` rows from the EDA framework's DAR
reservoir. One LLM synthesis call per raw source table; output is the
canonical SQL-writing conventions for that table (canonical alias,
primary key, typical joins, code-column decoder refs, reference SQL,
typical filters, common traps).

CLI:
  python scripts/compile_semantic_model.py                          # all approved terms
  python scripts/compile_semantic_model.py --term-ids BG001,BG027   # term-scoped subset
  python scripts/compile_semantic_model.py --tables ekbe,ekko       # direct table list
  python scripts/compile_semantic_model.py --dry-run                # preview without LLM calls

Compilation scope — all raw tables with sufficient DAR coverage. Skip
conditions: (a) human-protected rows (populated_by='human_override' or
review_state='human_reviewed') are preserved verbatim via the final
merge; (b) DAR-incomplete tables are skipped (insufficient grounding
for synthesis). Auto-generated rows are refreshed on recompile.

The prior §22.2 "skip if ontology-covered" gate was removed as part
of known_issue #79 — Layer A (LLM-synthesized table-level narrative:
canonical_alias, typical_filters, common_traps, reference_sql,
typical_use_cases, typical_values_range_json, natural_thresholds_json)
and the dbt ontology layer (dbt_column_lineage: auto-generated
column-level structural lineage) are non-overlapping context
dimensions in Piece 8's bundle. Both consumers need both layers.

Invariants:
- LF line endings (anti-pattern #48 / #50)
- csv-safeguard boundary (anti-pattern #56)
- conn param pattern (anti-pattern #31 / 8.3.1 fix pattern)
- RULE 36 timestamp formatting
- human_override / human_reviewed rows preserved across recompile (§22.5 step 4)

Exit codes:
  0 — success
  1 — LLM call failed (non-retryable) or API key missing
  2 — CSV write refused by safeguard
  3 — no tables to compile (empty scope)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import duckdb
import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SEED_DIR = _PROJECT_ROOT / "dbt" / "seeds"
_DB_PATH = _PROJECT_ROOT / "cpe_analytics.duckdb"
_ENV_PATH = _PROJECT_ROOT / ".env"
_PROMPT_PATH = _PROJECT_ROOT / "scripts" / "prompts" / "semantic_model_compilation_prompt.md"
_SEMANTIC_CSV = _SEED_DIR / "semantic_model.csv"

sys.path.insert(0, str(_PROJECT_ROOT / "app"))
from _csv_safeguard import (  # noqa: E402
    assert_csv_safe_row_count,
    assert_fieldnames_cover_rows,
)

if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

_API_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "claude-sonnet-4-6"
_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Column order locked to dbt/seeds/semantic_model.csv header and schema.yml
# column_types block. Any change here MUST be mirrored in both files.
SEMANTIC_COLUMNS: list[str] = [
    "table_name",
    "source_schema",
    "canonical_alias",
    "entity_class",
    "primary_key_cols",
    "natural_key_cols",
    "typical_join_keys_json",
    "code_column_refs_json",
    "typical_filters",
    "common_traps",
    "typical_use_cases",
    "reference_sql",
    "row_count_estimate",
    "populated_by",
    "populated_at_utc",
    "review_state",
    "source_dar_ids",
    # v3.9 §25.5 — Phase 2 EDA enrichment JSON fields. Populated by
    # LLM synthesis from the 4 new DAR types (temporal_coverage,
    # performance_baseline, grain_relationship, segmentation_threshold).
    # Defaults '{}' (or '[]' for grain_relationships_json) when the
    # corresponding DAR type has no rows for this table.
    "temporal_coverage_json",
    "typical_values_range_json",
    "grain_relationships_json",
    "natural_thresholds_json",
]

# Four original EDA analyses must all be present for a table to be EDA-complete.
_REQUIRED_DARS = ("completeness", "dimensions", "magnitude", "code_tables")

# v3.9 §25.3 Phase 2 — four additional DAR types co-compiled into Layer A.
# Presence of these is NOT required (analyzers may legitimately produce
# zero DARs for a given table, e.g. no date columns → no temporal_coverage).
# compile_semantic_model.py queries them opportunistically and passes
# findings to the LLM synthesis prompt.
_OPTIONAL_PHASE2_DARS = (
    "temporal_coverage",
    "performance_baseline",
    "grain_relationship",
    "segmentation_threshold",
)


# ─── Utility ──────────────────────────────────────────────────────────


def _now_utc_naive() -> dt.datetime:
    """RULE 36 / anti-pattern #54 — tz-naive UTC for DuckDB TIMESTAMP."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _iso(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
    return str(value)


# ─── DAR checks ────────────────────────────────────────────────────────
# The has_ontology_coverage function was removed as part of known_issue
# #79 (2026-04-24). §22.2 consumer-priority discipline treated ontology
# coverage as sufficient reason to skip Layer A compilation, but the two
# layers are non-overlapping: Layer A is LLM-synthesized table-level
# narrative; ontology is auto-generated column-level structural lineage.
# Piece 8 consumes both independently (see _context_assembler.LAYER_LOADERS
# — static + ontology are distinct layers). Skip gate removed; remaining
# skip conditions (preserved, DAR-incomplete) are unrelated domain guards.


def check_dar_completeness(conn, raw_table: str) -> dict:
    """Return dict mapping analysis_type → present_bool + the DAR ID
    for the most-recent DAR of that type, if present. §22.5 step 3
    requires all four present; caller skips with warning otherwise.
    """
    result: dict = {}
    for analysis in _REQUIRED_DARS:
        row = conn.execute(
            """
            SELECT id FROM main_seeds.domain_analysis_results
            WHERE analysis_type = ?
              AND LOWER(COALESCE(source_tables, '')) LIKE ?
            ORDER BY executed_at_utc DESC LIMIT 1
            """,
            [analysis, f"%{raw_table.lower()}%"],
        ).fetchone()
        result[analysis] = {"present": row is not None, "dar_id": row[0] if row else None}
    return result


def gather_dars(conn, raw_table: str) -> dict:
    """Fetch the latest DAR row per analysis type for this table.
    Returns dict keyed by analysis_type with the raw result_json string
    and the DAR id. Caller wraps into the compile prompt.

    v3.9 §25.3 extension: fetches latest DAR for each required type
    (completeness/dimensions/magnitude/code_tables) AND opportunistically
    for each Phase 2 type (temporal_coverage/performance_baseline/
    grain_relationship/segmentation_threshold). Unlike required types,
    Phase 2 types may have MULTIPLE rows per table (one per column);
    collect all of them as a list for the LLM to synthesize from.
    """
    dars: dict = {}
    for analysis in _REQUIRED_DARS:
        row = conn.execute(
            """
            SELECT id, result_json FROM main_seeds.domain_analysis_results
            WHERE analysis_type = ?
              AND LOWER(COALESCE(source_tables, '')) LIKE ?
            ORDER BY executed_at_utc DESC LIMIT 1
            """,
            [analysis, f"%{raw_table.lower()}%"],
        ).fetchone()
        if row:
            dars[analysis] = {"id": row[0], "result_json": row[1] or "null"}
        else:
            dars[analysis] = None

    # Phase 2 types — collect ALL rows (often one per column or per pair).
    for analysis in _OPTIONAL_PHASE2_DARS:
        rows = conn.execute(
            """
            SELECT id, result_json FROM main_seeds.domain_analysis_results
            WHERE analysis_type = ?
              AND LOWER(COALESCE(source_tables, '')) LIKE ?
            ORDER BY executed_at_utc DESC
            """,
            [analysis, f"%{raw_table.lower()}%"],
        ).fetchall()
        dars[analysis] = [
            {"id": r[0], "result_json": r[1] or "null"}
            for r in rows
        ]
    return dars


# ─── Scope resolution ─────────────────────────────────────────────────


def _split_csv(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in str(value).split(",") if v.strip()]


def resolve_scope_tables(conn, term_ids: Optional[list[str]]) -> set[str]:
    """Union of raw-table scopes for the requested terms (or all
    approved terms if term_ids is None). Reads s2t_mapping.source_table
    + business_glossary.notes token heuristic.
    """
    if term_ids:
        placeholders = ",".join(["?"] * len(term_ids))
        where = f"bg.id IN ({placeholders})"
        params = term_ids
    else:
        where = "bg.status = 'approved'"
        params = []

    rows = conn.execute(
        f"""
        SELECT DISTINCT LOWER(s.source_table) AS tbl
        FROM main_seeds.s2t_mapping s
        JOIN main_seeds.business_glossary bg ON bg.id = s.business_term_id
        WHERE {where}
          AND s.source_table IS NOT NULL
          AND s.source_table != ''
        """,
        params,
    ).fetchall()
    tables: set[str] = set()
    for r in rows:
        for piece in _split_csv(r[0]):
            # strip schema prefix if present (e.g. raw_sap.ekpo → ekpo)
            tables.add(piece.split(".")[-1].lower())
    return tables


# ─── Existing-row preservation ────────────────────────────────────────


def load_existing_rows(csv_path: Path) -> dict[str, dict]:
    """Returns {table_name: row_dict}. Missing file → empty dict."""
    if not csv_path.exists():
        return {}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return {row["table_name"].lower(): row for row in reader if row.get("table_name")}


def is_human_protected(row: dict) -> bool:
    """§22.5 step 4: preserve human-authored / human-reviewed rows."""
    return (
        row.get("populated_by") == "human_override"
        or row.get("review_state") == "human_reviewed"
    )


# ─── Atomic CSV write ─────────────────────────────────────────────────


def write_csv(csv_path: Path, rows: list[dict]) -> None:
    """Atomic LF-terminated write through csv-safeguard boundary.

    Anti-pattern #57: fieldnames validated before file.open('w') so a
    bad row can't truncate the file mid-write.
    Anti-pattern #48/#50: explicit newline='' + '\\n' terminator.
    Anti-pattern #56: assert_csv_safe_row_count + fieldnames check.
    """
    assert_fieldnames_cover_rows(SEMANTIC_COLUMNS, rows)
    assert_csv_safe_row_count(csv_path, len(rows))

    tmp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=SEMANTIC_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            serialized = {col: _iso(row.get(col, "")) for col in SEMANTIC_COLUMNS}
            writer.writerow(serialized)
    os.replace(tmp_path, csv_path)


# ─── LLM synthesis ────────────────────────────────────────────────────


def _load_prompt_template() -> tuple[str, str]:
    raw = _PROMPT_PATH.read_text(encoding="utf-8")
    sys_marker = "## SYSTEM PROMPT"
    emit_marker = "## EMIT YOUR JSON RESPONSE BELOW"
    sys_start = raw.index(sys_marker) + len(sys_marker)
    sys_end = raw.index("## INPUTS")
    return raw[sys_start:sys_end].strip(), raw[sys_end:raw.index(emit_marker)].strip()


def synthesize_row(
    conn,
    raw_table: str,
    source_schema: str,
    dars: dict,
    bundle_text: str,
    dry_run: bool = False,
) -> Optional[dict]:
    """Call Claude to produce a Layer A row for one table.
    Returns None if dry_run or if the call fails.

    v3.9 §25.4 + §25.11 mock support: when env var
    PIECE8_COMPILE_MOCK_RESPONSE is set to a path of a JSON file, the
    file's contents are used as the LLM response (skipping the API call).
    Used by regression scenarios 17-20 to exercise the JSON round-trip
    without LLM cost.
    """
    if dry_run:
        print(f"  [dry-run] would synthesize {raw_table}")
        return None

    # v3.9 §25.11 — mock mode for regression scenarios.
    mock_path = os.environ.get("PIECE8_COMPILE_MOCK_RESPONSE")
    if mock_path:
        try:
            with open(mock_path, encoding="utf-8") as f:
                emitted = json.load(f)
            print(f"  [mock] using {mock_path} for {raw_table}")
            # Stage F Decision 1: merge schema_discovery FKs before shaping row.
            emitted = _merge_schema_discovery_fks(emitted, raw_table, conn)
            return _row_from_emitted(emitted, raw_table, source_schema)
        except Exception as e:
            print(f"  ERROR: mock response load failed: {e}")
            return None

    if not _API_KEY or _API_KEY == "your-api-key-here":
        print(f"  ERROR: ANTHROPIC_API_KEY not set — cannot synthesize {raw_table}")
        return None

    system_prompt, inputs_template = _load_prompt_template()

    def _phase2_bundle(key: str) -> str:
        items = dars.get(key) or []
        if not items:
            return "[]"
        # Aggregate the list of findings into a single JSON array
        wrapped = [{"dar_id": it["id"], "finding": it["result_json"]} for it in items]
        return json.dumps(wrapped, default=str)

    def _legacy_bundle(key: str) -> str:
        # 8.4.8 Part 3 complement: legacy DAR injection previously passed
        # only result_json, hiding the DAR id from the LLM. Citation
        # discipline in the prompt requires citing every DAR id seen;
        # expose the id alongside the finding so the LLM CAN cite it.
        d = dars.get(key)
        if not d:
            return "null"
        return json.dumps(
            {"dar_id": d["id"], "finding": d["result_json"]},
            default=str,
        )

    user_prompt = (
        inputs_template
        .replace("{scope_table}", raw_table)
        .replace("{source_schema}", source_schema)
        .replace("{dar_completeness_json}", _legacy_bundle("completeness"))
        .replace("{dar_dimensions_json}", _legacy_bundle("dimensions"))
        .replace("{dar_magnitude_json}", _legacy_bundle("magnitude"))
        .replace("{dar_code_tables_json}", _legacy_bundle("code_tables"))
        # v3.9 §25.4 — Phase 2 DAR injection as JSON arrays
        .replace("{dar_temporal_coverage_json}", _phase2_bundle("temporal_coverage"))
        .replace("{dar_performance_baseline_json}", _phase2_bundle("performance_baseline"))
        .replace("{dar_grain_relationship_json}", _phase2_bundle("grain_relationship"))
        .replace("{dar_segmentation_threshold_json}", _phase2_bundle("segmentation_threshold"))
        .replace("{context_bundle}", bundle_text or "(no bundle)")
    )

    try:
        resp = requests.post(
            _API_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": _API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": _MODEL,
                "max_tokens": 2000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"  ERROR: API call failed for {raw_table}: {e}")
        return None

    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        emitted = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  ERROR: JSON parse failed for {raw_table}: {e}")
        print(f"    raw text (first 500 chars): {text[:500]}")
        return None

    # Stage F Decision 1: merge schema_discovery FKs before shaping row.
    emitted = _merge_schema_discovery_fks(emitted, raw_table, conn)
    return _row_from_emitted(emitted, raw_table, source_schema)


def _merge_schema_discovery_fks(
    emitted: dict,
    raw_table: str,
    conn,
    integrity_threshold: float = 0.95,
) -> dict:
    """Stage F Decision 1 — override LLM-authored typical_join_keys_json
    with schema_discovery's empirical FK candidates at high confidence.

    Transforms flat-shape LLM emission `{to_table: [cols]}` into nested-
    shape `{to_table: {columns, source, integrity_pct}}` so provenance
    travels with every entry. LLM entries for tables schema_discovery
    didn't cover stay intact (tagged `source='llm_authored'`, no integrity).

    Stage A's `_render_join_keys` handles both the legacy flat shape and
    the new nested shape for backward compatibility with rows compiled
    pre-Stage-F.

    If no schema_discovery DAR exists for `raw_table` (or it's unparseable),
    returns `emitted` unchanged — zero impact on the existing flow.
    """
    try:
        row = conn.execute(
            """
            SELECT result_json FROM main_seeds.domain_analysis_results
            WHERE LOWER(source_tables) = LOWER(?)
              AND analysis_type = 'schema_discovery'
              AND status = 'success'
            ORDER BY executed_at_utc DESC LIMIT 1
            """,
            [raw_table],
        ).fetchone()
    except Exception:  # noqa: BLE001
        return emitted
    if row is None:
        return emitted
    try:
        sd_result = json.loads(row[0]) if row[0] else {}
    except (json.JSONDecodeError, TypeError):
        return emitted

    fk_candidates = sd_result.get("fk_candidates") or []
    high_conf = [
        fk for fk in fk_candidates
        if (fk.get("referential_integrity_pct") or 0) >= integrity_threshold * 100
    ]
    # Short-circuit if schema_discovery has nothing at high confidence —
    # leave LLM output intact (still flat shape, still consumable).
    if not high_conf:
        return emitted

    # Parse existing LLM-authored keys; migrate legacy flat shape.
    llm_keys: dict[str, dict] = {}
    raw_llm = emitted.get("typical_join_keys_json", "{}")
    try:
        parsed = json.loads(raw_llm) if isinstance(raw_llm, str) else raw_llm
        if isinstance(parsed, dict):
            for tgt, val in parsed.items():
                if isinstance(val, list):
                    llm_keys[str(tgt).lower()] = {
                        "columns": list(val),
                        "source": "llm_authored",
                        "integrity_pct": None,
                    }
                elif isinstance(val, dict):
                    llm_keys[str(tgt).lower()] = val
    except (json.JSONDecodeError, TypeError):
        llm_keys = {}

    # Override / insert schema_discovery high-confidence FKs.
    for fk in high_conf:
        tgt = str(fk.get("to_table", "")).lower()
        if not tgt:
            continue
        llm_keys[tgt] = {
            "columns": list(fk.get("from_columns") or []),
            "source": "schema_discovery",
            "integrity_pct": fk.get("referential_integrity_pct"),
        }

    emitted["typical_join_keys_json"] = json.dumps(llm_keys)
    return emitted


def _row_from_emitted(
    emitted: dict,
    raw_table: str,
    source_schema: str,
    populated_by: str = "eda_compile",
) -> dict:
    """Shape an LLM (or mock) emitted dict into a canonical Layer A row.
    Applies runtime-populated fields (populated_by/at/review_state).
    v3.9 §25.5: also handles 4 new Phase 2 JSON fields with sensible
    defaults ({} / []) when the LLM omits them.
    """
    now = _now_utc_naive()
    return {
        "table_name": str(emitted.get("table_name", raw_table)).lower(),
        "source_schema": str(emitted.get("source_schema", source_schema)),
        "canonical_alias": str(emitted.get("canonical_alias", "")),
        "entity_class": str(emitted.get("entity_class", "")),
        "primary_key_cols": str(emitted.get("primary_key_cols", "")),
        "natural_key_cols": str(emitted.get("natural_key_cols", "")),
        "typical_join_keys_json": _ensure_json_str(emitted.get("typical_join_keys_json")),
        "code_column_refs_json": _ensure_json_str(emitted.get("code_column_refs_json")),
        "typical_filters": str(emitted.get("typical_filters", "")),
        "common_traps": str(emitted.get("common_traps", "")),
        "typical_use_cases": str(emitted.get("typical_use_cases", "")),
        "reference_sql": str(emitted.get("reference_sql", "")),
        "row_count_estimate": int(emitted.get("row_count_estimate") or 0),
        "populated_by": populated_by,
        "populated_at_utc": now,
        "review_state": "auto_generated",
        "source_dar_ids": str(emitted.get("source_dar_ids", "")),
        # v3.9 §25.5 Phase 2 fields. Defaults '{}' for dict-shaped,
        # '[]' for grain_relationships_json list shape.
        "temporal_coverage_json": _ensure_json_str(
            emitted.get("temporal_coverage_json"), default="{}"),
        "typical_values_range_json": _ensure_json_str(
            emitted.get("typical_values_range_json"), default="{}"),
        "grain_relationships_json": _ensure_json_str(
            emitted.get("grain_relationships_json"), default="[]"),
        "natural_thresholds_json": _ensure_json_str(
            emitted.get("natural_thresholds_json"), default="{}"),
    }


def _ensure_json_str(value, default: str = "{}") -> str:
    """Accepts dict/list from LLM parse or string; returns canonical JSON string.
    v3.9 §25.5: `default` parameter lets callers specify '[]' for list-shaped
    fields (grain_relationships_json) vs '{}' for dict-shaped fields."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return json.dumps(parsed, sort_keys=True)
        except json.JSONDecodeError:
            return default
    return json.dumps(value, sort_keys=True)


# ─── Orchestration ────────────────────────────────────────────────────


def _assemble_bundle(
    term_ids: Optional[list[str]], scope_tables: list[str],
    conn=None,
) -> str:
    """Build a narrow static+business bundle for the compile call.
    Keeps the call cheap (no dynamic/DAR layer — DARs are passed
    explicitly in the prompt to prevent double-citation ambiguity).

    #80 fix: accepts optional conn. When passed, threads it into
    assemble_context so no second read-only connection is opened in
    the same process. Parent compile holds a read-write conn; DuckDB
    forbids two conns with different configs per process. Without
    threading, assemble_context crashed with ConnectionError and this
    function silently returned "" — LLM synthesis proceeded without
    DAR-grounded context.
    """
    try:
        sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
        from _context_assembler import assemble_context, ContextScopeError  # noqa: E402
        first_term = term_ids[0] if term_ids else None
        bundle = assemble_context(
            purpose="eda_classification",
            term_id=first_term,
            scope_tables=scope_tables,
            max_tokens=8000,
            strict=False,
            conn=conn,
        )
        return bundle.formatted_prompt
    except Exception as e:
        print(f"  WARNING: bundle assembly failed, proceeding with empty bundle: {e}")
        return ""


def compile_all(
    conn,
    term_ids: Optional[list[str]] = None,
    explicit_tables: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict:
    """Main compile loop. Returns summary dict."""
    if explicit_tables:
        candidate_tables = {t.lower() for t in explicit_tables}
    else:
        candidate_tables = resolve_scope_tables(conn, term_ids)

    if not candidate_tables:
        print("No candidate tables — empty scope. Nothing to compile.")
        return {"status": "no_tables", "compiled": 0, "skipped": 0}

    print(f"Scope tables ({len(candidate_tables)}): {sorted(candidate_tables)}")

    existing = load_existing_rows(_SEMANTIC_CSV)
    source_schema = "raw_sap"

    # Preserved: rows that must survive regardless of recompile
    preserved = {k: v for k, v in existing.items() if is_human_protected(v)}
    # Replaceable: auto-generated rows we may overwrite
    replaceable = {k: v for k, v in existing.items() if not is_human_protected(v)}

    compiled_rows: dict[str, dict] = {}
    stats = {
        "compiled": 0, "skipped_dar_incomplete": 0,
        "skipped_preserved": 0, "failed_synthesis": 0,
    }

    for table in sorted(candidate_tables):
        if table in preserved:
            print(f"  skip {table} — human_override / human_reviewed; preserved")
            stats["skipped_preserved"] += 1
            continue

        # #79 fix: ontology coverage is NOT a skip condition. Layer A and
        # the ontology layer provide independent context in Piece 8's
        # bundle (LLM narrative vs structural lineage).

        completeness = check_dar_completeness(conn, table)
        missing = [a for a, v in completeness.items() if not v["present"]]
        if missing:
            print(f"  skip {table} — EDA incomplete; missing: {missing}")
            stats["skipped_dar_incomplete"] += 1
            continue

        dars = gather_dars(conn, table)
        bundle_text = _assemble_bundle(term_ids, [table], conn=conn)
        row = synthesize_row(
            conn, table, source_schema, dars, bundle_text, dry_run=dry_run
        )
        if row is None:
            stats["failed_synthesis"] += 1
            continue
        compiled_rows[table] = row
        stats["compiled"] += 1
        print(f"  compiled {table} — alias='{row['canonical_alias']}', "
              f"class={row['entity_class']}, DARs={row['source_dar_ids']}")

    if dry_run:
        print(f"\n[dry-run] would compile {stats['compiled']} rows; summary: {stats}")
        return {"status": "dry_run", **stats}

    # Merge: preserved + untouched replaceable + new compiled
    final: dict[str, dict] = {}
    final.update(preserved)
    for k, v in replaceable.items():
        if k not in compiled_rows:
            final[k] = v  # untouched — not in this compile's scope
    final.update(compiled_rows)

    sorted_rows = [final[k] for k in sorted(final.keys())]

    try:
        write_csv(_SEMANTIC_CSV, sorted_rows)
        print(f"\nWrote {len(sorted_rows)} rows to {_SEMANTIC_CSV}")
    except RuntimeError as e:
        print(f"ERROR: write refused by safeguard: {e}")
        return {"status": "safeguard_block", **stats}

    # v3.9 fix (parallel to compile_dbt_semantic_model.py 8.4.4 bugfix) —
    # signal caller to re-seed AFTER closing the DuckDB conn. Running the
    # subprocess while the parent conn is open collides with DuckDB's
    # single-writer lock (anti-pattern #31). main() handles the actual
    # subprocess.run call post-close.
    return {"status": "ok", "reseed_after_close": True, **stats}


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compile Layer A semantic_model.csv from DARs.")
    p.add_argument("--term-ids", type=str, default=None,
                   help="Comma-separated business term IDs (e.g. BG001,BG027). "
                        "Default: all approved terms.")
    p.add_argument("--tables", type=str, default=None,
                   help="Comma-separated raw table names. Bypasses term-scope "
                        "resolution; useful for targeted recompile.")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview compile plan without LLM calls or CSV writes.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None, conn=None) -> int:
    args = _parse_args(argv)
    term_ids = _split_csv(args.term_ids) if args.term_ids else None
    explicit_tables = _split_csv(args.tables) if args.tables else None

    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB_PATH))  # read-write; writer needs it
    try:
        result = compile_all(
            conn,
            term_ids=term_ids,
            explicit_tables=explicit_tables,
            dry_run=args.dry_run,
        )
    finally:
        if owned:
            conn.close()

    # KI-107 fix: replace subprocess `dbt seed` + bulk parquet sync with
    # in-process `sync_parquet_and_invalidate(seed_name=...)`. The helper
    # does CREATE OR REPLACE TABLE from CSV (equivalent to dbt seed) and
    # parquet export atomically — no subprocess, no fragile lock contention,
    # no silent failure. Same fix-class as KI-103/KI-105/KI-106. Original
    # comment about closing the parent conn first still applies because
    # the helper opens its own writer conn when conn=None (default).
    if owned and result.get("reseed_after_close"):
        print("\nRe-seeding semantic_model into DuckDB (in-process)...")
        from _parquet_sync import sync_parquet_and_invalidate  # noqa: E402
        sync_warning = sync_parquet_and_invalidate(
            project_root=_PROJECT_ROOT,
            seed_name="semantic_model",
            source="compile_semantic_model",
        )
        if sync_warning:
            print(
                f"  WARN: semantic_model sync incomplete: {sync_warning}",
                file=sys.stderr,
            )
        else:
            print("  in-process re-seed + parquet sync OK")

    status = result.get("status")
    if status == "no_tables":
        return 3
    if status == "safeguard_block":
        return 2
    if result.get("failed_synthesis", 0) > 0 and result.get("compiled", 0) == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
