"""Code Tables analysis end-to-end.

CLI:
  python scripts/run_code_tables_analysis.py --table mseg
  python scripts/run_code_tables_analysis.py --table mseg --code-column BWART

Mirrors run_completeness/dimensions/magnitude: assemble_context → Claude →
execute SQL → post-process → write DAR row.

Anti-hallucination guards (D2.1/D2.2):
  - Prompt forbids CASE-based descriptions; LLM must JOIN.
  - Output JSON carries used_join_not_case self-attestation + description_source tag.
  - Runner greps the generated SQL for \\bCASE\\s+WHEN\\b; flag as
    case_statement_detected in the result_json for post-hoc audit.

Exit codes:
  0 — success
  1 — LLM retry exhausted
  2 — SQL execution failed (all retries)
  3 — scope resolution failed
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "dbt" / "seeds"
DB_PATH = ROOT / "cpe_analytics.duckdb"
ENV_PATH = ROOT / ".env"
PROMPT_PATH = ROOT / "scripts" / "prompts" / "code_tables_analysis_prompt.md"
DAR_CSV = SEED_DIR / "domain_analysis_results.csv"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _context_assembler import (  # noqa: E402
    assemble_context, ContextScopeError, has_prior_dars_for_scope,
)
from _sidecar import now_iso_utc  # noqa: E402
from _dar_supersede import supersede_prior_dars_for_table  # noqa: E402
from _stage_a_blocker_loader import (  # noqa: E402
    load_blockers_for_table, render_analyst_concerns_block,
)


def _extract_blockers_addressed(
    payload: dict | None,
) -> tuple[list, bool, str]:
    """Stage B — soft-coerce `blockers_addressed` out of the LLM payload."""
    if not isinstance(payload, dict):
        return ([], True,
                "LLM response missing or malformed 'blockers_addressed' field")
    if "blockers_addressed" not in payload:
        return ([], True,
                "LLM response missing or malformed 'blockers_addressed' field")
    value = payload.get("blockers_addressed")
    if not isinstance(value, list):
        return ([], True,
                "LLM response missing or malformed 'blockers_addressed' field")
    return (value, False, "")

API_URL = "https://api.anthropic.com/v1/messages"
from _model_config import MODEL  # single source of truth (env: DG_AGENT_MODEL)
MAX_RETRIES = 3

DAR_FIELDS = [
    "id", "analysis_type", "executed_at_utc", "result_json",
    "promoted", "promoted_at_utc", "promoted_to_target_id",
    "run_id", "query_sql", "row_count", "error_message", "status",
    "superseded_by", "executed_by", "schema_version",
    "source_tables", "domain_name",
    "last_source_ingestion_at",
]

# Anti-hallucination regex — catches "CASE WHEN ..." pattern (case insensitive,
# whitespace-tolerant). Legitimate CASTs in SQL do not match this.
CASE_WHEN_RE = re.compile(r"\bCASE\s+WHEN\b", re.IGNORECASE)

if ENV_PATH.exists():
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _load_prompt_template() -> tuple[str, str]:
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    sys_marker = "## SYSTEM PROMPT"
    user_marker = "## RETRY FEEDBACK"
    sys_start = raw.index(sys_marker) + len(sys_marker)
    sys_end = raw.index(user_marker)
    return raw[sys_start:sys_end].strip(), raw[sys_end:].strip()


def _next_dar_id() -> str:
    if not DAR_CSV.exists():
        return "DAR-00001"
    with DAR_CSV.open("r", encoding="utf-8", newline="") as f:
        ids = [r.get("id", "") for r in csv.DictReader(f)]
    nums = [int(i.split("-")[1]) for i in ids if i.startswith("DAR-")]
    return f"DAR-{(max(nums) + 1 if nums else 1):05d}"


def _schema_version(conn, table: str) -> str:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position", [table],
    ).fetchall()
    return hashlib.sha256(
        ",".join(f"{c}:{t}" for c, t in rows).encode("utf-8")
    ).hexdigest()[:12]


def _max_source_ingestion(conn, table: str) -> str:
    """Decision #72 age-staleness signal."""
    try:
        has_col = conn.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
            "AND LOWER(column_name)='ingestion_date'", [table],
        ).fetchone()[0]
        if not has_col:
            return ""
        r = conn.execute(
            f'SELECT MAX("ingestion_date") FROM raw_sap.{table}'
        ).fetchone()
        return str(r[0]) if r and r[0] is not None else ""
    except Exception:
        return ""


