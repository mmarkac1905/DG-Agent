"""Phase 15a piece 6 Gate D1 — Magnitude analysis end-to-end.

CLI: python scripts/run_magnitude_analysis.py --table ekpo [--term-id BG012]

Mirrors run_completeness/dimensions: assemble_context → Claude with
magnitude prompt → execute SQL → post-process → write DAR row.

Output result_json per piece 2 §4b magnitude shape:
  {grouping_dimension, measure, aggregation, top_n, total_rows, measure_total}

Exit codes same as siblings (0 ok, 1 LLM exhausted, 2 SQL failed, 3 scope).
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
import requests

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "dbt" / "seeds"
DB_PATH = ROOT / "cpe_analytics.duckdb"
ENV_PATH = ROOT / ".env"
PROMPT_PATH = ROOT / "scripts" / "prompts" / "magnitude_analysis_prompt.md"
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
        "ORDER BY ordinal_position", [table],
    ).fetchall()
    return hashlib.sha256(
        ",".join(f"{c}:{t}" for c, t in rows).encode("utf-8")
    ).hexdigest()[:12]


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


def _discover_numeric_columns_heuristic(conn, table: str) -> list[str]:
    """Stage D.1 fallback when source_column_roles is empty/under-classified
    for `table`. Queries information_schema.columns in the raw_sap schema
    for numeric data types. Excludes SAP client-id columns (MANDT/CLIENT)
    which are numeric-typed but never real measures.

    Mirrors run_code_tables_analysis._discover_dimension_candidates_heuristic
    (known_issue #45 resolution pattern).
    """
    try:
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'raw_sap'
              AND LOWER(table_name) = LOWER(?)
              AND (
                  UPPER(data_type) LIKE 'DECIMAL%'
                  OR UPPER(data_type) IN (
                      'DOUBLE', 'INTEGER', 'BIGINT', 'FLOAT',
                      'REAL', 'NUMERIC', 'HUGEINT', 'SMALLINT'
                  )
              )
              AND LOWER(column_name) NOT IN ('mandt', 'client')
            ORDER BY ordinal_position
            """,
            [table],
        ).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows]


def _role_of(conn, table: str, column: str) -> str:
    r = conn.execute(
        "SELECT role FROM main_seeds.source_column_roles "
        "WHERE LOWER(table_name)=LOWER(?) AND UPPER(column_name)=UPPER(?)",
        [table, column],
    ).fetchone()
    return r[0] if r else "unknown"


