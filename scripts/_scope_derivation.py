"""Piece 9 Stage A — scope derivation backend.

LLM-driven table-scope proposal for draft business terms. Analyst
reviews via the Term Scope tab in Streamlit, possibly re-prompts
with revisions, then confirms. On confirm, s2t_mapping is rewritten
to match the proposed scope and business_glossary.status
transitions draft -> scope_confirmed with the full iteration trail
persisted to scope_derivation_history_json.

Design doc: context/phase_15b_piece_8_pre_s2t_reasoning_layer.md
§28 (v3.11 amendment).

Public surface:
  propose_scope(term_id, conn=None)
  revise_scope(term_id, analyst_instruction, conn=None)
  confirm_scope(term_id, iter_num, confirmed_by, conn=None,
                export_parquet=True)
  load_scope_history(term_id, conn=None)
  check_prerequisites(term_id, conn=None)

Persistence pattern (post-confirmation — critical for Streamlit visibility):
  1. Backup business_glossary.csv + s2t_mapping.csv to .bak
  2. Write CSVs
  3. dbt seed --full-refresh --select business_glossary s2t_mapping
  4. scripts/export_parquet.py (refreshes data/parquet/*)
  5. Invalidate Streamlit view catalog via app.db.close_connection()
  6. On any failure in 3-5: restore from .bak + return error

Steps 4-5 are REQUIRED for the Streamlit dashboard to see fresh data
(per app/db.py architecture: Streamlit reads parquet-backed views,
never opens cpe_analytics.duckdb). Scenario 22 (regression harness)
skips steps 4-5 via export_parquet=False because it restores state
afterward and the backup/restore parquet churn would be wasteful.

Anti-pattern #31: owned-when-None conn pattern throughout.
RULE 22: confirmation is analyst-triggered via UI, not auto-called.
RULE 34: CSV writes are LF-only with backup/restore on dbt-seed failure.
RULE 36: timestamps use _now_utc_naive().
known_issue #53: parquet/DuckDB divergence; other seed-writer audit
deferred to follow-up session.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import duckdb
import requests

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "cpe_analytics.duckdb"
_ENV = _ROOT / ".env"
_PROMPT_PATH = _ROOT / "scripts" / "prompts" / "scope_derivation_prompt.md"
_BG_CSV = _ROOT / "dbt" / "seeds" / "business_glossary.csv"
_S2T_CSV = _ROOT / "dbt" / "seeds" / "s2t_mapping.csv"

_API_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 16000  # 6000 truncated rich/wide-scope terms into invalid JSON (KI #127)


# ─── data classes ──────────────────────────────────────────────────────

@dataclass
class ScopeProposal:
    """One iteration of scope derivation."""
    iter_num: int
    mode: str  # "propose" or "revise"
    timestamp: str  # tz-naive UTC isoformat
    proposed_tables: list[str]
    primary_field_per_table: dict[str, str]
    rationale_per_table: dict[str, str]
    join_path: list[dict]
    blockers: list[dict]
    attestation_echo: dict
    confidence: str
    confidence_rationale: str
    diff_from_prior: Optional[dict] = None
    reasoning_for_diff: Optional[str] = None
    analyst_instruction: Optional[str] = None
    analyst_action: Optional[str] = None  # confirmed/rejected/superseded/None
    validation_issues: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


@dataclass
class ConfirmationResult:
    term_id: str
    confirmed_iter_num: int
    confirmed_at_utc: str
    confirmed_by: str
    s2t_rows_written: int
    success: bool
    error: Optional[str] = None


@dataclass
class PrerequisitesStatus:
    term_id: str
    current_status: str
    scope_tables: list[str]
    domain_eda_status: dict[str, bool]  # table -> has DARs?
    domain_eda_needed_on: list[str]
    term_eda_status: str  # "not_applicable_yet" for Stage A
    s2t_readiness: str  # "blocked" / "ready" / "done"
    next_steps: list[str]


# ─── env + prompt loading ──────────────────────────────────────────────

def _load_env() -> None:
    if not _ENV.exists():
        return
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _now_utc_naive() -> dt.datetime:
    """RULE 36 / anti-pattern #54."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _load_prompt() -> tuple[str, str]:
    raw = _PROMPT_PATH.read_text(encoding="utf-8")
    sys_marker = "## SYSTEM PROMPT"
    user_marker = "## USER PROMPT TEMPLATE"
    s_start = raw.index(sys_marker) + len(sys_marker)
    s_end = raw.index(user_marker)
    return raw[s_start:s_end].strip(), raw[s_end:].replace(user_marker, "").strip()


# ─── catalog rendering ─────────────────────────────────────────────────

def _render_sap_catalog(conn, exclude_tables: Optional[list[str]] = None) -> str:
    """Compact rendering of sap_data_dictionary + source_column_roles
    merged. ~80 chars per field line; drops description_hr and
    business_meaning (kept short enough to fit in the bundle).

    Stage F: `exclude_tables` suppresses rows for tables that have a
    populated `semantic_model` row — the Semantic Model block renders
    richer per-table evidence for those tables, so dictionary rows
    would be redundant. Caller passes the compiled-table list.
    """
    excluded = {t.lower() for t in (exclude_tables or [])}
    rows = conn.execute("""
        SELECT d.table_name, d.field_name, d.data_type, d.length,
               COALESCE(r.role, '?') AS role,
               COALESCE(d.domain_area, '?') AS domain,
               COALESCE(d.description_en, '(no description)') AS descr
        FROM main_seeds.sap_data_dictionary d
        LEFT JOIN main_seeds.source_column_roles r
          ON LOWER(d.table_name) = LOWER(r.table_name)
         AND UPPER(d.field_name) = UPPER(r.column_name)
        ORDER BY d.table_name, d.field_name
    """).fetchall()
    lines = []
    for tn, fn, dt_, length, role, domain, descr in rows:
        if tn.lower() in excluded:
            continue
        # length is VARCHAR — may be "10" or SAP DEC notation "13.2". Keep as-is.
        length_part = f"/{length}" if length else ""
        lines.append(
            f"{tn}.{fn} [{dt_ or '?'}{length_part}] (role={role}, domain={domain}) {descr}"
        )
    return "\n".join(lines)


# ─── Stage F renderers: semantic_model + schema_discovery ─────────────

