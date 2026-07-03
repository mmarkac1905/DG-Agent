"""Completeness analysis end-to-end.

CLI: python scripts/run_completeness_analysis.py --table ekpo [--term-id BG012]

Flow:
  1. Resolve scope (explicit --table wins; else via assemble_context cascade).
  2. Build context bundle via assemble_context(purpose='eda_sql_generation').
  3. Call Claude with bundle + completeness prompt template.
  4. Execute returned SQL against raw_sap.<table>. RULE 38 retry loop on error.
  5. Post-process null_count/total_rows → reliability + structured JSON in
     the standard completeness result shape.
  6. Append row to domain_analysis_results.csv with the full DAR schema.

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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from _source_config import SOURCE_SCHEMA
import requests

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "dbt" / "seeds"
DB_PATH = ROOT / "cpe_analytics.duckdb"
ENV_PATH = ROOT / ".env"
PROMPT_PATH = ROOT / "scripts" / "prompts" / "completeness_analysis_prompt.md"
DAR_CSV = SEED_DIR / "domain_analysis_results.csv"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _context_assembler import (  # noqa: E402
    assemble_context, ContextScopeError, has_prior_dars_for_scope,
)
from _sidecar import now_iso_utc  # noqa: E402
from _stage_a_blocker_loader import (  # noqa: E402
    load_blockers_for_table, render_analyst_concerns_block,
)
from _dar_supersede import supersede_prior_dars_for_table  # noqa: E402

API_URL = "https://api.anthropic.com/v1/messages"
from _model_config import MODEL  # single source of truth (env: DG_AGENT_MODEL)
MAX_RETRIES = 3

DAR_FIELDS = [
    "id", "analysis_type", "executed_at_utc", "result_json",
    "promoted", "promoted_at_utc", "promoted_to_target_id",
    "run_id", "query_sql", "row_count", "error_message", "status",
    "superseded_by", "executed_by", "schema_version",
    "source_tables", "domain_name",
    "last_source_ingestion_at",  # decision #72 — informational staleness signal
]

# --- .env loader (same pattern as sync_s2t_plain_from_dbt.py) ---
if ENV_PATH.exists():
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _load_prompt_template() -> tuple[str, str]:
    """Split the markdown into (system, user_template). System comes from
    the block between '## SYSTEM PROMPT' and '## OUTPUT FORMAT'; user_template
    is everything from '## OUTPUT FORMAT' to end, interpolated at call time."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    # Split on the canonical headings
    sys_marker = "## SYSTEM PROMPT"
    out_marker = "## OUTPUT FORMAT"
    task_marker = "## TASK"
    if sys_marker not in raw or out_marker not in raw:
        raise RuntimeError(f"Prompt template at {PROMPT_PATH} is malformed")
    sys_start = raw.index(sys_marker) + len(sys_marker)
    sys_end = raw.index(out_marker)
    system_prompt = raw[sys_start:sys_end].strip()
    # User prompt = output format + retry feedback + context bundle + task
    user_template = raw[sys_end:].strip()
    return system_prompt, user_template


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
        f"WHERE table_schema='{SOURCE_SCHEMA}' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    payload = ",".join(f"{c}:{t}" for c, t in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _table_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema='{SOURCE_SCHEMA}' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return [r[0] for r in rows]


