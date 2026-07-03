#!/usr/bin/env python
"""One-command bootstrap for DG AI Agent.

Builds the entire local database from scratch — exactly what a new clone needs:
  1. generate the synthetic SAP source data (deterministic, seed=42)
  2. dbt deps   — install dbt package dependencies (dbt_utils)
  3. dbt seed   — load the knowledge-graph seeds
  4. dbt run    — build every layer (staging -> vault -> marts -> obt -> knowledge)
  5. dbt test   — validate

No database is committed to the repo (it's a build artifact). This script
recreates `cpe_analytics.duckdb` reproducibly. Re-runnable: it overwrites the DB.

Usage:
    python scripts/bootstrap.py            # full build
    python scripts/bootstrap.py --no-test  # skip dbt test

Requires `pip install -r requirements.txt`. The LLM pipeline (Stage A/D, EDA
analyzers) additionally needs ANTHROPIC_API_KEY in .env, but is NOT needed for
this build — the data layers are pure dbt SQL.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DBT = ROOT / "dbt"

GENERATORS = [
    ("MM  (PO, GR, inventory, equipment)", "scripts/generate_sap_sample_data.py"),
    ("ZMM (custom approval-log Z-table)", "scripts/generate_zmm_approval_log.py"),
    ("SD  (customers, sales orders, billing)", "scripts/generate_sd_billing.py"),
    ("FI  (accounting-document shadows)", "scripts/generate_fi_shadows.py"),
]


def run(label: str, cmd: list[str], cwd: Path) -> None:
    print(f"\n\033[1m==> {label}\033[0m  ({' '.join(cmd)})", flush=True)
    rc = subprocess.run(cmd, cwd=str(cwd)).returncode
    if rc != 0:
        print(f"\n\033[31mFAILED:\033[0m {label} (exit {rc})", file=sys.stderr)
        sys.exit(rc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-test", action="store_true", help="skip `dbt test`")
    args = ap.parse_args()

    if shutil.which("dbt") is None:
        print("\033[31mdbt not found on PATH.\033[0m  Activate the virtualenv first\n"
              "  (Windows: .venv\\Scripts\\activate   |   Linux/macOS: source .venv/bin/activate)\n"
              "or install dependencies: pip install -r requirements.txt",
              file=sys.stderr)
        return 1

    py = sys.executable
    for label, script in GENERATORS:
        run(f"generate {label}", [py, script], cwd=ROOT)

    # dbt runs from the dbt/ dir (profiles.yml uses a relative DuckDB path)
    run("dbt deps", ["dbt", "deps"], cwd=DBT)
    run("dbt seed", ["dbt", "seed"], cwd=DBT)
    run("dbt run", ["dbt", "run"], cwd=DBT)
    if not args.no_test:
        run("dbt test", ["dbt", "test"], cwd=DBT)

    # ASCII only: on Windows a non-UTF-8 stdout (cp1252) can't encode "✔"
    # and the resulting UnicodeEncodeError would report a successful build as exit 1.
    print("\n\033[32mBootstrap complete.\033[0m  Launch the app with: "
          "streamlit run app/Home.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
