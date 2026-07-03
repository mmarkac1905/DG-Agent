"""CLI entry for Create S2T (normally invoked from Streamlit).

Thin wrapper so live verification can run from the terminal.
Loads glossary metadata for the term, dispatches through the standard
create_s2t_with_implementation (which checks for a promoted BAR and
picks BAR-consumer path when present), prints the JSON result.

CLI:
  python scripts/run_create_s2t.py --term-id BG028

Exit codes:
  0 — Create S2T returned a valid result dict
  1 — error returned from Create S2T
  2 — DuckDB or import error
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "cpe_analytics.duckdb"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--term-id", required=True)
    p.add_argument("--save-model-to", default=None,
                   help="Optional path to save the generated model SQL (first "
                        "dbt_models entry only). Directory must exist.")
    args = p.parse_args(argv)

    # Load term metadata from glossary
    try:
        c = duckdb.connect(str(_DB), read_only=True)
        row = c.execute(
            "SELECT term_name, definition, unit, grain, status "
            "FROM main_seeds.business_glossary WHERE id = ?",
            [args.term_id],
        ).fetchone()
        c.close()
    except Exception as exc:
        print(f"ERROR: DuckDB: {exc}", file=sys.stderr)
        return 2
    if not row:
        print(f"ERROR: term {args.term_id} not in business_glossary")
        return 1
    term_name, term_def, term_unit, term_grain, term_status = row

    # Import Create S2T
    sys.path.insert(0, str(_ROOT / "app"))
    try:
        from claude_api import create_s2t_with_implementation  # type: ignore
    except Exception as exc:
        print(f"ERROR: import claude_api: {exc}", file=sys.stderr)
        return 2

    print(f"Invoking Create S2T for {args.term_id} '{term_name}' (status={term_status})...")
    out = create_s2t_with_implementation(
        term_name=term_name,
        term_definition=term_def or "",
        term_unit=term_unit or "",
        term_grain=term_grain or "",
        term_id=args.term_id,
    )

    # Print a compact summary + dispatched path
    print()
    if isinstance(out, dict):
        if "error" in out:
            print(f"ERROR: {out['error']}")
            return 1
        print(f"source: {out.get('source', '(unset)')}")
        print(f"bar_id: {out.get('bar_id', '(none)')}")
        print(f"confidence: {out.get('confidence', '(none)')}")
        print(f"n dbt_models: {len(out.get('dbt_models') or [])}")
        print(f"warnings: {out.get('warnings', [])}")
        if out.get("_bar_audit_issues"):
            print(f"_bar_audit_issues: {out['_bar_audit_issues']}")
        print(f"bundle_fingerprint: {out.get('_bundle_fingerprint')}")
        print(f"bundle_total_tokens: {out.get('_bundle_total_tokens')}")
        print()
        print("=== dbt_models ===")
        for i, m in enumerate(out.get("dbt_models") or [], 1):
            print(f"\n--- model {i}: {m.get('name')} ---")
            print(f"  layer: {m.get('layer')}")
            print(f"  materialization: {m.get('materialization')}")
            print(f"  description: {m.get('description')}")
            print(f"  meta: {json.dumps(m.get('meta') or {}, default=str)}")
            print(f"  tests: {m.get('tests')}")
            print(f"  sql (first 400 chars): {(m.get('sql') or '')[:400]}")

        if args.save_model_to and out.get("dbt_models"):
            tgt = Path(args.save_model_to)
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text((out["dbt_models"][0].get("sql") or ""),
                           encoding="utf-8")
            print(f"\nSaved first model's SQL to: {tgt}")
        return 0
    print(f"ERROR: unexpected return type {type(out).__name__}: {out!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