def _call_llm(system_prompt: str, user_prompt: str, api_key: str) -> dict:
    r = requests.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL, "max_tokens": 4096,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=180,
    )
    r.raise_for_status()
    body = r.json()
    text = body["content"][0]["text"].strip()
    usage = body.get("usage", {})
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) if not lines[-1].startswith("```") else -1
        text = "\n".join(lines[1:end])
    return {"payload": json.loads(text), "usage": usage}


# Heuristic dimension-candidate discovery used when
# source_column_roles has no entries for the table. Short-VARCHAR + name
# pattern match, same cardinality window [2, 50] as the roles-based path.
_CODE_COL_NAME_SUFFIXES = ("_STATUS", "_TYPE", "_CODE", "_KEY", "_IND",
                           "_FLAG", "_CATEGORY")
_CODE_COL_SAP_NAMES = frozenset({
    "BWART", "AUART", "MTART", "MATKL", "KOSTL", "PSTYV", "LOEKZ",
    "XBLNR", "WAERS", "MEINS", "LGORT", "BUKRS", "WERKS", "LAND1",
    "SPRAS", "APPR_STATUS",
})


def _discover_dimension_candidates_heuristic(conn, table: str) -> list[str]:
    """Fallback dimension-candidate discovery from information_schema.

    Used only when source_column_roles has no entries for `table`.
    Returns column names matching either:
      - VARCHAR with character_maximum_length <= 10 (SAP code convention), or
      - name ends with one of _CODE_COL_NAME_SUFFIXES, or
      - name is in _CODE_COL_SAP_NAMES.

    Production deployments should populate source_column_roles for
    accurate role tagging; this fallback keeps the analyzer from
    hard-erroring on new scope tables.
    """
    try:
        rows = conn.execute(
            "SELECT column_name, data_type, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
            "ORDER BY ordinal_position",
            [table],
        ).fetchall()
    except Exception:
        return []
    out: list[str] = []
    for name, dtype, max_len in rows:
        if not name or not dtype:
            continue
        up = name.upper()
        dt_up = dtype.upper()
        short_varchar = (dt_up == "VARCHAR" and max_len is not None
                         and 1 <= int(max_len) <= 10)
        name_match = (up in _CODE_COL_SAP_NAMES
                      or any(up.endswith(s) for s in _CODE_COL_NAME_SUFFIXES))
        if short_varchar or name_match:
            out.append(name)
    return out


def _auto_pick_code_column(conn, table: str) -> str | None:
    """Pick a low-cardinality dimension-role column suitable for Code Tables.

    Heuristic: cardinality window [2, 50]. Below 2 is trivial (single-value
    column; no code universe to describe). Above 50 is more like a key than a
    code. Within the window, prefer lower cardinality (classic SAP codes are
    typically 4-15 distinct values).

    Refinements (known_issue #27 resolution):
    - Excludes distinct_count < 2 (unchanged — already in place)
    - Prefers columns whose name matches a discovered decoder's code column
      (convention-based discovery via _discover_decoder_candidates) BEFORE
      the legacy BWART preference and lowest-cardinality tiebreaker.
    - BWART preference retained as final SAP-standard bias.

    When source_column_roles has no entries for the table, this
    falls back to heuristic discovery via
    _discover_dimension_candidates_heuristic rather than hard-erroring.

    Returns the column name or None if no candidate fits the window.
    """
    cand = conn.execute(
        "SELECT column_name FROM main_seeds.source_column_roles "
        "WHERE LOWER(table_name) = LOWER(?) AND role='dimension'",
        [table],
    ).fetchall()
    if cand:
        candidate_cols = [col for (col,) in cand]
    else:
        candidate_cols = _discover_dimension_candidates_heuristic(conn, table)
        if candidate_cols:
            print(f"  [info] source_column_roles empty for {table}; "
                  f"heuristic fallback found {len(candidate_cols)} candidate(s)")
    if not candidate_cols:
        return None
    sized: list[tuple[str, int]] = []
    for col in candidate_cols:
        try:
            n = conn.execute(
                f'SELECT COUNT(DISTINCT "{col}") FROM raw_sap.{table}'
            ).fetchone()[0]
            if n and 2 <= n <= 50:
                sized.append((col, int(n)))
        except Exception:
            continue
    if not sized:
        return None

    # Prefer columns matched by a discovered decoder's code col.
    # A column matches when its name (upper-cased) equals or ends with the
    # decoder's code column name. Example: APPR_STATUS matches a decoder
    # whose code column is 'code' via the convention-discovery logic.
    try:
        decoders = _discover_decoder_candidates(conn)
    except Exception:
        decoders = []
    decoder_code_cols = {d["code_col"].upper() for d in decoders}
    for col, _ in sized:
        if col.upper() in decoder_code_cols or col.upper().endswith("_CODE") \
                or col.upper().endswith("_STATUS"):
            return col

    # Prefer BWART if present (bundle has dedicated description source)
    for col, _ in sized:
        if col.upper() == "BWART":
            return col
    # Else lowest cardinality within window
    sized.sort(key=lambda x: x[1])
    return sized[0][0]


