"""In-process sync helpers for Streamlit pages.

Every write from the UI goes first to a seed CSV on disk (source of
truth for the dbt pipeline) and then directly into the live DuckDB
database via `save_and_sync` — NEVER via a `dbt seed` subprocess.

Subprocess-based syncs were unreliable: on Windows, even a read-only
DuckDB connection held by Streamlit could block a dbt writer process
from another PID, and the failure modes were confusing to users.
Direct in-process writes are faster, avoid the lock race entirely, and
let the UI show the updated data on the very next rerun.

These helpers render only user-friendly feedback — they never surface a
terminal command. They keep the same public API as the old subprocess
helpers so existing call sites work unchanged.
"""
from pathlib import Path
from typing import Iterable, Optional, Union

import streamlit as st

from db import save_and_sync, validate_test_sql

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = PROJECT_ROOT / "dbt" / "seeds"


def _normalise(seed_names: Union[str, Iterable[str], None]) -> list:
    if seed_names is None:
        return []
    if isinstance(seed_names, str):
        return [seed_names]
    return list(seed_names)


def sync_seed(
    seed_names: Union[str, Iterable[str], None],
    *,
    success_msg: Optional[str] = None,
    spinner_msg: str = "Saving changes to the database...",
):
    """Push one or more seed CSVs into DuckDB as
    `main_seeds.<name>` tables, then rerun the page so the UI
    immediately picks up the fresh rows.
    """
    names = _normalise(seed_names)
    if not names:
        st.warning("No seed to sync.")
        return

    failures = []
    loaded = []
    with st.spinner(spinner_msg):
        for name in names:
            csv_path = SEED_DIR / f"{name}.csv"
            ok, detail = save_and_sync(csv_path, name)
            if ok:
                loaded.append(f"{name} ({detail})")
            else:
                failures.append(f"{name}: {detail}")

    if not failures:
        st.success(success_msg or "✅ Changes saved and synced to the database.")
        if loaded:
            st.caption("Synced: " + ", ".join(loaded))
        st.rerun()
    else:
        st.warning(
            "⚠️ Changes saved to file, but the live database sync hit an issue. "
            "They'll appear after the next full pipeline run."
        )
        with st.expander("Technical details"):
            st.code("\n".join(failures))


def sync_models(
    *,
    success_msg: Optional[str] = None,
    **_ignored,
):
    """Acknowledgement helper for the Deploy-dbt-Models button.

    We deliberately do NOT run `dbt run` from within the app — model
    compilation uses jinja macros and can take minutes. Instead we
    surface a user-friendly confirmation and let the engineering team's
    scheduled pipeline pick up the new files on its next run.
    """
    st.success(
        success_msg
        or "✅ Models saved. Your data engineering team will build them on the next pipeline run."
    )


def sync_tests(
    test_selector: Optional[str] = None,
    *,
    success_msg: Optional[str] = None,
    spinner_msg: str = "Running the new data quality check...",
):
    """Validate a newly-saved dbt singular test by executing it directly
    against DuckDB — never via a `dbt test` subprocess.

    This is only used by the Data Quality tab: the rule SQL is already
    written to `dbt/tests/<name>.sql` before we get here, so the on-disk
    artefact survives for the real `dbt test` run that happens in the
    scheduled pipeline. Here we just execute the SQL body to tell the
    user whether the rule currently passes against the live data.
    """
    if not test_selector:
        st.success(success_msg or "✅ Data quality check saved.")
        return

    tests_dir = PROJECT_ROOT / "dbt" / "tests"
    test_file = tests_dir / f"{test_selector}.sql"
    if not test_file.exists():
        st.success(success_msg or "✅ Data quality check saved.")
        return

    try:
        content = test_file.read_text(encoding="utf-8")
    except Exception as e:
        st.warning(f"⚠️ Saved but could not read the test file: {e}")
        return

    # Strip comment-only provenance header so it doesn't get wrapped
    sql_lines = [
        ln for ln in content.splitlines() if not ln.strip().startswith("--")
    ]
    sql_body = "\n".join(sql_lines).strip()

    with st.spinner(spinner_msg):
        ok, violations, detail = validate_test_sql(sql_body)

    if not ok:
        st.warning(
            "⚠️ Rule saved, but it could not be executed against the live data. "
            "Your data engineering team will pick this up on the next pipeline run."
        )
        with st.expander("Technical details"):
            st.code(detail)
        return

    if violations == 0:
        st.success(
            success_msg
            or f"✅ Rule `{test_selector}` is active and passing — no violations found."
        )
    else:
        st.info(
            f"Rule `{test_selector}` saved and is now active. "
            f"It currently catches **{violations:,}** violating row"
            f"{'' if violations == 1 else 's'} — review these with your data team."
        )