def _max_source_ingestion(conn, table: str) -> str:
    """Decision #72 age-staleness signal.

    Returns MAX(ingestion_date)::VARCHAR from raw_sap.<table> when the column
    exists, else empty string. Never raises — missing column is the expected
    case today (known_issue #25). Informational only; not a blocker.
    """
    try:
        has_col = conn.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            f"WHERE table_schema='{SOURCE_SCHEMA}' AND LOWER(table_name)=LOWER(?) "
            "AND LOWER(column_name)='ingestion_date'",
            [table],
        ).fetchone()[0]
        if not has_col:
            return ""
        r = conn.execute(
            f'SELECT MAX("ingestion_date") FROM {SOURCE_SCHEMA}.{table}'
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
            "model": MODEL,
            "max_tokens": 4096,
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


def _extract_blockers_addressed(
    payload: dict | None,
) -> tuple[list, bool, str]:
    """Stage B: pull `blockers_addressed` from the LLM payload, soft-coerce.

    Returns (blockers_addressed, contract_violation, reason).

    If the field is present and is a list: return (list, False, "").
    If the field is absent or not a list: return ([], True, reason).
    Soft-coerce keeps analysis content even when the LLM drops the field;
    `blockers_contract_violation=True` makes the failure observable in DAR.
    """
    if not isinstance(payload, dict):
        return (
            [], True,
            "LLM response missing or malformed 'blockers_addressed' field",
        )
    if "blockers_addressed" not in payload:
        return (
            [], True,
            "LLM response missing or malformed 'blockers_addressed' field",
        )
    value = payload.get("blockers_addressed")
    if not isinstance(value, list):
        return (
            [], True,
            "LLM response missing or malformed 'blockers_addressed' field",
        )
    return (value, False, "")


def _reliability(null_pct: float) -> str:
    if null_pct < 0.05:
        return "high"
    if null_pct < 0.25:
        return "medium"
    return "low"


def run(table: str, term_id: str | None, verbose: bool) -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[error] ANTHROPIC_API_KEY not set (cannot call Claude)")
        return 1

    t0 = time.perf_counter()
    print(f"--- Completeness analysis: {SOURCE_SCHEMA}.{table} ---")

    # Stage 1: scope + bundle
    # known_issue #75: strict=True raises ContextDegradedError when the
    # HEAVY dynamic layer is empty. For a fresh raw table with zero prior
    # DARs, that's expected — degrade gracefully with strict=False so the
    # analyzer can still write a DAR from Layer A/B + ontology signals.
    _has_priors = has_prior_dars_for_scope([table])
    if not _has_priors:
        print(f"  [info] scope=[{table}] has zero prior DARs; "
              f"assemble_context strict=False (degrade gracefully)")
    try:
        bundle = assemble_context(
            purpose="eda_sql_generation",
            scope_tables=[table],
            max_tokens=20_000,
            strict=_has_priors,
            include_debug_metadata=True,
        )
    except ContextScopeError as e:
        print(f"[error] scope resolution failed: {e}")
        return 3

    print(f"  scope strategy: {bundle.scope_resolution['strategy_used']}")
    print(f"  resolved:       {bundle.scope_resolution['resolved_tables']}")
    print(f"  bundle tokens:  {bundle.token_count}")
    print(f"  layer summary:  {bundle.layer_summary}")
    print(f"  fingerprint:    {bundle.debug['fingerprint']}")

    # Stage 2: prompt assembly
    system_prompt, user_template = _load_prompt_template()
    # Stage B: load Stage A blockers targeting this table with
    # resolves_in='domain_eda'. Empty list -> placeholder renders empty
    # (tolerance path).
    blocker_entries, truncation_count = load_blockers_for_table(table)
    concerns_block = render_analyst_concerns_block(
        blocker_entries, truncation_count,
    )
    # system_prompt has {scope_table} and {analyst_concerns_block} placeholders
    system_prompt = (
        system_prompt
        .replace("{scope_table}", table)
        .replace("{analyst_concerns_block}", concerns_block)
    )
    # Stage B debug hook — scenario 23 asserts injection reached the LLM.
    _debug_path = os.environ.get("STAGE_B_DEBUG_PROMPT_FILE")
    if _debug_path:
        try:
            Path(_debug_path).write_text(system_prompt, encoding="utf-8")
        except OSError as _e:
            print(f"  [warn] STAGE_B_DEBUG_PROMPT_FILE write failed: {_e}")

    # Stage 3: LLM + retry loop (RULE 38)
    conn = duckdb.connect(str(DB_PATH))  # read-write for the final CSV seed op
    try:
        schema_version = _schema_version(conn, table)
        columns = _table_columns(conn, table)
        if not columns:
            print(f"[error] {SOURCE_SCHEMA}.{table} has no columns (or doesn't exist)")
            return 2

        total_input_tokens = 0
        total_output_tokens = 0
        retry_events = 0
        retry_feedback = "(no prior attempts)"
        generated_sql: str | None = None
        exec_error: str | None = None
        query_result: list | None = None
        last_payload: dict | None = None  # for blockers_addressed extraction

        for attempt in range(1, MAX_RETRIES + 1):
            user_prompt = (
                user_template
                .replace("{context_bundle}", bundle.formatted_prompt)
                .replace("{retry_feedback}", retry_feedback)
                .replace("{scope_table}", table)
            )
            print(f"  LLM attempt {attempt}/{MAX_RETRIES}...")
            try:
                resp = _call_llm(system_prompt, user_prompt, api_key)
            except Exception as e:
                print(f"    [warn] LLM call failed: {e}")
                retry_events += 1
                retry_feedback = (
                    f"Prior attempt hit API error: {type(e).__name__}: {e}. "
                    "Retry with the same task."
                )
                continue

            usage = resp["usage"]
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)
            last_payload = resp["payload"] if isinstance(resp.get("payload"), dict) else None
            sql = str(resp["payload"].get("sql", "")).strip()
            if not sql:
                retry_events += 1
                retry_feedback = (
                    "Prior JSON response had an empty 'sql' field. "
                    "Return a complete DuckDB query in the 'sql' key."
                )
                continue

            if verbose:
                print(f"    generated SQL ({len(sql)} chars):")
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
                schema_dump = "\n".join(f"  {c}" for c in columns)
                retry_feedback = (
                    f"Prior SQL failed execution:\n"
                    f"  Error: {exec_error}\n\n"
                    f"Actual columns of {SOURCE_SCHEMA}.{table}:\n{schema_dump}\n\n"
                    "Fix the SQL so it runs against only these columns and "
                    "returns one row per column with (column_name, null_count, "
                    "total_rows)."
                )
                print(f"    [retry] execution error: {exec_error[:200]}")
                generated_sql = sql  # keep the attempt for debugging

        if query_result is None or generated_sql is None:
            print(f"[error] LLM/SQL retries exhausted after {MAX_RETRIES} attempts")
            # Write error row
            err_row = _build_dar_row(
                table=table, status="error",
                query_sql=generated_sql or "",
                result_json="",
                row_count=0,
                error_message=exec_error or "retries exhausted",
                schema_version=schema_version,
                last_source_ingestion_at=_max_source_ingestion(conn, table),
            )
            _append_dar(err_row)
            supersede_prior_dars_for_table(
                err_row["analysis_type"], err_row["source_tables"],
                [err_row["id"]],
            )
            print(f"  error row {err_row['id']} written to {DAR_CSV.name}")
            return 2 if exec_error else 1

        # Stage 5: post-process into the standard completeness result shape
        column_checks = []
        total_rows_val = 0
        for row in query_result:
            # Accept either (column_name, null_count, total_rows) or a
            # 2-col variant (column_name, null_count) — defensive.
            if len(row) < 3:
                continue
            col_name, null_count, total_rows = row[0], int(row[1]), int(row[2])
            total_rows_val = max(total_rows_val, total_rows)
            null_pct = (null_count / total_rows) if total_rows else 0.0
            column_checks.append({
                "column": col_name,
                "null_count": null_count,
                "null_pct": round(null_pct, 6),
                "reliability": _reliability(null_pct),
            })
        # Stage B: extract blockers_addressed from the LLM payload + record
        # contract violation if the field is missing or malformed.
        blockers_addressed, contract_violation, contract_reason = \
            _extract_blockers_addressed(last_payload)
        result_dict = {
            "column_checks": column_checks,
            "total_rows": total_rows_val,
            "blockers_addressed": blockers_addressed,
            "blockers_contract_violation": contract_violation,
            "blockers_contract_violation_reason": contract_reason,
        }
        result_json = json.dumps(result_dict, separators=(",", ":"))

        # Stage 6: write row
        last_ing = _max_source_ingestion(conn, table)
        dar_row = _build_dar_row(
            table=table, status="success",
            query_sql=generated_sql,
            result_json=result_json,
            row_count=len(column_checks),
            error_message="",
            schema_version=schema_version,
            last_source_ingestion_at=last_ing,
        )
        _append_dar(dar_row)
        supersede_prior_dars_for_table(
            dar_row["analysis_type"], dar_row["source_tables"],
            [dar_row["id"]],
        )
        wall = time.perf_counter() - t0
        print(f"  [ok] row {dar_row['id']} appended to {DAR_CSV.name}")
        print(f"  columns checked: {len(column_checks)}")
        print(f"  total_rows seen: {total_rows_val}")
        print(f"  wall time:       {wall:.1f}s")
        print(f"  token usage:     input={total_input_tokens} output={total_output_tokens}")
        print(f"  retry events:    {retry_events}")
        print(f"  schema_version:  {schema_version}")
        return 0
    finally:
        conn.close()


