"""Stage D.1 — canonical skipped DAR row builder.

Used by 5 analyzers (run_date, run_segmentation, run_grain_relationship,
run_magnitude, run_code_tables) when they cannot apply to the selected
table. All skipped DARs MUST go through this builder to ensure
consistent shape across analyzers.

Inconsistent skipped-DAR shapes would break:
  - Stage C prereq logic (expects status IN ('success', 'skipped')
    uniformly across analyzer types).
  - Collapsible UI renderer (_dar_render.py branches on status).
  - Layer A compile (if it reads DARs for grounding).

Timestamp format mirrors _sidecar.now_iso_utc() convention used by all
existing analyzers for consistency across success/error/skipped rows.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure scripts/ is on path for _sidecar import regardless of caller.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _sidecar import now_iso_utc  # noqa: E402


def build_skipped_dar_row(
    *,
    dar_id: str,
    analysis_type: str,
    source_tables: str,
    skip_reason: str,
    schema_version: str,
    last_source_ingestion_at: str,
    executed_by: str,
    domain_name: str = "",
    run_id: str | None = None,
) -> dict:
    """Return a DAR row dict with all 18 columns populated.

    Parameters
    ----------
    dar_id : str
        Next DAR id from the analyzer's _next_dar_id() helper.
    analysis_type : str
        One of the 8 DAR analysis_type values.
    source_tables : str
        Single table name (lowercase) OR 'table1,table2' for grain pairs
        (already sorted lex lowercase per grain_relationship convention).
    skip_reason : str
        Human-readable reason for the skip — surfaced in the collapsible UI
        and stored for future audit. Keep brief (<200 chars).
    schema_version : str
        sha256[:12] of the target table's column set, from the analyzer's
        _schema_version helper. Same as success-path DARs.
    last_source_ingestion_at : str
        Max ingestion_date from the target table, or "" if unavailable.
        Same as success-path DARs.
    executed_by : str
        e.g. "run_magnitude_analysis.py". Same as success-path.
    domain_name : str, default ""
        Analyzer-specific default (e.g., "baseline" for performance_baseline
        — though performance_baseline never skips independently, see spec).
    run_id : str, optional
        If None, generated deterministically from analysis_type + timestamp
        + source_tables, sanitized for identifier safety.

    Returns
    -------
    dict
        18-column DAR row matching the domain_analysis_results seed schema.
    """
    ts = now_iso_utc()
    # Sanitize timestamp for run_id use — strip ':' and any '+' (tz suffix).
    ts_safe = ts.replace(":", "-").replace("+", "p")
    default_run_id = f"{analysis_type}_skipped_{ts_safe}_{source_tables}"
    return {
        "id": dar_id,
        "analysis_type": analysis_type,
        "executed_at_utc": ts,
        "result_json": json.dumps({
            "skip_reason": skip_reason,
            "blockers_addressed": [],  # schema uniformity per Stage B contract
        }),
        "promoted": "false",
        "promoted_at_utc": "",
        "promoted_to_target_id": "",
        "run_id": run_id or default_run_id,
        "query_sql": f"-- skipped: {skip_reason}",
        "row_count": "0",
        "error_message": "",
        "status": "skipped",
        "superseded_by": "",
        "executed_by": executed_by,
        "schema_version": schema_version,
        "source_tables": source_tables.lower(),
        "domain_name": domain_name,
        "last_source_ingestion_at": last_source_ingestion_at,
    }