# Convention-based decoder discovery (known_issue #40).
# Replaces the hardcoded description-source allowlist with a runtime
# query against main_seeds. A seed qualifies as a decoder when:
#   - has <= 500 rows (decoders are small; larger seeds are not code tables)
#   - has 2-4 columns (a decoder is (code, description) with optional
#     description_en / description_hr variants; bigger is domain data)
#   - has at least one column matching the code-convention:
#       * column named exactly 'code'
#       * column ending in '_code'
#       * column matching an SAP-standard convention ('bwart',
#         'movement_type', 'mvmt_type_code', 'matkl')
#   - has at least one column matching the description-convention:
#       * column named 'description', 'desc', 'name', 'text'
#       * column starting with 'description' (e.g. 'description_en')
# Hardcoded backward-compat anchors preserved so existing
# movement_type_mapping / t156 flow is unchanged.
_DECODER_CODE_COL_CONVENTIONS = ("code", "movement_type", "bwart", "matkl")
_DECODER_CODE_COL_SUFFIX = "_code"
_DECODER_DESC_COL_CONVENTIONS = ("description", "desc", "name", "text",
                                  "description_en", "description_hr",
                                  "btext")


def _discover_decoder_candidates(conn) -> list[dict]:
    """Return list of decoder seeds discovered in main_seeds by shape.

    Each dict: {seed_name, code_col, desc_col, row_count}. Stable order
    by seed_name for deterministic prompt rendering.
    """
    seeds = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main_seeds' ORDER BY table_name"
    ).fetchall()
    out: list[dict] = []
    for (name,) in seeds:
        try:
            cols = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='main_seeds' AND table_name=? "
                "ORDER BY ordinal_position",
                [name],
            ).fetchall()
            col_names = [c[0] for c in cols]
            if not (2 <= len(col_names) <= 4):
                continue
            row_count = conn.execute(
                f"SELECT COUNT(*) FROM main_seeds.{name}"
            ).fetchone()[0]
            if row_count > 500:
                continue
            code_col = None
            desc_col = None
            lower_cols = [c.lower() for c in col_names]
            for c, lc in zip(col_names, lower_cols):
                if code_col is None and (
                    lc == "code"
                    or lc.endswith(_DECODER_CODE_COL_SUFFIX)
                    or lc in _DECODER_CODE_COL_CONVENTIONS
                ):
                    code_col = c
            for c, lc in zip(col_names, lower_cols):
                if desc_col is None and (
                    lc in _DECODER_DESC_COL_CONVENTIONS
                    or lc.startswith("description")
                ):
                    desc_col = c
            if code_col and desc_col and code_col != desc_col:
                out.append({
                    "seed_name": name,
                    "code_col": code_col,
                    "desc_col": desc_col,
                    "row_count": int(row_count),
                })
        except Exception:
            continue
    return out


def _render_decoder_candidates_block(decoders: list[dict]) -> str:
    """Format discovered decoders for the prompt's {decoder_candidates}
    placeholder. Stable ordering; concise table for the LLM to parse.
    """
    if not decoders:
        return ("  (no decoder seeds discovered matching the convention; "
                "fall back to description_source='none' if no SAP-standard "
                "source applies)")
    lines = ["  seed_name | code_col | desc_col | rows"]
    for d in decoders:
        lines.append(
            f"  main_seeds.{d['seed_name']} | {d['code_col']} | "
            f"{d['desc_col']} | {d['row_count']}"
        )
    return "\n".join(lines)