def _build_dar_row(*, table: str, status: str, query_sql: str,
                   result_json: str, row_count: int, error_message: str,
                   schema_version: str, last_source_ingestion_at: str = "") -> dict:
    run_id = f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    return {
        "id": _next_dar_id(),
        "analysis_type": "completeness",
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
    header_needed = not DAR_CSV.exists() or DAR_CSV.stat().st_size == 0
    # CSV reader-then-writer to rewrite the whole file with the new row
    # (preserves header + LF line endings; simpler than append mode).
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
    ap.add_argument("--table", required=True, metavar="NAME",
                    help=f"{SOURCE_SCHEMA} table to analyze (lowercase)")
    ap.add_argument("--term-id", metavar="ID", default=None,
                    help="optional business_term_id for scope cascade")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-parquet-sync", action="store_true",
                    help="Skip post-write parquet export + Streamlit view invalidation (for batch callers)")
    args = ap.parse_args()
    rc = run(args.table, args.term_id, args.verbose)
    if rc == 0:
        # known_issue #53 — see scripts/_parquet_sync.py module docstring.
        from _parquet_sync import sync_parquet_and_invalidate
        sync_parquet_and_invalidate(
            project_root=Path(__file__).resolve().parent.parent,
            seed_name="domain_analysis_results",
            skip=args.no_parquet_sync,
            source="run_completeness_analysis",
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
