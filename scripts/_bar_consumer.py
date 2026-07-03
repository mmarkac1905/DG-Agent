"""Promoted BAR consumer for Create S2T.

Resolves a term's promoted BAR (status='promoted', latest-executed) and
packages the fields Create S2T's BAR-consumer path needs as a typed
PromotedBarInput dataclass. Validates completeness; raises
BarConsumptionError on malformed rows so the dispatcher can fall back
to the legacy generator path with a clear warning.

BAR promotion is human-gated per anti-pattern #18 / RULE 22. This module
READS promoted BARs; it NEVER writes status='promoted'.

Public surface:
    PromotedBarInput                 — dataclass
    BarConsumptionError              — raised on malformed BAR content
    resolve_promoted_bar(conn, term_id) -> Optional[PromotedBarInput]

Invariants:
- conn parameter is optional; owned-when-None per anti-pattern #31
- Read-only (opens read_only=True when self-owned)
- Returns None (not raises) when no promoted BAR exists — dispatcher
  treats this as "use fallback generator path"
- Raises BarConsumptionError when a promoted BAR exists but its payload
  is unusable (malformed JSON, missing required field, empty SQL)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "cpe_analytics.duckdb"


class BarConsumptionError(RuntimeError):
    """Raised when a promoted BAR exists but its payload can't be
    safely consumed (malformed JSON, missing required field, empty SQL).
    Dispatcher catches and falls back to the generator path with warning.
    """


@dataclass
class PromotedBarInput:
    """Typed container for promoted BAR fields consumed by Create S2T.

    Required vs optional is enforced by resolve_promoted_bar();
    callers can trust non-None / non-empty values for the required set.
    """

    bar_id: str
    term_id: str
    # Required
    final_query_sql: str
    dbt_semantic_model_consumed: list[str] = field(default_factory=list)
    term_conditions_covered: list[str] = field(default_factory=list)
    final_metric_interpretation: str = ""
    # Optional
    semantic_model_consumed: list[str] = field(default_factory=list)
    iteration_trace: list[dict] = field(default_factory=list)
    # Context metadata (not strictly required but useful for audit)
    confidence: str = ""
    iterations_count: int = 0
    executed_at_utc: str = ""


def _parse_json_list(value: Any, field_name: str, bar_id: str) -> list:
    """Parse a VARCHAR-stored JSON list from DuckDB into a Python list.
    Empty string / None → []. Malformed JSON or non-list → raises.
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BarConsumptionError(
            f"{bar_id}.{field_name}: malformed JSON ({exc})"
        )
    if not isinstance(parsed, list):
        raise BarConsumptionError(
            f"{bar_id}.{field_name}: expected JSON list, got "
            f"{type(parsed).__name__}"
        )
    return parsed


