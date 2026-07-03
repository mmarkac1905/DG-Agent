"""Shared DAR supersede helper — known_issue #73 resolution.

All 7 Source Diagnostic analyzers append rows to
`dbt/seeds/domain_analysis_results.csv`. Before this helper existed, only
`run_schema_discovery_analysis.py` implemented supersede — and only in
--mode bridges_only. Re-runs of the other 6 analyzers appended rows with
`superseded_by=''`, leaving multiple "current" DARs for the same
(analysis_type, source_tables) pair. The context assembler reads
via ORDER BY executed_at_utc DESC with no superseded_by filter, so stale
or skipped DARs could win the latest-by-timestamp race.

Contract
--------
Call AFTER appending the new DAR row(s) for an analyzer invocation with
the list of the just-written row ids. The helper rewrites the CSV
atomically, marking all prior rows matching (analysis_type,
source_tables) that are NOT in new_dar_ids as
`superseded_by=new_dar_ids[0]` and `status='superseded'`. Rows already
superseded are not re-flipped.

Order-of-operations rationale: writing new rows first, then flipping
priors, makes partial-failure safe — if the append sequence crashes
part-way, priors stay current (no dangling supersede pointers).

Multi-row per invocation (temporal_coverage emits one DAR per date
column): pass ALL new row ids. The pointer uses `new_dar_ids[0]` by
convention. The grain remains (analysis_type, source_tables) — all
priors for that pair flip together.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

_SEED_DIR = Path(__file__).resolve().parent.parent / "dbt" / "seeds"
_DAR_CSV = _SEED_DIR / "domain_analysis_results.csv"

_DAR_FIELDS: list[str] = [
    "id", "analysis_type", "executed_at_utc", "result_json",
    "promoted", "promoted_at_utc", "promoted_to_target_id",
    "run_id", "query_sql", "row_count", "error_message", "status",
    "superseded_by", "executed_by", "schema_version",
    "source_tables", "domain_name",
    "last_source_ingestion_at",
]


def supersede_prior_dars_for_table(
    analysis_type: str,
    source_tables: str,
    new_dar_ids: list[str],
) -> int:
    """Mark prior DAR rows for (analysis_type, source_tables) as superseded.

    Returns the number of rows flipped. Matches `source_tables` exactly
    (case-sensitive) — analyzers normalize to lowercase at write time, so
    callers should pass the same lowercase form. Rows already marked
    (superseded_by populated) are left untouched.
    """
    if not _DAR_CSV.exists():
        return 0
    if not new_dar_ids:
        return 0

    new_ids_set = set(new_dar_ids)
    pointer = new_dar_ids[0]

    with _DAR_CSV.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    flipped = 0
    for row in rows:
        if row.get("id") in new_ids_set:
            continue
        if row.get("analysis_type") != analysis_type:
            continue
        if row.get("source_tables") != source_tables:
            continue
        if (row.get("superseded_by") or "").strip():
            continue
        row["superseded_by"] = pointer
        row["status"] = "superseded"
        flipped += 1

    if flipped == 0:
        return 0

    tmp = _DAR_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=_DAR_FIELDS, lineterminator="\n",
            quoting=csv.QUOTE_MINIMAL,
        )
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, _DAR_CSV)
    return flipped
