"""Vault grounding + column verification for create_s2t (issue #130).

After RULE 3 (#129) steers the generator to ref vault models, the bundle must
also surface the vault models' REAL column names — otherwise the LLM falls back
to raw SAP names (FKDAT/FKSTO/NETWR) that don't exist in the vault sats (which
use billing_date/cancelled_flag/revenue_amount), producing vault-layered but
unbuildable SQL.

Two deterministic helpers (no LLM):
  - vault_schema_block(): a compact 'real columns per vault model' block injected
    into the generation prompt (grounding).
  - verify_vault_columns(): a column-existence pre-flight that flags any
    <alias>.<col> referencing a vault model where <col> is not a real column
    (verification). dbt build remains the hard backstop; this catches it earlier.
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

import duckdb

_DB = Path(__file__).resolve().parent.parent / "cpe_analytics.duckdb"

_REF_RE = re.compile(
    r"(?:FROM|JOIN)\s+\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}\s+(?:AS\s+)?([A-Za-z_]\w*)",
    re.IGNORECASE,
)
_COLREF_RE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w+)\b")


def _vault_columns(conn) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'main_vault' "
        "ORDER BY table_name, ordinal_position"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for t, c in rows:
        out.setdefault(t, []).append(c)
    return out


def _mart_columns(conn) -> dict[str, list[str]]:
    """Sibling-mart schemas: cross-mart refs (e.g. shared dims) are allowed
    by the layering directives, so their REAL columns must be grounded too —
    otherwise the generator can ref a dim and assume columns it doesn't have."""
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'main_marts' "
        "ORDER BY table_name, ordinal_position"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for t, c in rows:
        out.setdefault(t, []).append(c)
    return out


def vault_schema_block(conn=None) -> str:
    """Compact 'vault model -> real columns' catalog for the generation prompt."""
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB), read_only=True)
    try:
        cols = _vault_columns(conn)
        mart_cols = _mart_columns(conn)
    finally:
        if owned:
            conn.close()
    lines = [
        "## VAULT model column schemas (issue #130 grounding)",
        "RULE 3 marts/obt must ref() these vault models. Use ONLY these EXACT "
        "column names — do NOT use raw SAP names (FKDAT, FKSTO, NETWR, VBELN, "
        "MATNR, KUNRG...). The vault uses business names + hash keys (hk_*). "
        "If a column you need is absent below, emit a warning naming the gap "
        "instead of inventing a column or a model.",
        "",
    ]
    for t in sorted(cols):
        lines.append(f"  {t}({', '.join(cols[t])})")
    if mart_cols:
        lines += [
            "",
            "## SIBLING MART column schemas (cross-mart refs, e.g. shared dims)",
            "These are the EXACT columns of existing marts. If you ref() one of "
            "these, use ONLY these columns. If the identifier or measure your "
            "term needs is absent from every vault model AND every sibling mart "
            "below, the source lacks vault coverage — propose the missing "
            "staging + vault models in dbt_models[] instead of forcing a join "
            "onto a model that lacks the column.",
            "",
        ]
        for t in sorted(mart_cols):
            lines.append(f"  {t}({', '.join(mart_cols[t])})")
    return "\n".join(lines)


def verify_no_staging_refs(dbt_models) -> list[str]:
    """RULE 3 pre-flight (#129 hard-enforce): mart/obt/knowledge models must not
    ref() staging (stg_sap__*) or raw_sap. Returns violation strings (empty=ok)."""
    issues: list[str] = []
    ref_re = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)")
    for m in dbt_models or []:
        layer = (m.get("layer") or "").lower()
        if layer not in ("marts", "mart", "obt", "knowledge"):
            continue
        name = m.get("name") or m.get("filename") or "?"
        for ref in ref_re.findall(m.get("sql") or ""):
            if ref.startswith("stg_sap__") or ref.startswith("raw_") or ref.startswith("stg_"):
                issues.append(
                    f"{name} ({layer}) refs staging `{ref}` — RULE 3 requires vault-only"
                )
    return sorted(set(issues))


def verify_vault_columns(dbt_models, conn=None) -> list[str]:
    """Pre-flight: flag <alias>.<col> refs to vault models where col is not real.

    Returns a deduped list of human-readable violation strings (empty = clean).
    Only checks aliases bound (via FROM/JOIN {{ ref() }}) to a VAULT model;
    CTE aliases and non-vault refs (dims) are ignored.
    """
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB), read_only=True)
    try:
        vcols = {t: {c.lower() for c in cs} for t, cs in _vault_columns(conn).items()}
        vcols_disp = {t: cs for t, cs in _vault_columns(conn).items()}
    finally:
        if owned:
            conn.close()

    issues: list[str] = []
    for m in dbt_models or []:
        sql = m.get("sql") or ""
        name = m.get("name") or m.get("filename") or "?"
        alias2model: dict[str, str] = {}
        for mref, alias in _REF_RE.findall(sql):
            if mref in vcols:  # only track vault-model aliases
                alias2model[alias.lower()] = mref
        seen: set[tuple[str, str]] = set()
        for alias, col in _COLREF_RE.findall(sql):
            mdl = alias2model.get(alias.lower())
            if not mdl:
                continue
            if col.lower() not in vcols[mdl] and (alias.lower(), col.lower()) not in seen:
                seen.add((alias.lower(), col.lower()))
                sample = ", ".join(vcols_disp[mdl][:10])
                issues.append(
                    f"{name}: `{alias}.{col}` -> column '{col}' does NOT exist in "
                    f"vault model `{mdl}` (real columns: {sample}...)"
                )
    return sorted(set(issues))
