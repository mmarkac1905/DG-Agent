"""Piece 8 BAR row writer — Phase 15b §8c.

Handles writes and updates to main_seeds.business_term_analysis_results
across both the CSV (dbt seed source of truth) and DuckDB (runtime
query layer). Maintains CSV-DuckDB consistency: every operation writes
to both surfaces so a crash between calls leaves a recoverable state.

Contract with piece 8 §4a mainline flow:
- write_bar_row          → step 1 placeholder write, step 14 recovery sibling
- update_bar_row         → step 14 conditional UPDATE (with only_if_status guard)
- sweep_orphaned_inprogress → step 0b preflight TTL sweep

Invariants enforced at the write boundary:
- LF line endings in CSV (RULE 34 / anti-pattern #48-50)
- csv.DictWriter fieldnames pre-validated (anti-pattern #57)
- Row count safeguard via assert_csv_safe_row_count (anti-pattern #56)
- Atomic CSV overwrite via temp file + os.replace (crash-safe)
- Timestamp serialization via strftime ISO8601 (RULE 36 / anti-pattern #54)

Scope limitation for sub-piece 8.1: rewrites the full CSV on every
UPDATE/sweep. O(N) in BAR row count. Acceptable at piece 8's scale
(~1 row per analyst-invoked run). Revisit if BAR grows past ~5000 rows.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal, Optional

import duckdb

# Make app/ importable so we can reuse the csv-safeguard boundary.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "app"))
from _csv_safeguard import (  # noqa: E402
    assert_csv_safe_row_count,
    assert_fieldnames_cover_rows,
)


# Column order locked to match dbt/seeds/business_term_analysis_results.csv
# header and dbt/seeds/schema.yml column_types declaration.
# Changes here must be mirrored in BOTH files (and in piece 8 §3a).
BAR_COLUMNS: list[str] = [
    "id",
    "business_term_id",
    "status",
    "analysis_type",
    "executed_at_utc",
    "inprogress_since_utc",
    "finished_at_utc",
    "scope_tables",
    "bundle_fingerprint",
    "iterations_count",
    "convergence_reason",
    "final_query_sql",
    "final_metric_value",
    "final_metric_unit",
    "final_metric_interpretation",
    "term_conditions_covered",
    "term_conditions_missed",
    "confidence",
    "confidence_rationale",
    "analyst_review_needed",
    "analyst_review_reason",
    "promoted_at_utc",
    "promoted_by",
    "superseded_by",
    "last_source_ingestion_at",
    "iteration_trace",
    "bundle_token_count",
    "llm_total_input_tokens",
    "llm_total_output_tokens",
    "llm_total_cost_usd",
    "ontology_consumed",
    "domain_facts_consumed",
    "analysis_findings_consumed",
    "dar_consumed",
    "prior_bar_consumed",
    "record_source",
    "load_date",
    # v3.8 §24 (8.4.5) — BAR schema extension closing the 8.4.3/8.4.4
    # attestation migration gap. ATTESTATION_FIELDS grew in 8.4.3
    # (+semantic_model_consumed) and 8.4.4 (+dbt_semantic_model_consumed)
    # but BAR persistence columns were NOT extended, causing silent
    # attestation data loss on every run. Appended at the end of the
    # column list (migration-safe: existing CSV rows only need ",[],[]"
    # appended, no mid-row reshuffle).
    "semantic_model_consumed",
    "dbt_semantic_model_consumed",
    # Phase 2b (C5 sourcing recommendations) — populated only when C5
    # fires (consecutive scope_sanity=no → hard_stop_scope_mismatch).
    # NULL on every other run. sourcing_recommendations stores the full
    # validate_recommendations() output (validated list + summary +
    # catalog_gaps) as JSON.
    "sourcing_recommendations",
    "c5_input_tokens",
    "c5_output_tokens",
    "c5_cost_usd",
    "c5_skipped_reason",
    # Option B Phase 3 (OQ-3a) — bridge_coverage_consulted attestation.
    # Captures the DAR-NNNNN ids the iteration LLM consulted from
    # bridge_coverage_by_filter rows. Always emitted by the LLM (empty
    # list when scope has no DARs); validated conditionally by the
    # iteration-attestation gate (hard_stop_bridge_attestation_missing
    # fires when DARs exist but list is empty/missing).
    "bridge_coverage_consulted",
    # C3 (Theme 1 sub-items 1+2) — TAR-NNNNN citation discipline.
    # Captures the TAR-NNNNN ids the iteration LLM consulted from the
    # TERM EDA section (both row_type='query' and row_type='sufficiency';
    # both current-term and cross-term prior TARs from
    # _tar_corpus_loader). Always-emit (no conditional gate; per
    # OQ-C3-2 bridge_coverage's empirical-refutation rationale doesn't
    # transfer).
    "tars_consulted",
    # C4 (Theme 1 sub-item 5) — Stage A blocker citation discipline.
    # Captures the Stage A blocker IDs (iter{N}.b{I} format) the
    # iteration LLM consulted from the "## Stage A blockers" bundle
    # section. Always-emit (no conditional gate; mirrors C3).
    "stage_a_blockers_consumed",
]

BAR_CSV_PATH = _PROJECT_ROOT / "dbt" / "seeds" / "business_term_analysis_results.csv"
BAR_TABLE_FQN = "main_seeds.business_term_analysis_results"

BARStatus = Literal[
    "in_progress", "converged", "hard_stop", "failed", "promoted", "superseded",
    # Phase 2b: terminal status when C5 produces at least one usable
    # sourcing recommendation (grade in {verified, verified_low_priority,
    # divergence_warning}). Convergence_reason stays "hard_stop_scope_mismatch".
    "needs_data_extension",
]

_BAR_ID_RE = re.compile(r"^BAR-(\d{5,})$")


# ─── Helpers ──────────────────────────────────────────────────────────


def _iso(value: Any) -> str:
    """Serialize a timestamp for CSV/DuckDB. RULE 36 / anti-pattern #54 —
    normalize pandas.Timestamp and datetime.datetime via strftime so the
    string form is stable across versions. Returns '' for None."""
    if value is None or value == "":
        return ""
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _serialize_field(name: str, value: Any) -> str:
    """Normalize a field value for CSV write. NULL → empty; bools → lowercase;
    timestamps → ISO; lists/dicts → JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"), default=str)
    if isinstance(value, (dt.datetime, dt.date)):
        return _iso(value)
    if hasattr(value, "to_pydatetime"):
        return _iso(value)
    return str(value)