def run(table: str, term_id: str | None, verbose: bool) -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[error] ANTHROPIC_API_KEY not set")
        return 1

    t0 = time.perf_counter()
    print(f"--- Magnitude analysis: raw_sap.{table} ---")

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

        # Stage D.1 — pre-LLM measure-column check.
        # Query source_column_roles first; if empty, fall back to
        # information_schema heuristic (known_issue #45 pattern from
        # run_code_tables_analysis). Only emit skipped DAR when neither
        # path surfaces candidate measure columns.
        measure_rows = conn.execute(
            "SELECT column_name FROM main_seeds.source_column_roles "
            "WHERE LOWER(table_name)=LOWER(?) AND role='measure'",
            [table],
        ).fetchall()
        if measure_rows:
            measure_source = "source_column_roles"
        else:
            heuristic_numeric = _discover_numeric_columns_heuristic(conn, table)
            if not heuristic_numeric:
                # Neither classified nor heuristic candidates — truly no
                # usable measure columns on this table. Emit skipped DAR.
                from _skipped_dar import build_skipped_dar_row  # noqa: E402
                print(f"[info] no measure columns on {table} "
                      f"(source_column_roles empty, no numeric columns "
                      f"in information_schema for raw_sap.{table})")
                skipped = build_skipped_dar_row(
                    dar_id=_next_dar_id(),
                    analysis_type="magnitude",
                    source_tables=table,
                    skip_reason=(
                        "no measure columns in source_column_roles and no "
                        "numeric columns in information_schema raw_sap"
                    ),
                    schema_version=schema_version,
                    last_source_ingestion_at=_max_source_ingestion(conn, table),
                    executed_by="run_magnitude_analysis.py",
                )
                _append_dar(skipped)
                supersede_prior_dars_for_table(
                    skipped["analysis_type"], skipped["source_tables"],
                    [skipped["id"]],
                )
                print(f"  [ok] {skipped['id']} magnitude {table}: SKIPPED")
                # Do NOT call _emit_performance_baseline_dars — performance_baseline
                # is auto-satisfied by the skipped magnitude DAR per Stage C prereq.
                return 0
            measure_source = "heuristic"
            print(f"  [info] source_column_roles has no measures for {table}; "
                  f"heuristic found {len(heuristic_numeric)} numeric column(s)")

        total_input = 0
        total_output = 0
        retry_events = 0
        retry_feedback = "(no prior attempts)"
        generated_sql: str | None = None
        measure_chosen: str = ""
        dimension_chosen: str = ""
        aggregation: str = "SUM"
        rationale: str = ""
        null_filter: bool = False
        exec_error: str | None = None
        query_result: list | None = None
        last_payload: dict | None = None
        shape_used: str = "A"
        code_tables_consumed: bool = False

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
            measure_chosen = str(payload.get("measure_chosen", "")).strip()
            dimension_chosen = str(payload.get("dimension_chosen", "")).strip()
            aggregation = str(payload.get("aggregation", "SUM")).strip()
            null_filter = bool(payload.get("null_filter_applied", False))
            shape_used = str(payload.get("shape_used", "A")).strip().upper()
            code_tables_consumed = bool(payload.get("code_tables_consumed", False))
            rationale = str(payload.get("rationale", "") or "").strip()

            if not sql or not measure_chosen or not dimension_chosen:
                retry_events += 1
                retry_feedback = (
                    "Prior JSON missing sql/measure_chosen/dimension_chosen. "
                    "Return all three required fields populated."
                )
                continue

            if verbose:
                print(f"    measure:   {measure_chosen}  (role={_role_of(conn, table, measure_chosen)})")
                print(f"    dimension: {dimension_chosen}  (role={_role_of(conn, table, dimension_chosen)})")
                print(f"    rationale: {rationale}")
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
            print(f"[error] Magnitude retries exhausted after {MAX_RETRIES}")
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

        # Post-process — build piece 2 §4b magnitude shape
        # Accepts Shape A (dim_value, measure_total, row_count) OR
        # Shape B (dim_value, description, measure_total, row_count).
        top_n = []
        measure_total_sum = 0.0
        observed_shape = "A"
        for row in query_result:
            if len(row) == 4:
                observed_shape = "B"
                dim_val, description, total_raw, rowcount_raw = row[0], row[1], row[2], row[3]
            elif len(row) >= 3:
                dim_val, description, total_raw, rowcount_raw = row[0], None, row[1], row[2]
            else:
                continue
            total = float(total_raw) if total_raw is not None else 0.0
            measure_total_sum += total
            top_n.append({
                "dim_value": dim_val,
                "description": description,
                "measure_total": total,
                "row_count": int(rowcount_raw) if rowcount_raw is not None else 0,
            })
        # Add pct once we know the sum — based on observed top-10 only
        for t in top_n:
            t["pct"] = round(
                (t["measure_total"] / measure_total_sum) if measure_total_sum else 0.0, 6
            )

        total_rows = conn.execute(
            f"SELECT COUNT(*) FROM raw_sap.{table}"
        ).fetchone()[0]

        # Verify roles post-hoc (for the Gate D1 check)
        meas_role = _role_of(conn, table, measure_chosen)
        dim_role = _role_of(conn, table, dimension_chosen)
        roles_valid = (meas_role == "measure" and dim_role == "dimension")

        blockers_addressed, contract_violation, contract_reason = \
            _extract_blockers_addressed(last_payload)
        result_json = json.dumps({
            "grouping_dimension": dimension_chosen,
            "measure": measure_chosen,
            "aggregation": aggregation,
            "top_n": top_n,
            "total_rows": total_rows,
            "measure_total_top_n": measure_total_sum,
            "null_filter_applied": null_filter,
            "shape_claimed": shape_used,
            "shape_observed": observed_shape,
            "code_tables_consumed": code_tables_consumed,
            "rationale": rationale,
            "bundle_fingerprint": bundle.debug["fingerprint"],
            "measure_source": measure_source,  # Stage D.1: classified vs heuristic
            "roles_validated": {
                "measure_role": meas_role,
                "dimension_role": dim_role,
                "both_valid": roles_valid,
            },
            "blockers_addressed": blockers_addressed,
            "blockers_contract_violation": contract_violation,
            "blockers_contract_violation_reason": contract_reason,
        }, separators=(",", ":"), default=str)

        last_ing = _max_source_ingestion(conn, table)
        dar_row = _build_dar_row(
            table=table, status="success",
            query_sql=generated_sql,
            result_json=result_json,
            row_count=len(top_n),
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
        print(f"  measure × dimension: {measure_chosen} × {dimension_chosen}")
        print(f"  role validation: measure={meas_role}, dimension={dim_role}, "
              f"both_valid={roles_valid}")
        print(f"  top buckets: {len(top_n)}")
        print(f"  wall: {wall:.1f}s  tokens: in={total_input} out={total_output}")
        print(f"  retry events: {retry_events}")

        # v3.9 §25.3 / 8.4.6 — performance_baseline co-emission. After
        # the magnitude DAR writes, compute per-numeric-column baseline
        # statistics (avg/min/max/stddev/p25/p75) and emit a second
        # DAR row per numeric measure column. Deterministic SQL; no
        # additional LLM call. Feeds Layer A's typical_values_range_json
        # via compile_semantic_model.py.
        _emit_performance_baseline_dars(
            conn, table, schema_version=schema_version,
            last_source_ingestion_at=last_ing,
        )
        return 0
    finally:
        conn.close()


def _build_dar_row(*, table: str, status: str, query_sql: str,
                   result_json: str, row_count: int, error_message: str,
                   schema_version: str, last_source_ingestion_at: str = "") -> dict:
    run_id = f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    return {
        "id": _next_dar_id(),
        "analysis_type": "magnitude",
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


def _emit_performance_baseline_dars(
    conn,
    table: str,
    *,
    schema_version: str,
    last_source_ingestion_at: str = "",
) -> int:
    """v3.9 §25.3 — performance_baseline analyzer. Per numeric measure
    column of `table`, compute avg/min/max/stddev/p25/p75 and emit one
    DAR row per column. Returns count emitted.

    Numeric detection: information_schema.columns data_type in
    (DECIMAL/DOUBLE/INTEGER/BIGINT/FLOAT/REAL/NUMERIC/HUGEINT/SMALLINT).
    Skips columns that are all-null.
    """
    numeric_types = {
        "DECIMAL", "DOUBLE", "INTEGER", "BIGINT", "FLOAT", "REAL",
        "NUMERIC", "HUGEINT", "SMALLINT", "TINYINT", "UBIGINT",
        "UINTEGER", "USMALLINT", "UTINYINT",
    }
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    numeric_cols = [
        c for c, t in rows
        if t and (t.upper() in numeric_types or t.upper().startswith("DECIMAL"))
    ]
    if not numeric_cols:
        print("  [skip performance_baseline] no numeric columns")
        return 0

    emitted = 0
    run_id = f"performance_baseline_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{table}"
    new_dar_ids: list[str] = []

    for col in numeric_cols:
        safe_col = f'"{col}"'
        safe_table = f'raw_sap."{table}"'
        try:
            r = conn.execute(
                f"SELECT "
                f"  COUNT({safe_col}) AS n, "
                f"  MIN({safe_col}) AS mn, "
                f"  MAX({safe_col}) AS mx, "
                f"  AVG({safe_col}) AS avg_, "
                f"  STDDEV_POP({safe_col}) AS sd, "
                f"  QUANTILE_CONT({safe_col}, 0.25) AS p25, "
                f"  QUANTILE_CONT({safe_col}, 0.75) AS p75 "
                f"FROM {safe_table}"
            ).fetchone()
        except Exception as e:
            print(f"    [skip perf_baseline] {table}.{col}: {e}")
            continue

        n, mn, mx, avg_, sd, p25, p75 = r
        if not n or mn is None or mx is None:
            continue

        # DuckDB returns Decimal for some columns — cast to float for JSON
        def _num(v):
            if v is None:
                return None
            try:
                return float(v)
            except Exception:
                return None

        finding = {
            "col_name": col,
            "min": _num(mn),
            "max": _num(mx),
            "avg": _num(avg_),
            "stddev": _num(sd),
            "p25": _num(p25),
            "p75": _num(p75),
            # Stage B — non-LLM DAR type; schema uniformity per §4.3b.
            # No LLM consulted; blockers_contract_violation key intentionally
            # omitted to encode "no LLM contract applies here."
            "blockers_addressed": [],
        }

        dar_id = _next_dar_id()
        dar = {
            "id": dar_id,
            "analysis_type": "performance_baseline",
            "executed_at_utc": now_iso_utc(),
            "result_json": json.dumps(finding, separators=(",", ":"), default=str),
            "promoted": "false",
            "promoted_at_utc": "",
            "promoted_to_target_id": "",
            "run_id": run_id,
            "query_sql": f"-- performance_baseline aggregation on raw_sap.{table}.{col}",
            "row_count": "",
            "error_message": "",
            "status": "success",
            "superseded_by": "",
            "executed_by": "run_magnitude_analysis.py",
            "schema_version": schema_version,
            "source_tables": table.lower(),
            "domain_name": "baseline",
            "last_source_ingestion_at": last_source_ingestion_at,
        }
        _append_dar(dar)
        new_dar_ids.append(dar_id)
        print(f"  [ok] {dar_id} performance_baseline {table}.{col}: "
              f"min={finding['min']} max={finding['max']} avg={finding['avg']} "
              f"stddev={finding['stddev']} p25={finding['p25']} p75={finding['p75']}")
        emitted += 1
    if new_dar_ids:
        supersede_prior_dars_for_table(
            "performance_baseline", table.lower(), new_dar_ids,
        )
    if emitted:
        print(f"  performance_baseline: {emitted} DAR(s) emitted")
    return emitted


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
            source="run_magnitude_analysis",
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