def _render_join_keys(join_keys_json: str) -> str:
    """Render typical_join_keys_json as readable bullet lines.

    Handles two shapes:
      - Legacy flat: {target_table: [col_names]}  → authored tag
      - Stage F nested: {target_table: {columns, source, integrity_pct}}
        → source tag + integrity when present

    This helper is Layer-A-specific; Layer B (dbt_semantic_model) uses
    the same flat shape but is always deterministic-extracted — don't
    reuse this helper for Layer B without adjusting the legacy tag.
    """
    try:
        keys = json.loads(join_keys_json) if join_keys_json else {}
    except (json.JSONDecodeError, TypeError):
        return "      (malformed join-keys JSON)"
    if not keys:
        return "      (none)"
    lines = []
    for target, val in keys.items():
        if isinstance(val, list):
            lines.append(
                f"      → {target} via [{', '.join(str(c) for c in val)}] "
                f"(authored)"
            )
        elif isinstance(val, dict):
            cols = val.get("columns", [])
            src = val.get("source", "unknown")
            integrity = val.get("integrity_pct")
            src_tag = "empirical" if src == "schema_discovery" else "authored"
            integrity_str = (
                f", integrity {integrity:.0f}%" if isinstance(integrity, (int, float))
                else ""
            )
            lines.append(
                f"      → {target} via [{', '.join(str(c) for c in cols)}] "
                f"({src_tag}{integrity_str})"
            )
    return "\n".join(lines)


