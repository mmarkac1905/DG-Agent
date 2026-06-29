"""CSV write safeguard — blocks catastrophic truncation of critical seeds.

Phase 12 hotfix 5. Forensics after an `s2t_mapping.csv`-wiped-to-header
incident couldn't identify the exact code path that truncated the file.
The verify harness that was the most plausible culprit has been
deleted, but we don't know if another `pd.read_csv → mutate → pd.to_csv`
pattern anywhere else can repeat the same truncation. Rather than hunt
every pandas roundtrip site, we guard at the write boundary.

Usage — call `assert_csv_safe(path, new_df)` BEFORE writing `new_df` to
`path`. Raises `RuntimeError` if the write would violate the configured
safety bounds for that seed. Non-guarded seeds pass through silently.

Rules per seed:
- `min_rows_absolute` — floor. Writing below this count is refused.
- `max_delete_per_op` — max rows a single write may remove relative
  to the current on-disk count. Set to accommodate normal operations
  (Rule 14 LLM-hallucination cleanup of 3-7 rows, rollback of 3-7
  rows) while blocking catastrophic truncation.

Design notes:
- The helper reads the existing file to compute `delta`; if the file
  is itself unreadable (`pd.errors.ParserError`), the comparison is
  skipped rather than raised — the absolute floor still applies.
- `keep_default_na=False, dtype=str` on the comparison read so that
  embedded NULL-ish values don't change the row count.
- `pd.read_csv` normally handles multi-line quoted fields correctly;
  on the rare file where it can't, the safeguard degrades gracefully
  to only the absolute floor.

Safeguarded seeds today: `s2t_mapping`. Expand `SAFEGUARDED_SEEDS`
below if another seed becomes demo-critical.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd


SAFEGUARDED_SEEDS = {
    "s2t_mapping": {
        "min_rows_absolute": 30,
        "max_delete_per_op": 10,
    },
    # Phase 15a piece 3. Floor=310 (99% of 314 columns observed at cold-start
    # completion 2026-04-19). Bumped from bootstrap value 10 once steady-state
    # row count was known — see resolved known_issue #23. max_delete=50
    # accommodates removing a mid-size raw_sap table (~40 cols) without
    # blocking. Delta check handles catastrophic truncation independently.
    "source_column_roles": {
        "min_rows_absolute": 310,
        "max_delete_per_op": 50,
    },
    # Phase 15b piece 8 §18.7 bootstrap bounds. BAR starts empty; runner is
    # analyst-invoked, so steady-state row count is unknown until post-launch
    # data lands. Floor stays at 0 until observed-p01 data is available
    # (parallel to source_column_roles' bootstrap→steady-state pattern).
    # max_delete=5 allows the §4a step 0b orphan sweep to batch-mark up to
    # 5 stale rows in a single write (typical sweep is 0-1 rows).
    "business_term_analysis_results": {
        "min_rows_absolute": 0,
        "max_delete_per_op": 5,
    },
    # Phase 15b piece 8 §22.4 (v3.6 Layer A). Seed starts empty; analyst
    # invokes compile_semantic_model.py on demand. Floor stays at 0 until
    # first full compile establishes steady-state row count (parallel to
    # source_column_roles' bootstrap pattern). max_delete=5 accommodates
    # a recompile replacing up to 5 auto_generated rows in one write
    # (typical: 1-3 rows per term scope).
    "semantic_model": {
        "min_rows_absolute": 0,
        "max_delete_per_op": 5,
    },
    # Phase 15b piece 8 §23.4 (v3.7 Layer B). Seed starts empty; populated
    # by deterministic manifest.json extraction via compile_dbt_semantic_model.py.
    # Floor stays at 0 until first full compile establishes steady-state
    # row count (~90 for current project). max_delete=10 is generous vs
    # Layer A's 5 because recompile legitimately replaces most/all rows in
    # a single write when manifest changes materially (new models, layer
    # rename, test churn). Delta check via assert_csv_safe_row_count still
    # catches catastrophic truncation (N → 0).
    "dbt_semantic_model": {
        "min_rows_absolute": 0,
        "max_delete_per_op": 10,
    },
    # 8.4.7 demo_model_expansion — ZMM_APPROVAL_LOG decoder seeds. Small
    # finite enums (SAP-style code tables). max_delete tight because the
    # enum size is known and stable.
    "zmm_approval_status": {
        "min_rows_absolute": 4,
        "max_delete_per_op": 1,
    },
    "zmm_reason_codes": {
        "min_rows_absolute": 6,
        "max_delete_per_op": 1,
    },
    # 8.5.2 catalog backfill. Floor=300 (92% of 326 rows at post-backfill
    # steady state). Bumped from pre-backfill bootstrap value 50 (57 rows)
    # now that steady-state row count is known — parallel to
    # source_column_roles' bootstrap→steady-state pattern.
    # max_delete=10 accommodates removing a small raw_sap table's cells
    # (~3-10 cols) without blocking. Delta check handles catastrophic
    # truncation independently.
    "sap_data_dictionary": {
        "min_rows_absolute": 300,
        "max_delete_per_op": 10,
    },
}


def assert_csv_safe(path: Path, new_df: pd.DataFrame) -> None:
    """Raise `RuntimeError` if writing `new_df` to `path` would violate
    that seed's configured bounds. No-op for seeds not in
    `SAFEGUARDED_SEEDS`.
    """
    name = Path(path).stem
    rules = SAFEGUARDED_SEEDS.get(name)
    if rules is None:
        return

    new_count = len(new_df)

    if new_count < rules["min_rows_absolute"]:
        raise RuntimeError(
            f"SAFEGUARD: refusing to write {new_count} rows to {name}.csv "
            f"(minimum: {rules['min_rows_absolute']}). Likely corruption. "
            f"If this is intentional, bypass by writing directly (bypass "
            f"the safeguard)."
        )

    p = Path(path)
    if not p.exists():
        return
    try:
        existing = pd.read_csv(p, keep_default_na=False, dtype=str)
    except pd.errors.ParserError:
        # Existing file unreadable — absolute floor already checked above.
        return
    except Exception as e:
        print(f"[safeguard] could not compare against existing {p}: {e}")
        return

    delta = len(existing) - new_count
    if delta > rules["max_delete_per_op"]:
        raise RuntimeError(
            f"SAFEGUARD: write to {name}.csv would delete {delta} rows "
            f"(max per op: {rules['max_delete_per_op']}). "
            f"Current: {len(existing)}, attempted: {new_count}. "
            f"Investigate before bypassing."
        )


def assert_fieldnames_cover_rows(
    fieldnames: list[str],
    rows: list[dict],
) -> None:
    """Raise RuntimeError if any row has keys not in `fieldnames`.

    Called before a csv.DictWriter write loop. Without this check,
    DictWriter opens the file in "w" mode (truncating to 0 bytes),
    writes the header, THEN raises ValueError on the first row with
    extra keys — leaving a corrupted header-only file. Same truncation
    signature as the s2t_mapping wipes seen in Phase 12 hotfix 5.

    Runs in O(n_rows * n_keys_per_row). Cheap enough to call on every
    save_csv invocation.
    """
    fn_set = set(fieldnames)
    for i, row in enumerate(rows):
        extras = set(row.keys()) - fn_set
        if extras:
            raise RuntimeError(
                f"SAFEGUARD: Row {i} has keys not in fieldnames: "
                f"{sorted(extras)}. csv.DictWriter would raise "
                f"ValueError mid-write, leaving file truncated. "
                f"Either update fieldnames to include these keys "
                f"or remove them from rows before writing."
            )


def assert_csv_safe_row_count(path: Path, new_count: int) -> None:
    """Count-based variant for writers that don't build a DataFrame
    up front (e.g., appends via csv.DictWriter). Pass the expected
    post-write row count."""
    name = Path(path).stem
    rules = SAFEGUARDED_SEEDS.get(name)
    if rules is None:
        return

    if new_count < rules["min_rows_absolute"]:
        raise RuntimeError(
            f"SAFEGUARD: post-write count {new_count} on {name}.csv "
            f"below minimum {rules['min_rows_absolute']}. Abort."
        )

    p = Path(path)
    if not p.exists():
        return
    try:
        existing = pd.read_csv(p, keep_default_na=False, dtype=str)
    except Exception:
        return

    delta = len(existing) - new_count
    if delta > rules["max_delete_per_op"]:
        raise RuntimeError(
            f"SAFEGUARD: post-write on {name}.csv would reduce count "
            f"by {delta} (max {rules['max_delete_per_op']}). "
            f"Current: {len(existing)}, attempted: {new_count}."
        )
