"""Run a dbt command and refresh the Parquet export.

Use this instead of calling `dbt` directly whenever you mutate the
database — it guarantees the Streamlit dashboard sees the change
without needing a restart.

Usage:
    python scripts/run_pipeline.py run
    python scripts/run_pipeline.py seed --full-refresh
    python scripts/run_pipeline.py seed --select business_glossary --full-refresh
    python scripts/run_pipeline.py test
"""
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DBT_DIR = ROOT / "dbt"
VENV_DIR = ROOT / ".venv"


def _dbt_executable() -> str:
    if platform.system() == "Windows":
        cand = VENV_DIR / "Scripts" / "dbt.exe"
    else:
        cand = VENV_DIR / "bin" / "dbt"
    return str(cand) if cand.exists() else "dbt"


def run():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    extra_args = sys.argv[2:]

    print(f"▶ dbt {cmd} {' '.join(extra_args)}".strip())
    result = subprocess.run(
        [_dbt_executable(), cmd, *extra_args],
        cwd=str(DBT_DIR),
    )

    if result.returncode != 0:
        print(f"\n✗ dbt {cmd} failed (exit {result.returncode}) — skipping Parquet export")
        sys.exit(result.returncode)

    print("\n▶ Exporting to Parquet")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from export_parquet import export_all
    export_all()


if __name__ == "__main__":
    run()
