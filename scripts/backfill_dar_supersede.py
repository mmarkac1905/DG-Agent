"""One-shot backfill for known_issue #73 — mark duplicate-current DARs as superseded.

Run once after commit c8a16e3 to clean up DAR rows accumulated before
the supersede fix landed. For every (analysis_type, source_tables)
pair with multiple current (superseded_by empty) rows, keeps rows at
MAX(executed_at_utc) as current; flips the rest to
status='superseded', superseded_by=<any id at MAX>.

Already-superseded rows are preserved — their existing pointers stay.

Usage:
  python scripts/backfill_dar_supersede.py           # dry run (default)
  python scripts/backfill_dar_supersede.py --apply   # write CSV
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DAR_CSV = _PROJECT_ROOT / "dbt" / "seeds" / "domain_analysis_results.csv"

_DAR_FIELDS: list[str] = [
    "id", "analysis_type", "executed_at_utc", "result_json",
    "promoted", "promoted_at_utc", "promoted_to_target_id",
    "run_id", "query_sql", "row_count", "error_message", "status",
    "superseded_by", "executed_by", "schema_version",
    "source_tables", "domain_name",
    "last_source_ingestion_at",
]


def compute_flips(rows: list[dict]) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """Return (updated_rows, flip_log).

    flip_log is a list of (dar_id, analysis_type, source_tables) tuples
    for each row that will be marked superseded.
    """
    # Group current rows by (analysis_type, source_tables)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if (r.get("superseded_by") or "").strip():
            continue
        key = (r.get("analysis_type", ""), r.get("source_tables", ""))
        groups[key].append(r)

    flips: list[tuple[str, str, str]] = []
    for key, group in groups.items():
        if len(group) <= 1:
            continue
        max_ts = max((r.get("executed_at_utc") or "") for r in group)
        current_ids = [r["id"] for r in group
                       if (r.get("executed_at_utc") or "") == max_ts]
        pointer = sorted(current_ids)[0]  # deterministic pick
        for r in group:
            if (r.get("executed_at_utc") or "") == max_ts:
                continue
            r["superseded_by"] = pointer
            r["status"] = "superseded"
            flips.append((r["id"], key[0], key[1]))
    return rows, flips


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Write changes to CSV (default: dry run)")
    args = ap.parse_args(argv)

    if not _DAR_CSV.exists():
        print(f"ERROR: {_DAR_CSV} does not exist")
        return 1

    with _DAR_CSV.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"Loaded {len(rows)} DAR rows from {_DAR_CSV.name}")

    updated_rows, flips = compute_flips(rows)
    print(f"\nWill flip {len(flips)} row(s) to superseded:")
    for dar_id, at, st in flips:
        print(f"  {dar_id}  analysis_type={at}  source_tables={st}")

    if not flips:
        print("\nNothing to backfill. Exiting.")
        return 0

    if not args.apply:
        print("\nDry run. Re-run with --apply to write changes.")
        return 0

    tmp = _DAR_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_DAR_FIELDS, lineterminator="\n",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(updated_rows)
    os.replace(tmp, _DAR_CSV)
    print(f"\nWrote {len(updated_rows)} rows to {_DAR_CSV}")
    print("Next: run `dbt seed --select domain_analysis_results` to refresh DuckDB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
