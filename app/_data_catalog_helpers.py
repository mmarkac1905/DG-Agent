"""Helpers for app/pages/Data_Catalog.py — pure functions extracted for testability."""
from __future__ import annotations

import re
from typing import Any, Optional


def resolve_table_detail_default_index(
    cached_table: Optional[str],
    ingested_names: list[str],
) -> int:
    """Return the default index= for the Table Detail selectbox.

    Returns the position of `cached_table` in `ingested_names` if present,
    else 0. Handles stale cache (cached value no longer in the ingested
    set — e.g. un-ingested, removed, filtered) by falling back to 0 so
    the selectbox doesn't error on an out-of-range index.
    """
    if cached_table and cached_table in ingested_names:
        return ingested_names.index(cached_table)
    return 0


def has_semantic_model_row(conn: Any, table: str) -> bool:
    """True when main_seeds.semantic_model already has a Layer A row
    for `table` (case-insensitive match on table_name).

    Data_Catalog.py's Layer A panel uses this to branch between
    "row exists → render" and "no row → run compile" empty state.
    Replaces the former ontology_covers_table helper (retired as
    part of known_issue #79: the consumer-priority discipline
    was removed because Layer A and ontology are non-overlapping
    context layers, both consumed independently by the
    term-analysis (BAR) runner).

    Fails open (returns False) on query error so a DuckDB hiccup
    shows the "run compile" empty state rather than masking an
    absent row as present.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM main_seeds.semantic_model "
            "WHERE LOWER(table_name) = LOWER(?)",
            [table],
        ).fetchone()
        return bool(row) and (row[0] or 0) > 0
    except Exception:
        return False


def parse_compile_skip_reason(stdout: str, table: str) -> Optional[str]:
    """Extract the skip reason from a compile_semantic_model.py run.

    compile_semantic_model.py emits skip messages in the form
    `  skip {table} — {reason}`. Two variants remain post-#79:
      - "human_override / human_reviewed; preserved" (line 651)
      - "EDA incomplete; missing: [...]" (line 663)
    The prior "ontology coverage exists in dbt_column_lineage" message
    was removed with #79 — that skip gate no longer fires, ontology-
    covered tables are now compiled.

    Returns the reason string if a matching line is found; None
    otherwise. Used by Data_Catalog.py's compile handler to replace
    the misleading "Semantic model compiled" toast with an honest
    "Skipped: <reason>" message when rc=0 but no row was written
    (known_issue #78).
    """
    if not stdout:
        return None
    pattern = rf"  skip {re.escape(table)} — (.+)"
    m = re.search(pattern, stdout)
    if m:
        return m.group(1).strip()
    return None


def format_batch_error_expander(
    table: str, errors: list[dict],
) -> Optional[tuple[str, str]]:
    """Format per-table batch errors for an st.expander.

    Returns (expander_label, body_markdown). Returns None if errors is
    empty. known_issue #75 fix — Source Diagnostic batch dispatch
    previously counted but never surfaced per-analyzer stderr; this
    helper produces the content the caller renders in st.expander.

    Each error dict must carry keys: 'label' (analyzer name),
    'returncode' (int), and 'stderr' (str, may be empty).
    """
    if not errors:
        return None
    suffix = "s" if len(errors) != 1 else ""
    label = (
        f"`{table}`: {len(errors)} analyzer error{suffix} "
        f"— click to view stderr"
    )
    body_parts: list[str] = []
    for e in errors:
        stderr = (e.get("stderr") or "").strip() or "(empty stderr)"
        body_parts.append(
            f"**{e['label']}** (rc={e['returncode']})\n\n"
            f"```\n{stderr}\n```"
        )
    return label, "\n\n---\n\n".join(body_parts)