def run(table: str, code_column: str | None, term_id: str | None,
        verbose: bool) -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[error] ANTHROPIC_API_KEY not set")
        return 1

    t0 = time.perf_counter()
    print(f"--- Code Tables analysis: raw_sap.{table} ---")

    # known_issue #75: degrade gracefully on fresh tables (zero prior DARs)
    # — strict=True would raise ContextDegradedError and crash silently.
    _has_priors = has_prior_dars_for_scope([table])
    if not _has_priors:
        print(f"  [info] scope=[{table}] has zero prior DARs; "
              f"assemble_context strict=False (degrade gracefully)")
    try:
        bundle = assemble_context(
            purpose="eda_sql_generation",
            scope_tables=[table], term_id=term_id,
            max_tokens=20_000, strict=_has_priors,
            include_debug_metadata=True,
        )
    except ContextScopeError as e:
        print(f"[error] scope resolution failed: {e}")
        return 3

    print(f"  scope strategy: {bundle.scope_resolution['strategy_used']}")
    print(f"  bundle tokens:  {bundle.token_count}")
    print(f"  layer summary:  {bundle.layer_summary}")
    print(f"  fingerprint:    {bundle.debug['fingerprint']}")

    conn = duckdb.connect(str(DB_PATH))
    try:
        if not code_column:
            code_column = _auto_pick_code_column(conn, table)
            if not code_column:
                # Neither source_column_roles nor the heuristic
                # fallback found a candidate in the [2,50] cardinality window.
                # Stage D.1: emit canonical skipped DAR (prereq counts it as
                # satisfying 'code_tables' requirement).
                print(f"[info] no dimension candidates found on raw_sap.{table} "
                      f"(source_column_roles empty AND heuristic fallback "
                      f"returned nothing in the [2,50] cardinality window)")
                from _skipped_dar import build_skipped_dar_row  # noqa: E402
                skipped = build_skipped_dar_row(
                    dar_id=_next_dar_id(),
                    analysis_type="code_tables",
                    source_tables=table,
                    skip_reason=(
                        "no dimension-role columns in source_column_roles "
                        "and no heuristic candidates in cardinality window "
                        "[2, 50]"
                    ),
                    schema_version=_schema_version(conn, table),
                    last_source_ingestion_at=_max_source_ingestion(conn, table),
                    executed_by="run_code_tables_analysis.py",
                )
                _append_dar(skipped)
                supersede_prior_dars_for_table(
                    skipped["analysis_type"], skipped["source_tables"],
                    [skipped["id"]],
                )
                print(f"  [ok] {skipped['id']} code_tables {table}: SKIPPED")
                return 0
            print(f"  auto-picked code column: {code_column}")

        schema_version = _schema_version(conn, table)
        # Convention-discovered decoder list injected into
        # the prompt via {decoder_candidates} placeholder. Enables the LLM
        # to JOIN against project-added decoders (e.g. zmm_approval_status)
        # without the analyzer's hardcoded allowlist being updated per seed.
        decoders = _discover_decoder_candidates(conn)
        decoder_candidates_block = _render_decoder_candidates_block(decoders)
        print(f"  decoder candidates discovered: {len(decoders)}")
        for d in decoders:
            print(f"    - {d['seed_name']}({d['code_col']} -> {d['desc_col']}, {d['row_count']} rows)")

        system_prompt, user_template = _load_prompt_template()
        # Stage B — Stage A blocker injection.
        blocker_entries, truncation_count = load_blockers_for_table(table)
        concerns_block = render_analyst_concerns_block(
            blocker_entries, truncation_count,
        )
        system_prompt = (system_prompt
                         .replace("{scope_table}", table)
                         .replace("{code_column}", code_column)
                         .replace("{decoder_candidates}",
                                  decoder_candidates_block)
                         .replace("{analyst_concerns_block}", concerns_block))
        _debug_path = os.environ.get("STAGE_B_DEBUG_PROMPT_FILE")
        if _debug_path:
            try:
                Path(_debug_path).write_text(system_prompt, encoding="utf-8")
            except OSError as _e:
                print(f"  [warn] STAGE_B_DEBUG_PROMPT_FILE write failed: {_e}")

        total_input = 0
        total_output = 0
        retry_events = 0
        retry_feedback = "(no prior attempts)"
        generated_sql: str | None = None
        code_col_chosen = ""
        desc_source_chosen = ""
        desc_source_reason = ""
        used_join_claim = False
        rationale = ""
        exec_error: str | None = None
        query_result: list | None = None
        last_payload: dict | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            user_prompt = (
                user_template
                .replace("{context_bundle}", bundle.formatted_prompt)
                .replace("{retry_feedback}", retry_feedback)
                .replace("{scope_table}", table)
                .replace("{code_column}", code_column)
                .replace("{decoder_candidates}", decoder_candidates_block)
            )
            print(f"  LLM attempt {attempt}/{MAX_RETRIES}...")
            try:
                resp = _call_llm(system_prompt, user_prompt, api_key)
            except Exception as e:
                print(f"    [warn] LLM call failed: {e}")
                retry_events += 1
                retry_feedback = f"Prior attempt hit API error: {type(e).__name__}: {e}"
                continue

            usage = resp["usage"]
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)
            payload = resp["payload"]
            last_payload = payload if isinstance(payload, dict) else None
            sql = str(payload.get("sql", "")).strip()
            code_col_chosen = str(payload.get("code_column_chosen", "")).strip()
            desc_source_chosen = str(payload.get("description_source_chosen", "")).strip()
            desc_source_reason = str(payload.get("description_source_reason", "") or "").strip()
            used_join_claim = bool(payload.get("used_join_not_case", False))
            rationale = str(payload.get("rationale", "") or "").strip()

            if not sql or not code_col_chosen or not desc_source_chosen:
                retry_events += 1
                retry_feedback = (
                    "Prior JSON missing one of: sql, code_column_chosen, "
                    "description_source_chosen. Return all required fields populated."
                )
                continue

            if verbose:
                print(f"    code_column: {code_col_chosen}")
                print(f"    desc_source: {desc_source_chosen}")
                print(f"    reason:      {desc_source_reason}")
                print(f"    used_join_not_case (claim): {used_join_claim}")
                print(f"    rationale:   {rationale}")
                print(f"    SQL ({len(sql)} chars):")
                for ln in sql.splitlines():
                    print(f"      {ln}")

            try:
                query_result = conn.execute(sql).fetchall()
                generated_sql = sql
                exec_error = None
                print(f"    [ok] SQL returned {len(query_result)} rows")
                break
            except Exception as e:
                exec_error = str(e)
                retry_events += 1
                cols_rows = conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
                    "ORDER BY ordinal_position", [table],
                ).fetchall()
                cols_dump = ", ".join(c[0] for c in cols_rows)
                retry_feedback = (
                    f"Prior SQL failed: {exec_error}\n\n"
                    f"Actual columns of raw_sap.{table}: {cols_dump}\n\n"
                    "Only reference columns from this list. Fix the SQL."
                )
                print(f"    [retry] execution error: {exec_error[:200]}")
                generated_sql = sql

        if query_result is None or generated_sql is None:
            print(f"[error] Code Tables retries exhausted after {MAX_RETRIES}")
            err_row = _build_dar_row(
                table=table, status="error",
                query_sql=generated_sql or "",
                result_json="", row_count=0,
                error_message=exec_error or "retries exhausted",
                schema_version=schema_version,
                last_source_ingestion_at=_max_source_ingestion(conn, table),
            )
            _append_dar(err_row)
            supersede_prior_dars_for_table(
                err_row["analysis_type"], err_row["source_tables"],
                [err_row["id"]],
            )
            return 2 if exec_error else 1

        # Anti-hallucination audit: grep the SQL for CASE WHEN
        case_match = CASE_WHEN_RE.search(generated_sql)
        case_detected = case_match is not None
        llm_lied = used_join_claim and case_detected
        if case_detected:
            print(f"    [AUDIT] CASE WHEN detected in SQL at position {case_match.start()}")
        if llm_lied:
            print("    [AUDIT] LLM self-attestation mismatch: used_join_not_case=true but CASE WHEN present")

        # Post-process: build mappings list
        mappings = []
        for row in query_result:
            if len(row) < 4:
                continue
            code, description, description_source, cnt = row[0], row[1], row[2], int(row[3])
            mappings.append({
                "code": code,
                "description": description,
                "description_source": description_source,
                "count": cnt,
            })

        total_rows = conn.execute(
            f"SELECT COUNT(*) FROM raw_sap.{table}"
        ).fetchone()[0]

        # Collect distinct description_source values seen in output for audit
        sources_seen = sorted({m["description_source"] for m in mappings})
        none_count = sum(1 for m in mappings if m["description_source"] == "none")
        null_desc = sum(1 for m in mappings if m["description"] is None)

        blockers_addressed, contract_violation, contract_reason = \
            _extract_blockers_addressed(last_payload)
        result_json = json.dumps({
            "code_column": code_col_chosen,
            "description_source_used": desc_source_chosen,
            "description_source_reason": desc_source_reason,
            "used_join_not_case": used_join_claim,
            "case_statement_detected": case_detected,
            "llm_self_attestation_mismatch": llm_lied,
            "mappings": mappings,
            "total_rows": total_rows,
            "distinct_sources_in_output": sources_seen,
            "rows_with_source_none": none_count,
            "rows_with_null_description": null_desc,
            "rationale": rationale,
            "bundle_fingerprint": bundle.debug["fingerprint"],
            "blockers_addressed": blockers_addressed,
            "blockers_contract_violation": contract_violation,
            "blockers_contract_violation_reason": contract_reason,
        }, separators=(",", ":"), default=str)

        dar_row = _build_dar_row(
            table=table, status="success",
            query_sql=generated_sql,
            result_json=result_json,
            row_count=len(mappings),
            error_message="",
            schema_version=schema_version,
            last_source_ingestion_at=_max_source_ingestion(conn, table),
        )
        _append_dar(dar_row)
        supersede_prior_dars_for_table(
            dar_row["analysis_type"], dar_row["source_tables"],
            [dar_row["id"]],
        )
        wall = time.perf_counter() - t0
        print(f"  [ok] row {dar_row['id']} appended to {DAR_CSV.name}")
        print(f"  mappings: {len(mappings)}  distinct_sources: {sources_seen}")
        print(f"  CASE detected: {case_detected}  LLM-lied: {llm_lied}")
        print(f"  wall: {wall:.1f}s  tokens: in={total_input} out={total_output}")
        print(f"  retry events: {retry_events}")
        return 0
    finally:
        conn.close()


