"""Stage D.1 — central term status filtering helper.

Replaces copy-pasted `_active_glossary` patterns across the Streamlit
pages. Six call sites identified in the pre-deletion audit now route
through `filter_active_terms` for a single source-of-truth predicate.

Archive-is-final (decision #67): rows with status='archived' are
excluded from every selector, LLM prompt context pass, and new-term
name-uniqueness check. Write-path guards in Business_Glossary.py
retain their direct `== 'archived'` checks because they surface
operation-specific error messages; see spec 1.3 "Out of scope."

Import path per Streamlit convention: `from _term_status_utils import
filter_active_terms`. The `app/` directory is on `sys.path` via
Streamlit's page discovery — it is NOT a package, so `from app._term_
status_utils` will fail at import time.
"""
from __future__ import annotations

import pandas as pd


def filter_active_terms(glossary_df):
    """Return glossary rows where status != 'archived'.

    A missing or empty `status` field is treated as active (the row
    passes through). This preserves the existing hand-rolled pattern's
    behavior on rows that predate the archive-status enum.
    An empty DataFrame returns unchanged (shortcut avoids constructing
    a throwaway Series when nothing is loaded yet).

    Parameters
    ----------
    glossary_df : pandas.DataFrame
        Must contain an 'id' column. The 'status' column is optional;
        missing defaults to 'active'.

    Returns
    -------
    pandas.DataFrame
        Filtered copy — index preserved.
    """
    if len(glossary_df) == 0:
        return glossary_df
    status_col = glossary_df.get(
        "status",
        pd.Series(["active"] * len(glossary_df)),
    ).astype(str)
    return glossary_df[status_col != "archived"]