def _compiled_semantic_model_tables(conn) -> list[str]:
    """List of table_names with a populated semantic_model row."""
    try:
        rows = conn.execute(
            "SELECT LOWER(table_name) FROM main_seeds.semantic_model "
            "WHERE populated_by IS NOT NULL AND populated_by != '' "
            "ORDER BY table_name"
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _render_semantic_model_block(conn) -> str:
    """Render compiled Layer A semantic_model rows + flag not-yet-compiled
    tables. Per Stage F EDIT S4: no domain-area filter — render all.
    """
    try:
        rows = conn.execute("""
            SELECT table_name, canonical_alias, entity_class,
                   primary_key_cols, natural_key_cols,
                   typical_join_keys_json, typical_filters, common_traps,
                   typical_use_cases, row_count_estimate, review_state
            FROM main_seeds.semantic_model
            WHERE populated_by IS NOT NULL AND populated_by != ''
            ORDER BY table_name
        """).fetchall()
    except Exception:  # noqa: BLE001
        rows = []

    compiled_names = {r[0].lower() for r in rows}
    all_live_tables = _raw_sap_tables(conn)
    not_yet = [t for t in all_live_tables if t.lower() not in compiled_names]

    if not rows:
        uncompiled_note = (
            "  ⚠ No tables have compiled semantic_model yet. Run Source "
            "Diagnostic + Compile Semantic Model on Data Catalog page."
        )
        if not_yet:
            return uncompiled_note + "\n  Not yet compiled: " + ", ".join(
                sorted(not_yet)
            )
        return uncompiled_note

    out_lines: list[str] = []
    for r in rows:
        (tbl, alias, ec, pk, nk, joins_json, filters, traps, uses,
         row_est, review) = r
        out_lines.append(
            f"[TABLE] {tbl} (alias: {alias or '?'}, "
            f"entity_class: {ec or '?'}, review_state: {review or '?'})"
        )
        if pk:
            out_lines.append(f"    Primary key: {pk}")
        if nk:
            out_lines.append(f"    Natural key: {nk}")
        joins_render = _render_join_keys(joins_json or "{}")
        if joins_render and joins_render.strip() != "(none)":
            out_lines.append("    Typical join keys:")
            out_lines.append(joins_render)
        if filters:
            snippet = str(filters)[:240].rstrip() + ("..." if len(str(filters)) > 240 else "")
            out_lines.append(f"    Typical filters: {snippet}")
        if traps:
            snippet = str(traps)[:240].rstrip() + ("..." if len(str(traps)) > 240 else "")
            out_lines.append(f"    Common traps: {snippet}")
        if uses:
            snippet = str(uses)[:240].rstrip() + ("..." if len(str(uses)) > 240 else "")
            out_lines.append(f"    Typical use cases: {snippet}")
        if row_est:
            out_lines.append(f"    Row count estimate: {row_est}")
        out_lines.append("")  # blank line between tables

    if not_yet:
        out_lines.append("⚠ Not yet compiled (run Source Diagnostic): "
                         + ", ".join(sorted(not_yet)))
    return "\n".join(out_lines).rstrip()


def _render_schema_discovery_block(conn) -> str:
    """Render the latest successful schema_discovery DAR per raw_sap table."""
    try:
        rows = conn.execute("""
            SELECT source_tables, result_json, executed_at_utc
            FROM main_seeds.domain_analysis_results
            WHERE analysis_type = 'schema_discovery'
              AND status IN ('success', 'skipped')
            ORDER BY executed_at_utc DESC
        """).fetchall()
    except Exception:  # noqa: BLE001
        rows = []

    # Dedupe: keep only latest per table
    latest_per_table: dict[str, tuple] = {}
    for r in rows:
        tbl = (r[0] or "").lower()
        if tbl and tbl not in latest_per_table:
            latest_per_table[tbl] = r

    all_live_tables = _raw_sap_tables(conn)
    not_yet = [t for t in all_live_tables if t not in latest_per_table]

    if not latest_per_table:
        note = (
            "  ⚠ No schema_discovery DARs yet. Run Source Diagnostic on "
            "Data Catalog page to populate PK/FK/shape/bridge evidence."
        )
        if not_yet:
            return note + "\n  Not yet run: " + ", ".join(sorted(not_yet))
        return note

    out_lines: list[str] = []
    for tbl in sorted(latest_per_table):
        _, result_json, _ts = latest_per_table[tbl]
        try:
            p = json.loads(result_json) if result_json else {}
        except (json.JSONDecodeError, TypeError):
            p = {}
        out_lines.append(f"[RELATIONSHIPS] {tbl}:")
        pks = p.get("pk_candidates") or []
        fks = p.get("fk_candidates") or []
        shapes = p.get("relationship_shapes") or []
        bridges = p.get("bridge_tables") or []

        if pks:
            pk_text = "; ".join(
                f"[{', '.join(pk.get('columns') or [])}] "
                f"(conf {pk.get('confidence', '?')})"
                for pk in pks
            )
            out_lines.append(f"    PKs: {pk_text}")
        if fks:
            out_lines.append("    FKs:")
            for fk in fks:
                fc = ",".join(fk.get("from_columns") or [])
                tt = fk.get("to_table", "?")
                tc = ",".join(fk.get("to_columns") or [])
                integ = fk.get("referential_integrity_pct", 0)
                conf = fk.get("confidence", "?")
                out_lines.append(
                    f"      {fc} → {tt}.{tc} (integrity {integ}%, conf {conf})"
                )
        if shapes:
            out_lines.append("    Shapes:")
            for s in shapes:
                pair = " ↔ ".join(s.get("pair") or [])
                shape = s.get("shape", "?")
                card = s.get("cardinality", "?")
                extra = ""
                if s.get("sum_match_pct") is not None:
                    extra = f" · sum-match {s['sum_match_pct']}% on {s.get('sum_match_column', '?')}"
                out_lines.append(f"      {pair}: {shape} ({card}){extra}")
        if bridges:
            out_lines.append("    Bridges:")
            for b in bridges:
                btw = " → ".join(b.get("between") or [])
                via = b.get("via", "?")
                conf = b.get("confidence", "?")
                out_lines.append(f"      {btw} via {via} (conf {conf})")
        out_lines.append("")

    if not_yet:
        out_lines.append("⚠ Not yet run: " + ", ".join(sorted(not_yet)))
    return "\n".join(out_lines).rstrip()


# _raw_sap_tables() is defined below at the public-API block; the
# semantic_model + schema_discovery renderers reuse it.


def _render_source_column_roles(conn) -> str:
    """Compact role listing — row per (table, column)."""
    rows = conn.execute("""
        SELECT table_name, column_name, role, role_confidence
        FROM main_seeds.source_column_roles
        ORDER BY table_name, column_name
    """).fetchall()
    lines = [f"{t}.{c} -> {r} ({conf})" for t, c, r, conf in rows]
    return "\n".join(lines)


def _render_join_cardinality_block(
    conn,
    candidate_tables: Optional[list[str]] = None,
) -> str:
    """Spec §6.1 — render empirical join cardinality evidence per pair.

    Reads `join_cardinality` DARs (analysis_type filter, current rows
    only — superseded_by IS NULL). For each pair, prioritize rendering:
        1. per_record_key DARs (all)
        2. header_detail DARs (all)
        3. ≤2 representative catastrophic_fanout DARs
        4. ≤1 representative no_signal DAR
    Goal: keep context informative without swamping the LLM with 28
    catastrophic_fanout rows from over-bridged pairs (Amendment 2 v3.1
    leaves 5 pairs above the per-pair target).

    `candidate_tables` filters: if provided, only pairs where both t1
    and t2 are in the set are rendered. Pass None at propose-time to
    render all available cardinality evidence.

    F10-aware via the comma-tolerant `source_tables` storage convention
    (cardinality DARs use sorted-lex `t1,t2`); we parse via list_contains
    semantics so both single and multi-table source_tables match.
    """
    try:
        rows = conn.execute("""
            SELECT id, source_tables, result_json
            FROM main_seeds.domain_analysis_results
            WHERE analysis_type = 'join_cardinality'
              AND status = 'success'
              AND (superseded_by IS NULL OR superseded_by = '')
            ORDER BY source_tables, id
        """).fetchall()
    except duckdb.Error:
        return "(join_cardinality DARs unavailable)"
    if not rows:
        return "(no join_cardinality DARs — analyzer has not been run)"

    cand_set = (
        {t.lower() for t in candidate_tables}
        if candidate_tables is not None else None
    )

    by_pair: dict[tuple[str, str], list[dict]] = {}
    for dar_id, source_tables, result_json in rows:
        try:
            f = json.loads(result_json or "{}")
        except json.JSONDecodeError:
            continue
        t1 = (f.get("t1") or "").lower()
        t2 = (f.get("t2") or "").lower()
        if not t1 or not t2:
            continue
        if cand_set is not None and (t1 not in cand_set or t2 not in cand_set):
            continue
        f["_dar_id"] = dar_id
        by_pair.setdefault((t1, t2), []).append(f)
    if not by_pair:
        return ("(no join_cardinality DARs match candidate scope; "
                "run cardinality analyzer if needed)")

    _CLASS_RANK = {
        "per_record_key": 0,
        "header_detail": 1,
        "catastrophic_fanout": 2,
        "no_signal": 3,
    }
    out_lines: list[str] = []
    for pair in sorted(by_pair.keys()):
        t1, t2 = pair
        cands = by_pair[pair]
        cands.sort(key=lambda c: (_CLASS_RANK.get(c.get("fanout_class"), 9),
                                  -float(c.get("avg_fanout") or 0)))
        prks = [c for c in cands if c.get("fanout_class") == "per_record_key"]
        hds  = [c for c in cands if c.get("fanout_class") == "header_detail"]
        cats = [c for c in cands if c.get("fanout_class") == "catastrophic_fanout"]
        nos  = [c for c in cands if c.get("fanout_class") == "no_signal"]

        # Pick representative catastrophic: prefer 1 direct + 1 bridge.
        cat_pick: list[dict] = []
        cat_direct = next((c for c in cats if c.get("kind") == "direct"), None)
        cat_bridge = next((c for c in cats if c.get("kind") == "bridge"), None)
        if cat_direct:
            cat_pick.append(cat_direct)
        if cat_bridge and cat_bridge is not cat_direct:
            cat_pick.append(cat_bridge)
        if not cat_pick and cats:
            cat_pick.append(cats[0])

        no_pick = nos[:1] if nos else []

        rendered = prks + hds + cat_pick + no_pick
        if not rendered:
            continue

        out_lines.append(f"\n{t1} <-> {t2}:")
        n_total = len(cands)
        if n_total > len(rendered):
            out_lines.append(f"  ({n_total} candidates total, "
                             f"{len(rendered)} most-informative shown):")
        for c in rendered:
            kind = c.get("kind", "?")
            keys_t1 = "+".join(c.get("key_columns_t1") or [])
            keys_t2 = "+".join(c.get("key_columns_t2") or [])
            via = c.get("bridge_via")
            avg = c.get("avg_fanout") or 0
            stddev = c.get("stddev_fanout") or 0
            ratio = c.get("matched_keys_ratio") or 0
            cls = c.get("fanout_class", "?")
            if kind == "bridge":
                bl = "+".join(c.get("bridge_keys_left") or [])
                br = "+".join(c.get("bridge_keys_right") or [])
                desc = (f"bridge via {via} ({keys_t1} -> {bl} | "
                        f"{br} -> {keys_t2})")
            else:
                desc = f"direct via {keys_t1}"
            out_lines.append(
                f"  - {desc}: {cls} "
                f"(avg {avg:.2f}x, stddev {stddev:.2f}, "
                f"matched {ratio:.2f}). [{c.get('_dar_id')}]"
            )

    if not out_lines:
        return "(no informative cardinality evidence in scope)"
    return "## Join cardinality evidence\n" + "\n".join(out_lines).lstrip("\n")


def _render_dar_coverage_block(
    conn,
    candidate_tables: Optional[list[str]] = None,
) -> str:
    """Spec §6.2 — per-table analyzer coverage so the LLM has ground
    truth for `missing_domain_eda` emission (incidentally fixes F11).

    Renders one row per raw_sap table (or the candidate subset) with
    a check/cross per analyzer type: completeness, dimensions, magnitude,
    code_tables, temporal_coverage, segmentation_threshold, schema_discovery,
    grain_relationship.
    """
    analyzers = (
        "completeness", "dimensions", "magnitude", "code_tables",
        "temporal_coverage", "segmentation_threshold", "schema_discovery",
        "grain_relationship",
    )
    try:
        rows = conn.execute("""
            SELECT analysis_type, source_tables
            FROM main_seeds.domain_analysis_results
            WHERE status = 'success'
              AND (superseded_by IS NULL OR superseded_by = '')
              AND analysis_type IN (
                  'completeness','dimensions','magnitude','code_tables',
                  'temporal_coverage','segmentation_threshold',
                  'schema_discovery','grain_relationship')
        """).fetchall()
    except duckdb.Error:
        return "(domain_analysis_results unavailable)"

    coverage: dict[str, set[str]] = {}
    for analysis_type, source_tables in rows:
        for tbl in (source_tables or "").split(","):
            t = tbl.strip().lower()
            if t:
                coverage.setdefault(t, set()).add(analysis_type)

    if candidate_tables is None:
        try:
            tables = sorted({r[0].lower() for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='raw_sap'"
            ).fetchall()})
        except duckdb.Error:
            tables = sorted(coverage.keys())
    else:
        tables = sorted({t.lower() for t in candidate_tables})

    if not tables:
        return "(no candidate tables for DAR coverage block)"

    out = ["## DAR coverage (analyzers run per candidate table)"]
    out.append("")
    out.append("Each cell shows whether at least one current (non-superseded) "
               "DAR exists for that (table, analyzer) combination. Multi-table "
               "DARs (e.g. grain_relationship) count toward each constituent "
               "table.")
    out.append("")
    header = "table".ljust(8) + "  " + "  ".join(
        a[:8].ljust(8) for a in analyzers)
    out.append(header)
    for t in tables:
        cov = coverage.get(t, set())
        cells = []
        for a in analyzers:
            cells.append("yes".ljust(8) if a in cov else "NO ".ljust(8))
        out.append(t.ljust(8) + "  " + "  ".join(cells))
    return "\n".join(out)


