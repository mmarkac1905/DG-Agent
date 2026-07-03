"""Stage C — Prior TAR discovery for knowledge reuse.

Implements the s2t_mapping-overlap policy. Given a current
term_id, discovers candidate prior TAR rows from OTHER terms whose
scope tables overlap with the current term's scope.

No regex, no SQL text parsing. The overlap is computed via
s2t_mapping JOIN, keeping the link policy precise and auditable.

The LLM (not this loader) judges which candidates are relevant and
emits `grounded_in_tar_ids` citations in its response. The loader's
job is just to surface the candidate set.

Cap of 20 rows per call prevents unbounded bundle growth on mature
corpora. Caller can log the truncation count if it matters.

Superseded rows are INCLUDED in the candidate set. Rationale: a
superseded TAR was once-correct historical evidence. The term-analysis
bundle renders them with a `[CITATION NOTE]` staleness annotation
so the downstream LLM knows to prefer current-success
evidence where both exist.
"""
from __future__ import annotations

from typing import Optional

import duckdb


_MAX_CANDIDATES = 20


def load_candidate_prior_tars(
    conn: duckdb.DuckDBPyConnection,
    current_term_id: str,
) -> list[dict]:
    """Return candidate prior TAR query rows for knowledge reuse.

    A candidate row is a `term_analysis_results` row where:
      - row_type = 'query' (sufficiency rows are summaries; their
        underlying analytical evidence lives on the query rows).
      - term_id != current_term_id.
      - The originating term's current s2t_mapping scope has at least
        one source_table in common with the current term's scope.
      - status in ('success', 'superseded'). Superseded rows come
        through with the `superseded_flag=True` annotation so the bundle
        can render them with a freshness note.

    Returns list of dicts sorted by executed_at_utc DESC (most recent
    first). Capped at 20 rows.

    The `conn` must be an open DuckDB connection — read-only is
    sufficient. This loader never writes.
    """
    # Resolve current term's scope tables.
    current_scope = {
        r[0].lower()
        for r in conn.execute(
            "SELECT DISTINCT source_table FROM main_seeds.s2t_mapping "
            "WHERE business_term_id = ?",
            [current_term_id],
        ).fetchall()
        if r[0]
    }
    if not current_scope:
        return []

    # Candidate query: join TAR → s2t_mapping of the originating term,
    # keep rows whose origin-term scope overlaps current scope. One TAR
    # row can appear multiple times if the origin term has multiple
    # overlap tables — DISTINCT on tar.id collapses that.
    placeholders = ",".join("?" * len(current_scope))
    rows = conn.execute(
        f"""
        SELECT DISTINCT
            tar.id,
            tar.term_id,
            tar.row_type,
            tar.analysis_lens,
            tar.stage,
            tar.query_index,
            tar.query_sql,
            tar.query_result_json,
            tar.result_row_count,
            tar.interpretation,
            tar.grounded_in_tar_ids,
            tar.status,
            tar.confidence,
            tar.executed_at_utc,
            tar.run_id,
            bg.term_name AS originating_term_name
        FROM main_seeds.term_analysis_results tar
        JOIN main_seeds.business_glossary bg ON bg.id = tar.term_id
        JOIN main_seeds.s2t_mapping s2t ON s2t.business_term_id = tar.term_id
        WHERE tar.row_type = 'query'
          AND tar.term_id <> ?
          AND tar.status IN ('success', 'superseded')
          AND bg.status != 'archived'
          AND LOWER(s2t.source_table) IN ({placeholders})
        ORDER BY tar.executed_at_utc DESC
        LIMIT ?
        """,
        [current_term_id, *sorted(current_scope), _MAX_CANDIDATES],
    ).fetchall()

    # For superseded rows, optionally resolve the current-success
    # successor id so the bundle can point readers at fresh evidence.
    result: list[dict] = []
    for r in rows:
        (tar_id, term_id, row_type, analysis_lens, stage, query_index,
         query_sql, query_result_json, result_row_count, interpretation,
         grounded_in_tar_ids, status, confidence, executed_at_utc,
         run_id, originating_term_name) = r

        superseded_flag = status == "superseded"
        current_successor_id = None
        if superseded_flag:
            # Find the most recent success query row for the same
            # originating term + lens + stage (heuristic match — the
            # successor isn't formally linked; no supersede cascade).
            try:
                sr = conn.execute(
                    """
                    SELECT id FROM main_seeds.term_analysis_results
                    WHERE term_id = ?
                      AND row_type = 'query'
                      AND status = 'success'
                      AND analysis_lens = ?
                      AND stage = ?
                    ORDER BY executed_at_utc DESC
                    LIMIT 1
                    """,
                    [term_id, analysis_lens, stage],
                ).fetchone()
                if sr:
                    current_successor_id = sr[0]
            except Exception:
                pass

        # Originating term's full scope (for bundle rendering; may
        # reveal more context than just the overlap).
        try:
            origin_scope_rows = conn.execute(
                "SELECT DISTINCT source_table FROM main_seeds.s2t_mapping "
                "WHERE business_term_id = ? ORDER BY 1",
                [term_id],
            ).fetchall()
            originating_scope_tables = [r2[0] for r2 in origin_scope_rows]
        except Exception:
            originating_scope_tables = []

        result.append({
            "id": tar_id,
            "term_id": term_id,
            "row_type": row_type,
            "analysis_lens": analysis_lens,
            "stage": stage,
            "query_index": query_index,
            "query_sql": query_sql,
            "query_result_json": query_result_json,
            "result_row_count": result_row_count,
            "interpretation": interpretation,
            "grounded_in_tar_ids": grounded_in_tar_ids,
            "status": status,
            "confidence": confidence,
            "executed_at_utc": executed_at_utc,
            "run_id": run_id,
            "originating_term_name": originating_term_name,
            "originating_term_scope_tables": originating_scope_tables,
            "superseded_flag": superseded_flag,
            "current_successor_id": current_successor_id,
        })

    return result