def _build_dar_row(*, table: str, status: str, query_sql: str,
                   result_json: str, row_count: int, error_message: str,
                   schema_version: str,
                   last_source_ingestion_at: str = "") -> dict:
    run_id = f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    return {
        "id": _next_dar_id(),
        "analysis_type": "code_tables",
        "executed_at_utc": now_iso_utc(),
        "result_json": result_json,
        "promoted": "false",
        "promoted_at_utc": "",
        "promoted_to_target_id": "",
        "run_id": run_id,
        "query_sql": query_sql,
        "row_count": str(row_count),
        "error_message": error_message,
        "status": status,
        "superseded_by": "",
        "executed_by": "guided_analysis_llm",
        "schema_version": schema_version,
        "source_tables": table.lower(),
        "domain_name": "",
        "last_source_ingestion_at": last_source_ingestion_at,
    }


def _append_dar(row: dict) -> None:
    existing: list[dict] = []
    if DAR_CSV.exists():
        with DAR_CSV.open("r", encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))
    existing.append(row)
    tmp = DAR_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=DAR_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(existing)
    os.replace(tmp, DAR_CSV)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", required=True)
    ap.add_argument("--code-column", default=None,
                    help="Override the auto-picked code column")
    ap.add_argument("--term-id", default=None)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-parquet-sync", action="store_true",
                    help="Skip post-write parquet export + Streamlit view invalidation (for batch callers)")
    args = ap.parse_args()
    rc = run(args.table, args.code_column, args.term_id, args.verbose)
    if rc == 0:
        # known_issue #53 — see scripts/_parquet_sync.py module docstring.
        from _parquet_sync import sync_parquet_and_invalidate
        sync_parquet_and_invalidate(
            project_root=Path(__file__).resolve().parent.parent,
            seed_name="domain_analysis_results",
            skip=args.no_parquet_sync,
            source="run_code_tables_analysis",
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