def _render_dbt_coverage(conn) -> str:
    """Which raw_sap tables have downstream staging/vault/mart coverage."""
    try:
        rows = conn.execute("""
            SELECT LOWER(origin_table) AS t,
                   COUNT(DISTINCT model_name) AS models
            FROM main_seeds.dbt_column_lineage
            WHERE LOWER(origin_table) IN (
                SELECT LOWER(table_name) FROM information_schema.tables
                WHERE table_schema='raw_sap'
            )
            GROUP BY 1 ORDER BY 1
        """).fetchall()
    except Exception:
        return "(dbt_column_lineage unavailable)"
    if not rows:
        return "(no raw_sap origin rows in dbt_column_lineage)"
    return "\n".join(f"  {t}: {n} downstream models" for t, n in rows)


def _render_exemplars(conn, limit: int = 2) -> str:
    """Show up to N existing terms with their confirmed scope as patterns."""
    rows = conn.execute("""
        SELECT bg.id, bg.term_name, bg.definition,
               STRING_AGG(s.source_table, ', ') AS tables
        FROM main_seeds.business_glossary bg
        JOIN main_seeds.s2t_mapping s ON s.business_term_id = bg.id
        WHERE bg.status IN ('approved', 'scope_confirmed')
        GROUP BY bg.id, bg.term_name, bg.definition
        ORDER BY bg.id LIMIT ?
    """, [limit]).fetchall()
    if not rows:
        return "  (no exemplars available)"
    out = []
    for i, (tid, tn, defn, tbls) in enumerate(rows, 1):
        out.append(
            f"  Example {i}: {tid} ({tn})\n"
            f"    definition: {(defn or '')[:140]}\n"
            f"    scope tables: {tbls}"
        )
    return "\n".join(out)


# ─── term loading ──────────────────────────────────────────────────────

def _load_term(conn, term_id: str) -> dict:
    row = conn.execute("""
        SELECT id, term_name, display_name, definition, unit, grain,
               domain, notes, business_join_description,
               business_filter_description, status,
               COALESCE(scope_derivation_history_json, '{}') AS history
        FROM main_seeds.business_glossary WHERE id = ?
    """, [term_id]).fetchone()
    if not row:
        raise ValueError(f"term {term_id} not found in business_glossary")
    keys = ["id", "term_name", "display_name", "definition", "unit", "grain",
            "domain", "notes", "business_join_description",
            "business_filter_description", "status", "history"]
    return dict(zip(keys, row))


# ─── LLM call with prompt caching ──────────────────────────────────────

def _call_llm_cached(system_prompt: str, user_prompt: str,
                     api_key: str) -> dict:
    """Single cached LLM call. System prompt hits 5m-TTL cache on repeat."""
    r = requests.post(
        _API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": _MODEL,
            "max_tokens": _MAX_TOKENS,
            "system": [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": user_prompt}],
            }],
        },
        timeout=240,
    )
    r.raise_for_status()
    body = r.json()
    text = body["content"][0]["text"].strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) if not lines[-1].startswith("```") else -1
        text = "\n".join(lines[1:end])
    return {"payload": json.loads(text), "usage": body.get("usage", {})}


# ─── validation ────────────────────────────────────────────────────────