def _row_to_csv_dict(row: dict[str, Any]) -> dict[str, str]:
    """Project a kwargs-style dict to the canonical CSV column order, with
    missing columns filled by empty string."""
    return {col: _serialize_field(col, row.get(col)) for col in BAR_COLUMNS}


def _next_bar_id(conn: duckdb.DuckDBPyConnection) -> str:
    """Compute next BAR id from DuckDB's existing max. Format BAR-NNNNN."""
    result = conn.execute(
        f"SELECT id FROM {BAR_TABLE_FQN} WHERE id LIKE 'BAR-%'"
    ).fetchall()
    max_n = 0
    for (existing_id,) in result:
        m = _BAR_ID_RE.match(existing_id or "")
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return f"BAR-{max_n + 1:05d}"


def _export_csv_from_duckdb(conn: duckdb.DuckDBPyConnection) -> None:
    """Re-export the full BAR table from DuckDB to CSV, preserving the
    canonical column order. Atomic via temp file + os.replace.
    Runs fieldnames + row-count safeguards before overwriting.
    """
    rows = conn.execute(
        f"SELECT {', '.join(BAR_COLUMNS)} FROM {BAR_TABLE_FQN} ORDER BY id"
    ).fetchall()

    dict_rows = [
        _row_to_csv_dict(dict(zip(BAR_COLUMNS, row))) for row in rows
    ]

    # Anti-pattern #57 pre-validation (mandatory before opening for write).
    assert_fieldnames_cover_rows(BAR_COLUMNS, dict_rows)

    # Anti-pattern #56 row-count guard (BAR registered in SAFEGUARDED_SEEDS).
    assert_csv_safe_row_count(BAR_CSV_PATH, len(dict_rows))

    # Atomic write: temp file then os.replace.
    # newline="" + explicit \n keeps LF line endings (RULE 34).
    tmp_path = BAR_CSV_PATH.with_suffix(".csv.tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=BAR_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for dict_row in dict_rows:
            writer.writerow(dict_row)
    os.replace(tmp_path, BAR_CSV_PATH)


# ─── Public API ───────────────────────────────────────────────────────


def write_bar_row(
    conn: duckdb.DuckDBPyConnection,
    *,
    business_term_id: str,
    status: BARStatus = "in_progress",
    bar_id: Optional[str] = None,
    **fields: Any,
) -> str:
    """Append a new BAR row and return its id.

    Used at §4a step 1 (placeholder write) and step 14 recovery branch
    (sibling row when sweep-race detected). Defaults status=in_progress
    for placeholder use; callers writing terminal-state rows (recovery
    sibling) pass status explicitly.

    If bar_id is omitted, generates BAR-NNNNN from DuckDB max. Caller
    can pass bar_id to reserve a specific id (e.g. reproducible test
    fixtures).
    """
    if bar_id is None:
        bar_id = _next_bar_id(conn)

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    row = {
        "id": bar_id,
        "business_term_id": business_term_id,
        "status": status,
        "analysis_type": fields.pop("analysis_type", "pre_s2t_reasoning"),
        "executed_at_utc": fields.pop("executed_at_utc", now),
        "inprogress_since_utc": fields.pop(
            "inprogress_since_utc", now if status == "in_progress" else None
        ),
        "record_source": fields.pop("record_source", "piece8_term_injection"),
        "load_date": fields.pop("load_date", now),
        **fields,
    }

    # INSERT into DuckDB with all 37 columns (NULLs for omitted fields).
    col_list = ", ".join(BAR_COLUMNS)
    placeholders = ", ".join(["?"] * len(BAR_COLUMNS))
    values = [row.get(col) for col in BAR_COLUMNS]
    conn.execute(
        f"INSERT INTO {BAR_TABLE_FQN} ({col_list}) VALUES ({placeholders})",
        values,
    )

    _export_csv_from_duckdb(conn)
    return bar_id


def update_bar_row(
    conn: duckdb.DuckDBPyConnection,
    *,
    bar_id: str,
    only_if_status: Optional[str] = None,
    **updates: Any,
) -> int:
    """Update an existing BAR row. Returns affected row count.

    Piece 8 §4a step 14 conditional UPDATE: pass only_if_status='in_progress'
    to guard against the sweep-race case (§5d). If affected_rows==0, the
    caller writes a sibling recovery row via write_bar_row instead.

    Fields not in BAR_COLUMNS raise ValueError (caller programmer error).
    """
    unknown = set(updates) - set(BAR_COLUMNS)
    if unknown:
        raise ValueError(
            f"update_bar_row: unknown columns {sorted(unknown)}; "
            f"valid columns are {BAR_COLUMNS}"
        )

    if not updates:
        return 0

    set_clauses = ", ".join(f"{col} = ?" for col in updates)
    values = [updates[col] for col in updates]
    sql = f"UPDATE {BAR_TABLE_FQN} SET {set_clauses} WHERE id = ?"
    values.append(bar_id)
    if only_if_status is not None:
        sql += " AND status = ?"
        values.append(only_if_status)

    cursor = conn.execute(sql, values)
    # DuckDB Python binding: fetchall on RETURNING not used here; instead
    # use the connection's rows-affected via a re-query. DuckDB's
    # .execute(...).rowcount is unreliable across versions, so verify by
    # selecting the affected row's current state.
    if only_if_status is not None:
        check = conn.execute(
            f"SELECT COUNT(*) FROM {BAR_TABLE_FQN} WHERE id = ? AND status != ?",
            [bar_id, only_if_status],
        ).fetchone()[0]
        affected = 1 if check else 0
    else:
        check = conn.execute(
            f"SELECT COUNT(*) FROM {BAR_TABLE_FQN} WHERE id = ?",
            [bar_id],
        ).fetchone()[0]
        affected = check

    if affected > 0:
        _export_csv_from_duckdb(conn)
    return affected


def sweep_orphaned_inprogress(
    conn: duckdb.DuckDBPyConnection,
    *,
    term_id: str,
    ttl_hours: int,
) -> list[str]:
    """Sweep stale in_progress BAR rows for one term (§4a step 0b).

    Returns list of swept BAR ids. Each swept row transitions
    in_progress → failed with convergence_reason='hard_stop_orphaned_inprogress'.
    Machine transition per §5d (operational lifecycle, not governance —
    does not violate anti-pattern #18).
    """
    cutoff = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=ttl_hours)

    stale = conn.execute(
        f"""
        SELECT id
        FROM {BAR_TABLE_FQN}
        WHERE business_term_id = ?
          AND status = 'in_progress'
          AND inprogress_since_utc < ?
        ORDER BY id
        """,
        [term_id, cutoff],
    ).fetchall()

    candidate_ids = [row[0] for row in stale]
    if not candidate_ids:
        return []

    # v3.5 Prereq C: conditional UPDATE with WHERE status='in_progress' guard.
    # Race window exists between the SELECT above and the UPDATE below —
    # a slow-legitimate runner could transition its own placeholder to
    # 'converged' between those two steps. Without the status guard, this
    # sweep would overwrite the legitimate terminal state. RULE 39
    # (rollback-by-ID not position) + symmetry with update_bar_row pattern.
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    swept_ids: list[str] = []
    for candidate_id in candidate_ids:
        # Pre/post-COUNT pattern (same as update_bar_row — DuckDB .rowcount
        # unreliable across versions). Verify row is still in_progress AFTER
        # the UPDATE; if it flipped during the sweep, the WHERE status guard
        # caused the UPDATE to be a no-op and the row stays at whatever
        # terminal state its legitimate runner wrote.
        conn.execute(
            f"""
            UPDATE {BAR_TABLE_FQN}
               SET status = 'failed',
                   convergence_reason = 'hard_stop_orphaned_inprogress',
                   confidence = 'failed',
                   confidence_rationale = 'Runner did not terminate within TTL, presumed crashed',
                   analyst_review_needed = true,
                   analyst_review_reason = 'Orphaned in_progress row swept after TTL expiry',
                   finished_at_utc = ?,
                   inprogress_since_utc = NULL
             WHERE id = ?
               AND status = 'in_progress'
            """,
            [now, candidate_id],
        )
        # Verify: row still reports as orphan-swept? If yes, we transitioned it.
        # If no (status changed to something other than 'failed' with our
        # orphan reason), runner claimed the row first; skip.
        confirm = conn.execute(
            f"""
            SELECT status, convergence_reason
            FROM {BAR_TABLE_FQN}
            WHERE id = ?
            """,
            [candidate_id],
        ).fetchone()
        if (confirm is not None
                and confirm[0] == 'failed'
                and confirm[1] == 'hard_stop_orphaned_inprogress'):
            swept_ids.append(candidate_id)
        # else: runner reclaimed the row mid-sweep; legitimate transition wins.

    if swept_ids:
        _export_csv_from_duckdb(conn)
    return swept_ids
