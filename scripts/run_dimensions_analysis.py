"""Dimensions analysis end-to-end.

CLI: python scripts/run_dimensions_analysis.py --table ekpo [--term-id BG012]

Flow mirrors run_completeness_analysis.py:
  assemble_context → Claude → execute SQL → post-process → write DAR row.

The prompt instructs the LLM to pick 2-4 dimension columns and, if the
dynamic layer contains a prior Completeness finding with
reliability != 'high' for a column, to handle that column's nulls
explicitly. This is how loop closure is observable (temporal diff:
before dynamic had ELIKZ-medium, generated SQL had no null-aware shape;
after it did).

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
from collections import defaultdict

import duckdb
import requests

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "dbt" / "seeds"
DB_PATH = ROOT / "cpe_analytics.duckdb"
ENV_PATH = ROOT / ".env"
PROMPT_PATH = ROOT / "scripts" / "prompts" / "dimensions_analysis_prompt.md"
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
MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 3

DAR_FIELDS = [
    "id", "analysis_type", "executed_at_utc", "result_json",
    "promoted", "promoted_at_utc", "promoted_to_target_id",
    "run_id", "query_sql", "row_count", "error_message", "status",
    "superseded_by", "executed_by", "schema_version",
    "source_tables", "domain_name",
    "last_source_ingestion_at",  # decision #72 — informational staleness signal
]

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
    out_marker = "## RETRY FEEDBACK"
    sys_start = raw.index(sys_marker) + len(sys_marker)
    sys_end = raw.index(out_marker)
    system_prompt = raw[sys_start:sys_end].strip()
    user_template = raw[sys_end:].strip()
    return system_prompt, user_template


def _next_dar_id() -> str:
    if not DAR_CSV.exists():
        return "DAR-00001"
    with DAR_CSV.open("r", encoding="utf-8", newline="") as f:
        ids = [r.get("id", "") for r in csv.DictReader(f)]
    nums = [int(i.split("-")[1]) for i in ids if i.startswith("DAR-")]
    return f"DAR-{(max(nums) + 1 if nums else 1):05d}"


def _max_source_ingestion(conn, table: str) -> str:
    """Decision #72 age-staleness signal. See run_completeness_analysis.py."""
    try:
        has_col = conn.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
            "AND LOWER(column_name)='ingestion_date'",
            [table],
        ).fetchone()[0]
        if not has_col:
            return ""
        r = conn.execute(f'SELECT MAX("ingestion_date") FROM raw_sap.{table}').fetchone()
        return str(r[0]) if r and r[0] is not None else ""
    except Exception:
        return ""