def _validate_response(payload: dict, live_tables: set[str],
                       mode: str,
                       conn: Optional[duckdb.DuckDBPyConnection] = None,
                       ) -> list[str]:
    """Returns list of validation issue strings (empty = clean).

    When `conn` is provided, also runs Direction D §6.5 cardinality
    hard-gate: for each pair in `payload['join_path']`, if cardinality
    DARs exist but none classify as viable (per_record_key /
    header_detail) and non-stale, append a `validation_issues` entry
    UNLESS the LLM has emitted a `missing_table` blocker (which downgrades
    to silent warning per the spec's escape-hatch rule).
    """
    issues: list[str] = []

    if not isinstance(payload, dict):
        return [f"response is not a dict: {type(payload).__name__}"]

    required = {"proposed_tables", "primary_field_per_table",
                "rationale_per_table", "join_path", "blockers",
                "attestation_echo", "confidence", "confidence_rationale"}
    missing = required - set(payload.keys())
    if missing:
        issues.append(f"missing required keys: {sorted(missing)}")

    pt = payload.get("proposed_tables") or []
    if not isinstance(pt, list) or not all(isinstance(x, str) for x in pt):
        issues.append("proposed_tables must be list[str]")
    else:
        bad = [t for t in pt if t.lower() not in live_tables]
        if bad:
            issues.append(f"proposed_tables not in raw_sap: {bad}")

    pft = payload.get("primary_field_per_table") or {}
    if not isinstance(pft, dict):
        issues.append("primary_field_per_table must be dict")
    else:
        missing_fields = [t for t in pt if t not in pft]
        if missing_fields:
            issues.append(f"primary_field_per_table missing entries for: {missing_fields}")

    rat = payload.get("rationale_per_table") or {}
    if not isinstance(rat, dict):
        issues.append("rationale_per_table must be dict")

    conf = payload.get("confidence")
    if conf not in ("high", "medium", "low"):
        issues.append(f"confidence not in enum: {conf!r}")

    blockers = payload.get("blockers") or []
    valid_btypes = {"missing_table", "missing_domain_eda",
                    "join_ambiguity", "scope_concern"}
    valid_resolves_in = {"domain_eda", "term_eda",
                         "analyst_decision", "ingestion_required",
                         "source_diagnostic_required"}
    augment_fields = ("short_title", "what_it_means", "what_llm_needs",
                      "resolves_in", "resolves_via", "user_action_now")
    for i, b in enumerate(blockers):
        if not isinstance(b, dict):
            issues.append(f"blocker[{i}] not a dict")
            continue
        if b.get("type") not in valid_btypes:
            issues.append(f"blocker[{i}] invalid type: {b.get('type')!r}")
        # §28.11 cross-stage contract — 6 augmentation fields required on
        # new proposals. Older history JSON (pre-augmentation) is parsed
        # with missing fields and rendered via backward-compat path.
        missing = [f for f in augment_fields if not (b.get(f) or "").strip()]
        if missing:
            issues.append(
                f"blocker[{i}] type={b.get('type')!r} missing augmentation "
                f"fields: {missing}"
            )
        ri = b.get("resolves_in")
        if ri and ri not in valid_resolves_in:
            issues.append(
                f"blocker[{i}] invalid resolves_in: {ri!r} "
                f"(expected one of {sorted(valid_resolves_in)})"
            )

    att = payload.get("attestation_echo") or {}
    if not isinstance(att, dict):
        issues.append("attestation_echo must be dict")

    if mode == "revise":
        if "diff_from_prior" not in payload:
            issues.append("revise mode requires diff_from_prior")
        if "reasoning_for_diff" not in payload:
            issues.append("revise mode requires reasoning_for_diff")

    # Direction D §6.5 — cardinality hard-gate (only when conn provided).
    # Pre-chain placement satisfied by construction: validator runs in
    # _propose_or_revise BEFORE confirm_scope's side-effect chain.
    # Per-iteration re-check (F19) satisfied by virtue of validator
    # firing on every revise call independent of blocker state.
    if conn is not None:
        cardinality_issues = _validate_cardinality_join_path(payload, conn)
        issues.extend(cardinality_issues)

    return issues


def _validate_cardinality_join_path(
    payload: dict,
    conn: duckdb.DuckDBPyConnection,
) -> list[str]:
    """Direction D §6.5 — for each pair in the LLM-declared `join_path`,
    require at least one viable, non-stale `join_cardinality` DAR.

    Pair selection: uses `payload['join_path']` (NOT C(n,2) over
    `proposed_tables`). Per design Round 4 flag B, iterating all proposed
    pairs over-blocks pairs the term's query never joins.

    Staleness: `source_row_counts` for either side has shifted >10% from
    the DAR's recorded count → DAR treated as stale.

    Escape hatch: if the LLM has emitted ANY `missing_table` blocker in
    the same iteration, downgrade hard-gate to silent warning (per spec
    §6.5). Per-iteration re-check satisfies F19 — no blocker carry-forward
    state needed.
    """
    issues: list[str] = []
    join_path = payload.get("join_path") or []
    if not isinstance(join_path, list) or not join_path:
        return issues

    blockers = payload.get("blockers") or []
    has_missing_table_blocker = any(
        isinstance(b, dict) and b.get("type") == "missing_table"
        for b in blockers
    )

    seen_pairs: set[tuple[str, str]] = set()
    for j in join_path:
        if not isinstance(j, dict):
            continue
        from_t = (j.get("from") or "").strip().lower()
        to_t = (j.get("to") or "").strip().lower()
        if not from_t or not to_t or from_t == to_t:
            continue
        pair_key = tuple(sorted([from_t, to_t]))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        # Load join_cardinality DARs for the pair using F10-aware lookup.
        try:
            rows = conn.execute(
                "SELECT result_json FROM main_seeds.domain_analysis_results "
                "WHERE analysis_type='join_cardinality' "
                "  AND status='success' "
                "  AND (superseded_by IS NULL OR superseded_by='') "
                "  AND list_contains("
                "        list_transform(string_split(LOWER(source_tables), ',')"
                "                       , x -> trim(x)), LOWER(?)) "
                "  AND list_contains("
                "        list_transform(string_split(LOWER(source_tables), ',')"
                "                       , x -> trim(x)), LOWER(?))",
                [from_t, to_t],
            ).fetchall()
        except duckdb.Error:
            continue
        if not rows:
            # No cardinality evidence for this pair — don't gate.
            continue

        any_viable_fresh = False
        for (rj,) in rows:
            try:
                f = json.loads(rj or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if f.get("fanout_class") not in (
                "per_record_key", "header_detail"):
                continue
            # Staleness check.
            stale = False
            src_counts = f.get("source_row_counts") or {}
            for tn, n_old in src_counts.items():
                try:
                    n_now = int(conn.execute(
                        f'SELECT COUNT(*) FROM raw_sap."{tn}"'
                    ).fetchone()[0] or 0)
                except duckdb.Error:
                    continue
                if int(n_old or 0) > 0:
                    drift = abs(n_now - int(n_old)) / max(int(n_old), 1)
                    if drift > 0.10:
                        stale = True
                        break
            if not stale:
                any_viable_fresh = True
                break

        if any_viable_fresh:
            continue
        # No viable, non-stale candidate for this pair.
        if has_missing_table_blocker:
            # Escape hatch: LLM proposed resolution → soft warning,
            # not validation_issues. v3.1 keeps this silent (no separate
            # warnings channel ships in this commit).
            continue
        issues.append(
            f"No viable join key between {from_t} and {to_t}. "
            f"Cardinality evidence shows all candidates are "
            f"catastrophic_fanout, no_signal, or stale. Either revise "
            f"scope to add bridge tables (emit a missing_table blocker "
            f"naming the ingested-but-unscoped table that resolves the "
            f"gap), or adjust the term's grain."
        )
    return issues


# ─── public API ────────────────────────────────────────────────────────

def _raw_sap_tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='raw_sap'"
    ).fetchall()
    return {r[0].lower() for r in rows}


