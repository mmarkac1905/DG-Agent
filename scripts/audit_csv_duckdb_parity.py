"""CSV ↔ DuckDB parity audit — known_issue #81 diagnostic.

For every seed CSV under dbt/seeds/*.csv, compare:
  - CSV row count (csv.DictReader)
  - DuckDB row count (main_seeds.<seed> table)

Report per-seed divergence. Exit 0 always (diagnostic, not gate).

Invoked pre-#81-fix to establish baseline (what was actually
divergent today because of the subprocess-dbt-seed lock-contention
bug) and post-fix to verify convergence.

Usage:
  python scripts/audit_csv_duckdb_parity.py
  python scripts/audit_csv_duckdb_parity.py --output <json_path>
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Optional

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
_SEED_DIR = _ROOT / "dbt" / "seeds"
_DB_PATH = _ROOT / "cpe_analytics.duckdb"


def _csv_row_count(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def _duckdb_row_count(conn, seed_name: str) -> Optional[int]:
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM main_seeds.{seed_name}"
        ).fetchone()[0]
    except Exception:
        return None


def audit() -> dict:
    conn = duckdb.connect(str(_DB_PATH), read_only=True)
    try:
        results: list[dict] = []
        for csv_path in sorted(_SEED_DIR.glob("*.csv")):
            seed = csv_path.stem
            csv_n = _csv_row_count(csv_path)
            db_n = _duckdb_row_count(conn, seed)
            status = (
                "ABSENT" if db_n is None
                else "PARITY" if csv_n == db_n
                else "DIVERGENT"
            )
            results.append({
                "seed": seed,
                "csv_rows": csv_n,
                "duckdb_rows": db_n,
                "status": status,
            })
    finally:
        conn.close()

    summary = {
        "total_seeds": len(results),
        "parity": sum(1 for r in results if r["status"] == "PARITY"),
        "divergent": sum(1 for r in results if r["status"] == "DIVERGENT"),
        "absent": sum(1 for r in results if r["status"] == "ABSENT"),
        "per_seed": results,
    }
    return summary


def _render_report(summary: dict) -> str:
    lines: list[str] = []
    lines.append(f"Total seeds scanned: {summary['total_seeds']}")
    lines.append(
        f"  PARITY:    {summary['parity']}  "
        f"DIVERGENT: {summary['divergent']}  "
        f"ABSENT: {summary['absent']}"
    )
    lines.append("")
    lines.append(
        f"  {'seed':40s}  {'csv':>10s}  {'duckdb':>10s}  {'status'}"
    )
    lines.append("  " + "-" * 74)
    for r in summary["per_seed"]:
        db_str = "(absent)" if r["duckdb_rows"] is None else str(r["duckdb_rows"])
        flag = (
            " <-- DIVERGENT" if r["status"] == "DIVERGENT"
            else " <-- ABSENT" if r["status"] == "ABSENT"
            else ""
        )
        lines.append(
            f"  {r['seed']:40s}  {r['csv_rows']:>10d}  {db_str:>10s}  "
            f"{r['status']:10s}{flag}"
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=str, default=None,
                    help="Optional path to write JSON summary")
    args = ap.parse_args(argv)

    summary = audit()
    print(_render_report(summary))

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"\nJSON summary written to: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