def _schema_version(conn, table: str) -> str:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    payload = ",".join(f"{c}:{t}" for c, t in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


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


def run(table: str, term_id: str | None, verbose: bool) -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[error] ANTHROPIC_API_KEY not set")
        return 1

    t0 = time.perf_counter()
    print(f"--- Dimensions analysis: raw_sap.{table} ---")

    # known_issue #75: degrade gracefully on fresh tables (zero prior DARs)
    # — strict=True would raise ContextDegradedError and crash silently.
    _has_priors = has_prior_dars_for_scope([table])
    if not _has_priors:
        print(f"  [info] scope=[{table}] has zero prior DARs; "
              f"assemble_context strict=False (degrade gracefully)")
    try:
        bundle = assemble_context(
            purpose="eda_sql_generation",
            scope_tables=[table],
            term_id=term_id,
            max_tokens=20_000,
            strict=_has_priors,
            include_debug_metadata=True,
        )
    except ContextScopeError as e:
        print(f"[error] scope resolution failed: {e}")
        return 3

    print(f"  scope strategy: {bundle.scope_resolution['strategy_used']}")
    print(f"  bundle tokens:  {bundle.token_count}")
    print(f"  layer summary:  {bundle.layer_summary}")
    print(f"  fingerprint:    {bundle.debug['fingerprint']}")
    print(f"  dynamic rows:   {bundle.debug['layer_details']['dynamic']}")

    system_prompt, user_template = _load_prompt_template()
    # Stage B — Stage A blocker injection.
    blocker_entries, truncation_count = load_blockers_for_table(table)
    concerns_block = render_analyst_concerns_block(
        blocker_entries, truncation_count,
    )
    system_prompt = (
        system_prompt
        .replace("{scope_table}", table)
        .replace("{analyst_concerns_block}", concerns_block)
    )
    _debug_path = os.environ.get("STAGE_B_DEBUG_PROMPT_FILE")
    if _debug_path:
        try:
            Path(_debug_path).write_text(system_prompt, encoding="utf-8")
        except OSError as _e:
            print(f"  [warn] STAGE_B_DEBUG_PROMPT_FILE write failed: {_e}")

    conn = duckdb.connect(str(DB_PATH))
    try:
        schema_version = _schema_version(conn, table)

        total_input = 0
        total_output = 0
        retry_events = 0
        retry_feedback = "(no prior attempts)"
        generated_sql: str | None = None
        columns_chosen: list = []
        null_strategy: dict = {}
        rationale: str = ""
        exec_error: str | None = None
        query_result: list | None = None
        last_payload: dict | None = None
        bundle_prompt_used = bundle.formatted_prompt  # captured for substring check

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
                retry_feedback = f"Prior attempt hit API error: {type(e).__name__}: {e}"
                continue

            usage = resp["usage"]
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)
            payload = resp["payload"]
            last_payload = payload if isinstance(payload, dict) else None
            sql = str(payload.get("sql", "")).strip()
            columns_chosen = payload.get("columns_chosen", []) or []
            null_strategy = payload.get("null_strategy_per_column", {}) or {}
            rationale = str(payload.get("rationale", "") or "").strip()

            if not sql:
                retry_events += 1
                retry_feedback = "Prior JSON response had empty 'sql'. Return a complete DuckDB query."
                continue

            if verbose:
                print(f"    columns chosen: {columns_chosen}")
                print(f"    null strategy:  {null_strategy}")
                print(f"    rationale:      {rationale}")
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
                # Provide actual columns of the table for repair
                cols_rows = conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
                    "ORDER BY ordinal_position",
                    [table],
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
            print(f"[error] Dimensions retries exhausted after {MAX_RETRIES}")
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
            return 2 if exec_error else 1

        # Post-process: group rows by column, compute distinct_count + top-N
        per_column = defaultdict(list)
        for row in query_result:
            if len(row) < 3:
                continue
            col_name, value, cnt = row[0], row[1], int(row[2])
            per_column[col_name].append({"value": value, "count": cnt})

        columns_analyzed = []
        for col_name, entries in per_column.items():
            entries.sort(key=lambda x: -x["count"])
            total = sum(e["count"] for e in entries)
            top = []
            for e in entries[:10]:
                pct = round((e["count"] / total) if total else 0.0, 6)
                top.append({
                    "value": e["value"],
                    "count": e["count"],
                    "pct": pct,
                })
            null_count = sum(e["count"] for e in entries
                             if e["value"] is None or str(e["value"]) == "__NULL__")
            columns_analyzed.append({
                "column_name": col_name,
                "distinct_count": len(entries),
                "top_values": top,
                "null_count_in_result": null_count,
                "null_strategy": null_strategy.get(col_name, "none"),
            })

        total_rows_sampled = sum(
            c["count"] for col in columns_analyzed for c in col["top_values"]
        )
        # More accurate total_rows: query the table once
        total_rows_val = conn.execute(
            f"SELECT COUNT(*) FROM raw_sap.{table}"
        ).fetchone()[0]

        blockers_addressed, contract_violation, contract_reason = \
            _extract_blockers_addressed(last_payload)
        result_json = json.dumps({
            "columns_analyzed": columns_analyzed,
            "columns_chosen_by_llm": columns_chosen,
            "null_strategy_per_column": null_strategy,
            "rationale": rationale,
            "total_rows": total_rows_val,
            "bundle_fingerprint": bundle.debug["fingerprint"],
            "blockers_addressed": blockers_addressed,
            "blockers_contract_violation": contract_violation,
            "blockers_contract_violation_reason": contract_reason,
        }, separators=(",", ":"))

        last_ing = _max_source_ingestion(conn, table)
        dar_row = _build_dar_row(
            table=table, status="success",
            query_sql=generated_sql,
            result_json=result_json,
            row_count=len(columns_analyzed),
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
        print(f"  columns analyzed: {len(columns_analyzed)} ({list(per_column.keys())})")
        print(f"  wall:             {wall:.1f}s")
        print(f"  tokens:           input={total_input} output={total_output}")
        print(f"  retry events:     {retry_events}")
        return 0
    finally:
        conn.close()


def _build_dar_row(*, table: str, status: str, query_sql: str,
                   result_json: str, row_count: int, error_message: str,
                   schema_version: str, last_source_ingestion_at: str = "") -> dict:
    run_id = f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    return {
        "id": _next_dar_id(),
        "analysis_type": "dimensions",
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
    ap.add_argument("--term-id", default=None)
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
            source="run_dimensions_analysis",
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