def _build_user_prompt(term: dict, template: str, mode: str,
                       catalog: str, roles: str, coverage: str,
                       exemplars: str, analyst_instruction: Optional[str],
                       prior_proposal: Optional[dict],
                       semantic_model: str = "",
                       schema_discovery: str = "",
                       join_cardinality: str = "",
                       dar_coverage: str = "") -> str:
    if mode == "propose":
        mode_block = (
            "### Mode-specific input\n\n"
            "This is the initial proposal. There is no prior history."
        )
    else:
        prior_json = json.dumps(prior_proposal or {},
                                ensure_ascii=False, indent=2, default=str)
        mode_block = (
            "### Mode-specific input\n\n"
            "Prior proposal (the one the analyst is revising):\n\n"
            f"```json\n{prior_json}\n```\n\n"
            f"Analyst revision instruction:\n\n"
            f"{analyst_instruction or '(no instruction)'}"
        )
    return (
        template
        .replace("{mode}", mode)
        .replace("{term_id}", term["id"])
        .replace("{term_name}", term["term_name"] or "")
        .replace("{display_name}", term["display_name"] or "")
        .replace("{definition}", term["definition"] or "")
        .replace("{grain}", term["grain"] or "")
        .replace("{unit}", term["unit"] or "")
        .replace("{domain}", term["domain"] or "")
        .replace("{notes}", term["notes"] or "")
        .replace("{business_join_description}", term["business_join_description"] or "")
        .replace("{business_filter_description}", term["business_filter_description"] or "")
        .replace("{sap_data_dictionary_block}", catalog)
        .replace("{source_column_roles_block}", roles)
        .replace("{semantic_model_block}", semantic_model)
        .replace("{schema_discovery_block}", schema_discovery)
        .replace("{join_cardinality_block}", join_cardinality)
        .replace("{dar_coverage_block}", dar_coverage)
        .replace("{dbt_coverage_block}", coverage)
        .replace("{exemplars_block}", exemplars)
        .replace("{mode_specific_block}", mode_block)
    )


def _propose_or_revise(term_id: str, mode: str,
                        analyst_instruction: Optional[str] = None,
                        conn: Optional[duckdb.DuckDBPyConnection] = None
                        ) -> ScopeProposal:
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB), read_only=True)
    try:
        _load_env()
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        system_prompt, user_template = _load_prompt()
        term = _load_term(conn, term_id)
        live_tables = _raw_sap_tables(conn)

        # Stage F: semantic_model overrides dictionary rows for compiled
        # tables (tiered rendering per EDIT S3). schema_discovery ships
        # a parallel block covering empirical relational structure.
        compiled = _compiled_semantic_model_tables(conn)
        catalog = _render_sap_catalog(conn, exclude_tables=compiled)
        roles = _render_source_column_roles(conn)
        coverage = _render_dbt_coverage(conn)
        exemplars = _render_exemplars(conn, limit=2)
        semantic_model = _render_semantic_model_block(conn)
        schema_discovery = _render_schema_discovery_block(conn)

        history = json.loads(term["history"] or "{}")
        prior_iterations = history.get("iterations", [])
        iter_num = len(prior_iterations) + 1

        prior_proposal = None
        if mode == "revise" and prior_iterations:
            prior_proposal = prior_iterations[-1].get("llm_response")

        # Direction D §6.1 + §6.2: cardinality + DAR coverage blocks.
        # In revise mode, scope to the prior proposal's tables; in
        # propose mode, render all available evidence.
        cand_tables: Optional[list[str]] = None
        if prior_proposal:
            cand_tables = list(prior_proposal.get("proposed_tables") or []) or None
        join_cardinality = _render_join_cardinality_block(conn, cand_tables)
        dar_coverage = _render_dar_coverage_block(conn, cand_tables)

        user_prompt = _build_user_prompt(
            term, user_template, mode, catalog, roles, coverage,
            exemplars, analyst_instruction, prior_proposal,
            semantic_model=semantic_model,
            schema_discovery=schema_discovery,
            join_cardinality=join_cardinality,
            dar_coverage=dar_coverage,
        )

        resp = _call_llm_cached(system_prompt, user_prompt, api_key)
        payload = resp["payload"]
        usage = resp["usage"]

        issues = _validate_response(payload, live_tables, mode, conn=conn)

        now = _now_utc_naive().isoformat(timespec="seconds")
        return ScopeProposal(
            iter_num=iter_num,
            mode=mode,
            timestamp=now,
            proposed_tables=[t.lower() for t in payload.get("proposed_tables", [])],
            primary_field_per_table=payload.get("primary_field_per_table", {}),
            rationale_per_table=payload.get("rationale_per_table", {}),
            join_path=payload.get("join_path", []),
            blockers=payload.get("blockers", []),
            attestation_echo=payload.get("attestation_echo", {}),
            confidence=payload.get("confidence", "low"),
            confidence_rationale=payload.get("confidence_rationale", ""),
            diff_from_prior=payload.get("diff_from_prior"),
            reasoning_for_diff=payload.get("reasoning_for_diff"),
            analyst_instruction=analyst_instruction,
            validation_issues=issues,
            usage=usage,
        )
    finally:
        if owned:
            conn.close()


def propose_scope(term_id: str,
                  conn: Optional[duckdb.DuckDBPyConnection] = None
                  ) -> ScopeProposal:
    """Initial LLM proposal. Iter 1 always."""
    return _propose_or_revise(term_id, "propose", None, conn)


def revise_scope(term_id: str, analyst_instruction: str,
                 conn: Optional[duckdb.DuckDBPyConnection] = None
                 ) -> ScopeProposal:
    """Revision based on analyst instruction string."""
    return _propose_or_revise(term_id, "revise", analyst_instruction, conn)


def load_scope_history(term_id: str,
                       conn: Optional[duckdb.DuckDBPyConnection] = None
                       ) -> dict:
    """Read the scope_derivation_history_json for a term.

    Reads from the CSV (source of truth) rather than the DB so that
    in-flight proposals written by append_iteration_to_history are
    visible to confirm_scope without requiring a dbt seed between
    every revise iteration. conn is accepted for API symmetry but
    unused in the CSV path.
    """
    _ = conn  # API symmetry only
    with _BG_CSV.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("id") == term_id:
                raw = row.get("scope_derivation_history_json") or "{}"
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {}
    return {}


def append_iteration_to_history(term_id: str, proposal: ScopeProposal,
                                 conn: Optional[duckdb.DuckDBPyConnection] = None
                                 ) -> dict:
    """Persist a proposal to scope_derivation_history_json WITHOUT confirming.

    Called between propose/revise cycles so the analyst's in-flight
    work survives refreshes. Does NOT change status.
    """
    history = load_scope_history(term_id, conn=conn)
    iterations = history.get("iterations", [])
    entry = {
        "iter_num": proposal.iter_num,
        "mode": proposal.mode,
        "timestamp": proposal.timestamp,
        "analyst_instruction": proposal.analyst_instruction,
        "llm_response": {
            "proposed_tables": proposal.proposed_tables,
            "primary_field_per_table": proposal.primary_field_per_table,
            "rationale_per_table": proposal.rationale_per_table,
            "join_path": proposal.join_path,
            "blockers": proposal.blockers,
            "attestation_echo": proposal.attestation_echo,
            "confidence": proposal.confidence,
            "confidence_rationale": proposal.confidence_rationale,
            "diff_from_prior": proposal.diff_from_prior,
            "reasoning_for_diff": proposal.reasoning_for_diff,
        },
        "validation_issues": proposal.validation_issues,
        "analyst_action": None,
        "usage": proposal.usage,
    }
    iterations.append(entry)
    history["iterations"] = iterations
    _write_history(term_id, history)
    return history