def _parse_json_trace(value: Any, bar_id: str) -> list[dict]:
    """Parse iteration_trace (JSON array of objects). Tolerant — empty
    trace is valid (optional field) and returns [].
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        # Trace is optional; corruption here does not block consumption.
        # Caller can still build a dbt model from the SQL alone.
        return []
    if not isinstance(parsed, list):
        return []
    return parsed


def resolve_promoted_bar(
    conn: Optional[duckdb.DuckDBPyConnection],
    term_id: str,
) -> Optional[PromotedBarInput]:
    """Return the latest promoted BAR for term_id, or None if none exist.

    A BAR at `status='promoted'` is sticky. A newer
    `status='converged'` or `hard_stop` BAR does NOT replace it; only
    a subsequent explicit promotion would.

    Validates the row's required fields. Raises BarConsumptionError if
    the promoted BAR's payload is malformed. Dispatcher catches and
    falls back to the generator path with warning.
    """
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB), read_only=True)
    try:
        row = conn.execute(
            """
            SELECT id, business_term_id, final_query_sql,
                   dbt_semantic_model_consumed, semantic_model_consumed,
                   term_conditions_covered, final_metric_interpretation,
                   iteration_trace, confidence, iterations_count,
                   executed_at_utc, scope_tables
              FROM main_seeds.business_term_analysis_results
             WHERE business_term_id = ?
               AND status = 'promoted'
             ORDER BY executed_at_utc DESC
             LIMIT 1
            """,
            [term_id],
        ).fetchone()
        # Greenfield check: does ANY Layer B (dbt_semantic_model) coverage
        # exist for this BAR's scope tables? On a freshly onboarded source
        # there is none, so an empty dbt_semantic_model_consumed is the
        # expected state, not a suspect promotion.
        layer_b_covered = False
        if row is not None and row[11]:
            scope = [t.strip().lower() for t in str(row[11]).split(",") if t.strip()]
            if scope:
                placeholders = ",".join("?" for _ in scope)
                try:
                    n = conn.execute(
                        f"SELECT COUNT(*) FROM main_seeds.dbt_column_lineage "
                        f"WHERE LOWER(origin_table) IN ({placeholders})",
                        scope,
                    ).fetchone()[0]
                    layer_b_covered = bool(n)
                except duckdb.Error:
                    layer_b_covered = True  # can't verify -> keep strict contract
    finally:
        if owned:
            conn.close()

    if row is None:
        return None

    (bar_id, term, final_sql, dsm_consumed_raw, sm_consumed_raw,
     conditions_raw, metric_interp, trace_raw, confidence,
     iter_count, executed_at, _scope_tables_raw) = row

    # Required-field validation
    if not final_sql or not final_sql.strip():
        raise BarConsumptionError(
            f"{bar_id}: final_query_sql is empty — cannot consume"
        )

    dsm_consumed = _parse_json_list(
        dsm_consumed_raw, "dbt_semantic_model_consumed", bar_id
    )
    sm_consumed = _parse_json_list(
        sm_consumed_raw, "semantic_model_consumed", bar_id
    )
    conditions = _parse_json_list(
        conditions_raw, "term_conditions_covered", bar_id
    )
    trace = _parse_json_trace(trace_raw, bar_id)

    # At least one dbt_semantic_model_consumed ref target must exist
    # for a promoted BAR on an ontology-covered scope (every term-analysis
    # run there produces at least one Layer B citation). If absent while
    # coverage exists, the promotion is suspect — refuse to consume.
    # Greenfield exception: a freshly onboarded source has no Layer B
    # coverage at all, so an empty citation list is the expected state.
    if not dsm_consumed and layer_b_covered:
        raise BarConsumptionError(
            f"{bar_id}: dbt_semantic_model_consumed is empty — a promoted "
            f"BAR should reference at least one Layer B model; "
            f"refusing to consume (re-run the term analysis and re-promote)"
        )

    # Required: metric_interpretation non-empty (becomes dbt model description)
    if not metric_interp or not metric_interp.strip():
        raise BarConsumptionError(
            f"{bar_id}: final_metric_interpretation is empty — required "
            f"for dbt model description"
        )

    return PromotedBarInput(
        bar_id=str(bar_id),
        term_id=str(term),
        final_query_sql=str(final_sql),
        dbt_semantic_model_consumed=[str(x) for x in dsm_consumed],
        term_conditions_covered=[str(x) for x in conditions],
        final_metric_interpretation=str(metric_interp),
        semantic_model_consumed=[str(x) for x in sm_consumed],
        iteration_trace=trace,
        confidence=str(confidence or ""),
        iterations_count=int(iter_count or 0),
        executed_at_utc=str(executed_at or ""),
    )


def resolve_latest_bar(
    conn: Optional[duckdb.DuckDBPyConnection],
    term_id: str,
) -> Optional[dict]:
    """Return the latest *finished* BAR row for term_id (any status), or None.

    Distinct from `resolve_promoted_bar` which filters status='promoted'.
    Used by Stage E's dispatcher to detect needs_data_extension /
    hard_stop verdicts before falling through to the generator path.

    Filters:
      - finished_at_utc IS NOT NULL  (excludes in-progress runs)
      - superseded_by IS NULL/empty  (excludes obsolete rows)

    Raises BarConsumptionError on malformed JSON in
    `iteration_trace` or `sourcing_recommendations`. Mirrors
    `resolve_promoted_bar`'s error-on-malformed contract so the
    dispatcher's exception handling stays uniform across resolvers.
    """
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB), read_only=True)
    try:
        row = conn.execute(
            """
            SELECT id, business_term_id, status, convergence_reason,
                   sourcing_recommendations, iteration_trace,
                   bridge_coverage_consulted, finished_at_utc
              FROM main_seeds.business_term_analysis_results
             WHERE business_term_id = ?
               AND finished_at_utc IS NOT NULL
               AND (superseded_by IS NULL OR superseded_by = '')
             ORDER BY finished_at_utc DESC
             LIMIT 1
            """,
            [term_id],
        ).fetchone()
    finally:
        if owned:
            conn.close()

    if row is None:
        return None

    (bar_id, term, status, conv_reason, sr_raw, trace_raw,
     bcc_raw, finished_at) = row

    try:
        sr_parsed = json.loads(sr_raw) if sr_raw else None
    except json.JSONDecodeError as exc:
        raise BarConsumptionError(
            f"{bar_id}.sourcing_recommendations: malformed JSON ({exc})"
        )
    try:
        trace_parsed = json.loads(trace_raw) if trace_raw else []
    except json.JSONDecodeError as exc:
        raise BarConsumptionError(
            f"{bar_id}.iteration_trace: malformed JSON ({exc})"
        )

    return {
        "id": str(bar_id),
        "business_term_id": str(term),
        "status": str(status or ""),
        "convergence_reason": str(conv_reason or ""),
        "sourcing_recommendations": sr_parsed,
        "iteration_trace": trace_parsed if isinstance(trace_parsed, list) else [],
        "bridge_coverage_consulted_raw": bcc_raw,
        "finished_at_utc": str(finished_at or ""),
    }


def summarize_iteration_trace(trace: list[dict]) -> dict:
    """Reduce iteration_trace to a compact audit-pointer dict for the
    generated model's YAML meta block. Keeps file size sane; full trace
    stays in the BAR seed.
    """
    if not trace:
        return {"iterations": 0}
    last = trace[-1] if trace else {}
    gates = last.get("gates_result") or {}
    return {
        "iterations": len(trace),
        "last_convergence": {
            "compile": gates.get("compile"),
            "run": gates.get("run"),
            "semantic_alignment": gates.get("semantic_alignment"),
        },
    }
