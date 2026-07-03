"""Layer A DAR-completeness trigger.

Standalone CLI. Currently manual by design: no pipeline driver chains
the four EDA analyzers, so this trigger is invoked by the analyst
after the four `run_*_analysis.py` scripts have been run for the
term's scope.

Responsibilities:
1. Resolve union of raw tables in scope of selected terms (or all
   approved terms if no --term-ids filter).
2. For each table, check DAR-completeness: Completeness + Dimensions
   + Magnitude + Code Tables DARs all present in
   main_seeds.domain_analysis_results.
3. For EDA-complete tables → hand off to scripts/compile_semantic_model.py.
4. For EDA-incomplete tables → log with list of missing analyses so
   analyst knows exactly which EDA runs are still needed.

This makes Layer A compilation self-diagnostic: failures read as
"I can't compile X because Dimensions DAR is missing" rather than
mysterious behavior triggered by analyzer-order changes.

CLI:
  python scripts/trigger_layer_a_compile.py                          # all approved terms
  python scripts/trigger_layer_a_compile.py --term-ids BG001,BG027   # subset
  python scripts/trigger_layer_a_compile.py --report-only            # no compile, just report

Migration path — when a pipeline driver emerges (or one of the four EDA
analyzers becomes deterministically terminal per a build-graph contract),
move the invocation to that terminal hook point. dbt post-hook
automation is the longer-term hardening target; the current manual
CLI is the correct default while Layer A synthesis quality is being
validated on real scopes.

Exit codes:
  0 — all EDA-complete tables compiled successfully
  1 — compile step raised (LLM failure, safeguard block)
  2 — no EDA-complete tables in scope (nothing to hand off)
  3 — scope resolution yielded zero tables
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import duckdb

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "cpe_analytics.duckdb"

sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from compile_semantic_model import (  # noqa: E402
    _split_csv,
    check_dar_completeness,
    has_ontology_coverage,
    main as compile_main,
    resolve_scope_tables,
)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Trigger Layer A compilation with DAR-completeness self-diagnostic."
    )
    p.add_argument("--term-ids", type=str, default=None,
                   help="Comma-separated business term IDs (default: all approved).")
    p.add_argument("--report-only", action="store_true",
                   help="Print EDA-completeness diagnostic without invoking compiler.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    term_ids = _split_csv(args.term_ids) if args.term_ids else None

    conn = duckdb.connect(str(_DB_PATH), read_only=True)
    try:
        scope = resolve_scope_tables(conn, term_ids)
        if not scope:
            print("No scope tables resolved. Nothing to trigger.")
            return 3

        print(f"Scope tables ({len(scope)}): {sorted(scope)}")
        print()

        eda_complete: list[str] = []
        eda_incomplete: list[tuple[str, list[str]]] = []
        ontology_covered: list[str] = []

        for table in sorted(scope):
            if has_ontology_coverage(conn, table):
                ontology_covered.append(table)
                continue
            completeness = check_dar_completeness(conn, table)
            missing = [a for a, v in completeness.items() if not v["present"]]
            if missing:
                eda_incomplete.append((table, missing))
            else:
                eda_complete.append(table)

        # Diagnostic report
        print("=== DAR-completeness diagnostic ===")
        print(f"  ontology_covered ({len(ontology_covered)}): {ontology_covered}")
        print(f"  eda_incomplete ({len(eda_incomplete)}):")
        for table, missing in eda_incomplete:
            print(f"    {table} — missing: {missing}")
        print(f"  eda_complete ({len(eda_complete)}): {eda_complete}")
        print()
    finally:
        conn.close()

    if args.report_only:
        print("--report-only set; not invoking compile_semantic_model.py.")
        return 0

    if not eda_complete:
        print("No EDA-complete tables eligible for Layer A compile. "
              "Run the four EDA analyzers for the missing tables first.")
        return 2

    print(f"Handing off {len(eda_complete)} EDA-complete table(s) to "
          f"compile_semantic_model.py...")
    print()
    # Delegate via CLI arg-list — compile_main opens its own read-write conn.
    compile_argv = ["--tables", ",".join(eda_complete)]
    if term_ids:
        compile_argv += ["--term-ids", ",".join(term_ids)]
    return compile_main(compile_argv)


if __name__ == "__main__":
    sys.exit(main())