def _write_history(term_id: str, history: dict) -> None:
    """Overwrite business_glossary.scope_derivation_history_json for one term.

    Uses CSV rewrite (not UPDATE SQL) to keep CSV as source of truth
    per dbt seed pattern. LF-only, atomic replace. No backup needed
    at this stage — only the one cell changes.
    """
    with _BG_CSV.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        headers = r.fieldnames
    for row in rows:
        if row.get("id") == term_id:
            row["scope_derivation_history_json"] = json.dumps(
                history, ensure_ascii=False, default=str,
            )
            break
    tmp = _BG_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, lineterminator="\n",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in headers})
    os.replace(tmp, _BG_CSV)


def _has_deployed_s2t_rows(term_id: str) -> tuple[bool, int]:
    """known_issue #65 guard: returns (has_deployed, count).

    True iff any `s2t_mapping` row for term_id has `target_model`
    populated. Stage A writes rows with target_model empty; Piece 8
    Deploy populates target_model. Inlines the target_model-populated
    check (same semantic as Stage D.2's `has_piece8_s2t_rows` helper).
    Consolidation deferred to Direction C Part E if primitive design
    shares state-check logic.
    """
    if not _S2T_CSV.exists():
        return (False, 0)
    with _S2T_CSV.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    deployed = [
        r for r in rows
        if r.get("business_term_id") == term_id
        and (r.get("target_model") or "").strip() not in ("", "None", "nan")
    ]
    return (bool(deployed), len(deployed))


def confirm_scope(term_id: str, iter_num: int, confirmed_by: str,
                  conn: Optional[duckdb.DuckDBPyConnection] = None,
                  export_parquet: bool = True,
                  ) -> ConfirmationResult:
    """Human-gated confirmation. Writes s2t_mapping rows + updates
    business_glossary (status + history). Atomic via .bak restore.

    RULE 22: called only from UI button, never auto-invoked.

    DuckDB lock note: any passed-in conn is closed before the subprocess
    dbt seed fires (DuckDB permits one writer; subprocess dbt is that
    writer, so this process must release the file).

    Parquet sync (export_parquet=True, default): after dbt seed succeeds,
    run scripts/export_parquet.py so Streamlit's parquet-backed view
    catalog sees the updated schema. Also calls app.db.close_connection()
    to invalidate the cached view catalog. Set to False in test harnesses
    that restore state afterward (known_issue #53).
    """
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB), read_only=True)
    bg_bak = _BG_CSV.with_suffix(".csv.bak")
    s2t_bak = _S2T_CSV.with_suffix(".csv.bak")
    try:
        history = load_scope_history(term_id, conn=conn)
        iterations = history.get("iterations", [])
        target = next((it for it in iterations if it["iter_num"] == iter_num), None)
        if target is None:
            return ConfirmationResult(
                term_id=term_id, confirmed_iter_num=iter_num,
                confirmed_at_utc="", confirmed_by=confirmed_by,
                s2t_rows_written=0, success=False,
                error=f"iter_num {iter_num} not in history",
            )

        resp = target["llm_response"]
        tables = resp.get("proposed_tables", [])
        pft = resp.get("primary_field_per_table", {})
        rat = resp.get("rationale_per_table", {})

        live = _raw_sap_tables(conn)
        bad = [t for t in tables if t.lower() not in live]
        if bad:
            return ConfirmationResult(
                term_id=term_id, confirmed_iter_num=iter_num,
                confirmed_at_utc="", confirmed_by=confirmed_by,
                s2t_rows_written=0, success=False,
                error=f"proposed tables not in raw_sap: {bad}",
            )

        # known_issue #65 guard: refuse to re-derive scope on a term
        # that has Piece 8 Deploy output. Without this guard,
        # _rewrite_s2t_for_term below would silently delete rows
        # carrying target_model + transformation_logic + join/filter
        # descriptions — i.e., destroy Deploy-produced state with no
        # archival.
        has_deployed, n_deployed = _has_deployed_s2t_rows(term_id)
        if has_deployed:
            return ConfirmationResult(
                term_id=term_id, confirmed_iter_num=iter_num,
                confirmed_at_utc="", confirmed_by=confirmed_by,
                s2t_rows_written=0, success=False,
                error=(
                    f"Cannot re-derive scope for term {term_id}: "
                    f"{n_deployed} deployed S2T row(s) with populated "
                    "target_model would be destroyed.\n\n"
                    "Re-deriving scope on an already-deployed term is "
                    "blocked pending Direction C (Re-run S2T flow). To "
                    "change this term's scope today, create a new "
                    "business_glossary term with the desired scope and "
                    "follow the standard pipeline; the existing term's "
                    "production queries remain unaffected.\n\n"
                    "See known_issue #65 for background."
                ),
            )

        # Release DB lock before subprocess dbt (single-writer constraint).
        if owned:
            conn.close()
            conn = None

        # Mark iteration actions and stamp confirmation metadata
        now = _now_utc_naive().isoformat(timespec="seconds")
        for it in iterations:
            if it["iter_num"] == iter_num:
                it["analyst_action"] = "confirmed"
            else:
                it["analyst_action"] = "superseded"
        history["confirmed_at_utc"] = now
        history["confirmed_by"] = confirmed_by
        history["final_iter_num"] = iter_num

        # Backup both CSVs
        shutil.copy2(_BG_CSV, bg_bak)
        shutil.copy2(_S2T_CSV, s2t_bak)

        # Update business_glossary row (history + status transition)
        _update_bg_on_confirm(term_id, history)

        # Rewrite s2t_mapping for this term: delete prior rows + insert proposed
        rows_written = _rewrite_s2t_for_term(term_id, tables, pft, rat)

        # KI-105: in-process per-seed sync replaces the legacy dbt seed
        # + bulk parquet subprocesses. Strict semantics — any warning
        # (seed-write or parquet-export) rolls back both CSVs and
        # returns success=False, eliminating the silent-stale window.
        from _parquet_sync import sync_parquet_and_invalidate  # noqa: E402
        sync_warnings: list[str] = []
        for sn in ("business_glossary", "s2t_mapping"):
            w = sync_parquet_and_invalidate(
                project_root=_ROOT, seed_name=sn,
                skip=not export_parquet,
                source="_scope_derivation.confirm_scope",
            )
            if w:
                sync_warnings.append(w)

        if sync_warnings:
            shutil.copy2(bg_bak, _BG_CSV)
            shutil.copy2(s2t_bak, _S2T_CSV)
            return ConfirmationResult(
                term_id=term_id, confirmed_iter_num=iter_num,
                confirmed_at_utc="", confirmed_by=confirmed_by,
                s2t_rows_written=0, success=False,
                error="; ".join(sync_warnings),
            )

        # Success: clean up backups
        bg_bak.unlink(missing_ok=True)
        s2t_bak.unlink(missing_ok=True)

        return ConfirmationResult(
            term_id=term_id, confirmed_iter_num=iter_num,
            confirmed_at_utc=now, confirmed_by=confirmed_by,
            s2t_rows_written=rows_written, success=True,
        )
    except Exception as e:
        # Attempt rollback if backups exist
        if bg_bak.exists():
            shutil.copy2(bg_bak, _BG_CSV)
            bg_bak.unlink(missing_ok=True)
        if s2t_bak.exists():
            shutil.copy2(s2t_bak, _S2T_CSV)
            s2t_bak.unlink(missing_ok=True)
        return ConfirmationResult(
            term_id=term_id, confirmed_iter_num=iter_num,
            confirmed_at_utc="", confirmed_by=confirmed_by,
            s2t_rows_written=0, success=False,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        if owned and conn is not None:
            conn.close()


def _update_bg_on_confirm(term_id: str, history: dict) -> None:
    """Set status=scope_confirmed and write history for the term."""
    with _BG_CSV.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        headers = r.fieldnames
    for row in rows:
        if row.get("id") == term_id:
            row["status"] = "scope_confirmed"
            row["scope_derivation_history_json"] = json.dumps(
                history, ensure_ascii=False, default=str,
            )
            break
    tmp = _BG_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, lineterminator="\n",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in headers})
    os.replace(tmp, _BG_CSV)


