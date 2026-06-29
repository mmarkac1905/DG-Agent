"""Test for scripts/backfill_dar_supersede.compute_flips — #73 backfill.

Covers the grain-correct backfill contract: rows at MAX(executed_at_utc)
per (analysis_type, source_tables) stay current; earlier rows are
flipped to superseded_by pointing at a current-group id. Already-
superseded rows are preserved.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import backfill_dar_supersede as mod  # noqa: E402


def test_backfill_flips_older_rows_and_preserves_existing_supersede():
    rows = [
        # Duplicate-current for (completeness, equi): two runs, both NULL.
        {"id": "DAR-01", "analysis_type": "completeness", "source_tables": "equi",
         "executed_at_utc": "2026-04-22 22:38:13", "status": "success",
         "superseded_by": ""},
        {"id": "DAR-02", "analysis_type": "completeness", "source_tables": "equi",
         "executed_at_utc": "2026-04-24 00:58:05", "status": "success",
         "superseded_by": ""},
        # temporal_coverage EQUI: 2+2 identical-timestamp pairs per run —
        # backfill must flip the earlier-timestamp PAIR, keep both current-
        # timestamp rows as current (no self-supersede within the group).
        {"id": "DAR-10", "analysis_type": "temporal_coverage", "source_tables": "equi",
         "executed_at_utc": "2026-04-22 22:46:51.564083", "status": "success",
         "superseded_by": ""},
        {"id": "DAR-11", "analysis_type": "temporal_coverage", "source_tables": "equi",
         "executed_at_utc": "2026-04-22 22:46:51.564083", "status": "success",
         "superseded_by": ""},
        {"id": "DAR-12", "analysis_type": "temporal_coverage", "source_tables": "equi",
         "executed_at_utc": "2026-04-24 01:00:08.962400", "status": "success",
         "superseded_by": ""},
        {"id": "DAR-13", "analysis_type": "temporal_coverage", "source_tables": "equi",
         "executed_at_utc": "2026-04-24 01:00:08.962400", "status": "success",
         "superseded_by": ""},
        # Already-superseded row — must be preserved untouched.
        {"id": "DAR-20", "analysis_type": "dimensions", "source_tables": "ekko",
         "executed_at_utc": "2026-04-20 10:00:00", "status": "superseded",
         "superseded_by": "DAR-21"},
        {"id": "DAR-21", "analysis_type": "dimensions", "source_tables": "ekko",
         "executed_at_utc": "2026-04-22 10:00:00", "status": "success",
         "superseded_by": ""},
        # Single current row — no flip needed.
        {"id": "DAR-30", "analysis_type": "code_tables", "source_tables": "mara",
         "executed_at_utc": "2026-04-24 00:00:00", "status": "success",
         "superseded_by": ""},
    ]

    updated, flips = mod.compute_flips(rows)

    # 1 completeness + 2 temporal_coverage rows should flip.
    flipped_ids = {f[0] for f in flips}
    assert flipped_ids == {"DAR-01", "DAR-10", "DAR-11"}

    by_id = {r["id"]: r for r in updated}
    # Completeness: DAR-01 flipped to point at DAR-02.
    assert by_id["DAR-01"]["superseded_by"] == "DAR-02"
    assert by_id["DAR-01"]["status"] == "superseded"
    assert by_id["DAR-02"]["superseded_by"] == ""

    # Temporal: both earlier-timestamp rows point at same current-group id
    # (smallest id at MAX, deterministic). DAR-12 and DAR-13 stay current.
    assert by_id["DAR-10"]["superseded_by"] == "DAR-12"
    assert by_id["DAR-11"]["superseded_by"] == "DAR-12"
    assert by_id["DAR-12"]["superseded_by"] == ""
    assert by_id["DAR-13"]["superseded_by"] == ""

    # Already-superseded preserved: pointer unchanged.
    assert by_id["DAR-20"]["superseded_by"] == "DAR-21"
    assert by_id["DAR-20"]["status"] == "superseded"

    # Single-current row untouched.
    assert by_id["DAR-30"]["superseded_by"] == ""
