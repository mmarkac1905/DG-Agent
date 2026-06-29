"""Piece 8.5 §27 — Verify a term has a promoted BAR.

Diagnostic helper for the 8.5 live-verification prerequisite. Promotion
is human-gated (anti-pattern #18 / RULE 22); this script only READS
state. Prints a clear status report so the analyst knows whether the
Create S2T BAR-consumer path will activate.

CLI:
  python scripts/verify_bar_promotion.py --term-id BG028

Exit codes:
  0 — promoted BAR exists for the term
  1 — no promoted BAR (generator path would run)
  2 — DuckDB error
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "cpe_analytics.duckdb"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--term-id", required=True,
                   help="Business term id (e.g. BG028)")
    args = p.parse_args(argv)

    try:
        c = duckdb.connect(str(_DB), read_only=True)
    except Exception as exc:
        print(f"ERROR: could not open DuckDB: {exc}", file=sys.stderr)
        return 2

    try:
        # All BARs for this term, newest-first
        all_bars = c.execute(
            """
            SELECT id, status, convergence_reason, confidence,
                   iterations_count, executed_at_utc, promoted_at_utc,
                   promoted_by
              FROM main_seeds.business_term_analysis_results
             WHERE business_term_id = ?
             ORDER BY executed_at_utc DESC
            """,
            [args.term_id],
        ).fetchall()
    finally:
        c.close()

    if not all_bars:
        print(f"Term {args.term_id}: NO BAR rows exist.")
        print()
        print("  Before running Piece 8.5 Create S2T, execute Piece 8 first:")
        print(f"    python scripts/run_term_injection.py --term-id {args.term_id}")
        return 1

    print(f"Term {args.term_id}: {len(all_bars)} BAR row(s)")
    print()
    for r in all_bars:
        (bar_id, status, conv, conf, iters, exec_at, promoted_at, promoted_by) = r
        star = " *PROMOTED*" if status == "promoted" else ""
        print(f"  {bar_id}: status={status}{star}")
        print(f"    convergence={conv}, confidence={conf}, iterations={iters}")
        print(f"    executed_at_utc={exec_at}")
        if status == "promoted":
            print(f"    promoted_at_utc={promoted_at}, promoted_by={promoted_by}")

    promoted = [r for r in all_bars if r[1] == "promoted"]
    print()
    if not promoted:
        print(f"RESULT: NO promoted BAR for {args.term_id}.")
        print("  Create S2T would use the generator path (existing behavior).")
        print()
        print("  To promote a BAR (human-gated per anti-pattern #18):")
        print("    Option A (Streamlit): open Business_Glossary.py, click Promote")
        print("                          on the target BAR row.")
        print("    Option B (seed edit): set status='promoted', promoted_by=<user>,")
        print("                          promoted_at_utc=<ISO UTC> in the CSV, then")
        print("                          `dbt seed --full-refresh --select business_term_analysis_results`")
        return 1

    print(f"RESULT: Term {args.term_id} HAS promoted BAR {promoted[0][0]}.")
    print("  Create S2T will use the BAR-consumer path (§27.2).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