def _rewrite_s2t_for_term(term_id: str, tables: list[str],
                           primary_field_per_table: dict[str, str],
                           rationale_per_table: dict[str, str]) -> int:
    """Delete prior rows for term_id and insert one row per scope table."""
    with _S2T_CSV.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        headers = r.fieldnames

    # Drop prior rows for this term
    rows = [row for row in rows if row.get("business_term_id") != term_id]

    # Determine next id prefix (scan existing ids for next N)
    import re
    existing_nums = []
    for row in rows:
        m = re.match(r"S2T-(\d+)", row.get("id", ""))
        if m:
            existing_nums.append(int(m.group(1)))
    next_id = max(existing_nums) + 1 if existing_nums else 1

    # Load term_name for denormalized business_term_name
    with _BG_CSV.open(encoding="utf-8", newline="") as f:
        bg_rows = list(csv.DictReader(f))
    term_name = next((r["term_name"] for r in bg_rows if r["id"] == term_id),
                     term_id)

    new_rows = []
    for tbl in tables:
        field_ = primary_field_per_table.get(tbl, "").upper()
        rationale = rationale_per_table.get(tbl, "")
        new = {h: "" for h in headers}
        new["id"] = f"S2T-{next_id:04d}"
        new["business_term_id"] = term_id
        new["business_term_name"] = term_name
        new["source_table"] = tbl.upper()
        new["source_field"] = field_
        new["source_description"] = rationale
        new["notes"] = "stage_a_derived"
        new_rows.append(new)
        next_id += 1

    rows.extend(new_rows)

    tmp = _S2T_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, lineterminator="\n",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in headers})
    os.replace(tmp, _S2T_CSV)
    return len(new_rows)


def check_prerequisites(term_id: str,
                        conn: Optional[duckdb.DuckDBPyConnection] = None
                        ) -> PrerequisitesStatus:
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB), read_only=True)
    try:
        term = _load_term(conn, term_id)
        status = term["status"]
        scope = conn.execute(
            "SELECT DISTINCT source_table FROM main_seeds.s2t_mapping "
            "WHERE business_term_id = ? ORDER BY source_table",
            [term_id],
        ).fetchall()
        scope_tables = [r[0].lower() for r in scope]

        dar_counts: dict[str, int] = {}
        for t in scope_tables:
            # F10 fix (#87): source_tables is comma-joined for multi-table
            # DARs (e.g. 'ekbe,ekpo' for grain_relationship). Equality match
            # missed those rows. Split on comma + trim, then test membership;
            # substring-safe (excludes 'ek' from matching 'eket').
            n = conn.execute(
                "SELECT COUNT(*) FROM main_seeds.domain_analysis_results "
                "WHERE list_contains("
                "        list_transform("
                "          string_split(LOWER(source_tables), ','),"
                "          x -> trim(x)"
                "        ),"
                "        LOWER(?)"
                "      ) "
                "  AND status = 'success'",
                [t],
            ).fetchone()[0]
            dar_counts[t] = int(n or 0)

        domain_eda_status = {t: (n > 0) for t, n in dar_counts.items()}
        needed = sorted(t for t, ok in domain_eda_status.items() if not ok)

        if status in ("draft",):
            s2t_readiness = "blocked"
            term_eda_status = "not_applicable_yet"
        elif status in ("scope_confirmed", "domain_eda_pending",
                        "term_eda_pending"):
            s2t_readiness = "blocked"
            term_eda_status = "not_applicable_yet"
        elif status == "ready_for_s2t":
            s2t_readiness = "ready"
            term_eda_status = "not_applicable_yet"
        elif status == "approved":
            s2t_readiness = "done"
            term_eda_status = "not_applicable_yet"
        else:
            s2t_readiness = "blocked"
            term_eda_status = "not_applicable_yet"

        next_steps: list[str] = []
        if status == "draft":
            next_steps.append("Propose scope via Stage A (Term Scope tab).")
        elif status == "scope_confirmed":
            if needed:
                for t in needed:
                    next_steps.append(
                        f"Run domain EDA for {t}: open Data Analysis -> "
                        f"Domain Analysis tab, use the per-table "
                        f"analyzer section at the top, select '{t}' in the "
                        f"dropdown, and click Run on applicable analyzers. "
                        f"Stage A blockers targeting {t} are injected "
                        f"automatically."
                    )
            else:
                next_steps.append("Domain EDA complete for all scope tables. "
                                  "Next: term EDA (Stage C, deferred).")
        elif status == "approved":
            next_steps.append("Term is legacy-approved. No Stage A action needed.")

        return PrerequisitesStatus(
            term_id=term_id, current_status=status, scope_tables=scope_tables,
            domain_eda_status=domain_eda_status,
            domain_eda_needed_on=needed,
            term_eda_status=term_eda_status,
            s2t_readiness=s2t_readiness, next_steps=next_steps,
        )
    finally:
        if owned:
            conn.close()
