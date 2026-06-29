"""Phase 12 — LLM context loader for same-name S2T re-attempts.

`load_archived_term_context(term_name)` reads `main_seeds.archive_log`
through the normal `db.query()` path, filters on:

    term_name == <input> AND learning_signal == TRUE

sorts by `archived_at_utc DESC`, takes the top N (default 3), and
produces a formatted block ready to paste above a system prompt:

    ## Previously archived attempts (learn from these failures)

    - ARC-YYYYMMDD-001 (archived 2026-04-17, reason: wrong_grain):
      The per-vendor grain was wrong — this metric is a per-PO fact.
      Target models were: fact_purchase_orders
      Column mappings attempted: 6

Truncation uses the same token-counting approach as
`_domain_context_loader.py` — `tiktoken` if available, `len(text)//4`
fallback. Returns an empty string (no header) when no qualifying
archives exist so callers never print a stale header.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

try:  # pragma: no cover - optional dep
    import tiktoken as _tiktoken
    _ENC = _tiktoken.get_encoding("cl100k_base")

    def _count_tokens(text: str) -> int:
        return len(_ENC.encode(text))
except Exception:  # pragma: no cover
    _ENC = None

    def _count_tokens(text: str) -> int:
        return max(1, len(text) // 4)


def _parse_bool(v) -> bool:
    """Accept true/false (string), 1/0, Python bool. Everything else → False."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in {"true", "1", "t", "y", "yes"}


def _format_date(v) -> str:
    """Trim ISO timestamp to YYYY-MM-DD for human display."""
    s = str(v or "").strip()
    if not s:
        return ""
    return s[:10]


def load_archived_term_context(
    term_name: str,
    max_tokens: int = 500,
    max_entries: int = 3,
) -> str:
    """Return a bullet-list block of prior archives for this term_name.

    Only entries with learning_signal=TRUE are surfaced. Demo / test
    archives that were flagged learning_signal=FALSE are INVISIBLE here
    by design (see decision #47). Returns "" when nothing qualifies.
    """
    if not term_name or not str(term_name).strip():
        return ""

    # Lazy import so scripts / tools that don't need Streamlit can import
    # this module without pulling it in.
    try:
        from db import query
    except Exception:
        return ""

    # Fetch every archive for the term name (learning_signal may be
    # stored as bool or 'true'/'false' string depending on csv→parquet
    # type inference — filter it in Python for robustness).
    safe_name = str(term_name).replace("'", "''")
    try:
        df = query(
            "SELECT archive_id, business_term_id, term_name, "
            "archived_at_utc, archived_reason_code, archived_reason_text, "
            "learning_signal, target_models, s2t_row_ids "
            "FROM main_seeds.archive_log "
            f"WHERE term_name = '{safe_name}' "
            "ORDER BY CAST(archived_at_utc AS VARCHAR) DESC"
        )
    except Exception:
        return ""

    if df is None or len(df) == 0:
        return ""

    rows = []
    for _, r in df.iterrows():
        if not _parse_bool(r.get("learning_signal")):
            continue
        rows.append(r)
        if len(rows) >= max_entries:
            break

    if not rows:
        return ""

    header = "## Previously archived attempts (learn from these failures)"
    lines = [header, ""]
    budget = max_tokens - _count_tokens(header) - 1
    if budget <= 0:
        return ""

    for r in rows:
        def _sv(k):
            _v = r.get(k)
            return str(_v).strip() if pd.notna(_v) else ""
        arc_id = _sv("archive_id")
        date = _format_date(r.get("archived_at_utc"))
        code = _sv("archived_reason_code")
        text = _sv("archived_reason_text")
        tms = _sv("target_models")
        s2t_ids = _sv("s2t_row_ids")
        # Semicolon-separated per the archive_term helper (avoids
        # embedded commas that confuse DuckDB's CSV dialect sniffer).
        n_cols = len([x for x in s2t_ids.split(";") if x.strip()]) if s2t_ids else 0

        block_lines = [f"- {arc_id} (archived {date}, reason: {code}):"]
        if text:
            block_lines.append(f"  {text}")
        if tms:
            block_lines.append(f"  Target models were: {tms}")
        block_lines.append(f"  Column mappings attempted: {n_cols}")
        block_lines.append("")

        entry = "\n".join(block_lines)
        cost = _count_tokens(entry)
        if cost > budget:
            break
        lines.append(entry)
        budget -= cost

    if len(lines) <= 2:  # header + blank only, nothing actually rendered
        return ""

    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    # Quick CLI smoke check — prints what a real call would look like
    # for a given term_name. Expects an active DB + exported Parquet.
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    print(load_archived_term_context(name) or "(no qualifying archives)")
