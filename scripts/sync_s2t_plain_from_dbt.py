"""Sync s2t_mapping.transformation_logic_plain with actual dbt SQL.

For each s2t_mapping row that has a non-empty transformation_logic_sql,
call Claude to generate a one-sentence plain-English description of
what that SQL does for a business user. dbt SQL is the single source
of truth; the plain field should describe what the SQL *actually* does,
not what someone thought the column would do when they wrote the seed.

Pairs with scripts/sync_s2t_from_dbt.py (which keeps the SQL field
aligned with dbt); run this second so the plain-language regen sees
the fresh SQL.

Usage: python scripts/sync_s2t_plain_from_dbt.py
Requires: ANTHROPIC_API_KEY in .env or environment.

Mirrors the HTTP-based Claude integration style from app/claude_api.py
so we don't pull in the anthropic SDK as a new dependency.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "dbt" / "seeds"

# CSV write safeguard — Phase 12 hotfix 5 extension. Fourth s2t_mapping
# writer identified; guard it at the write boundary like the other three.
sys.path.insert(0, str(ROOT / "app"))
from _csv_safeguard import assert_csv_safe, assert_fieldnames_cover_rows  # noqa: E402
ENV_PATH = ROOT / ".env"
# Stable cache: row_id → sha256 of the SQL that last produced the plain
# description. Without it every end_of_task run would regenerate all 28
# descriptions (the LLM rewords each call) and spam the git diff.
CACHE_PATH = SEED_DIR / ".s2t_plain_cache.json"
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"

# Load .env from project root (same parser as app/claude_api.py)
if ENV_PATH.exists():
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def load_csv(name):
    path = SEED_DIR / f"{name}.csv"
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(name, rows, fieldnames):
    path = SEED_DIR / f"{name}.csv"
    # Two guards run BEFORE opening the file for writing:
    #  1. Row count — catches catastrophic truncation (Phase 12 hotfix 5).
    #  2. Fieldnames-cover-rows — catches csv.DictWriter's truncate-then-
    #     fail pattern, where open("w", ...) truncates to 0 before
    #     DictWriter validates row keys (Phase 12 hotfix 5 extension).
    assert_csv_safe(path, pd.DataFrame(rows))
    assert_fieldnames_cover_rows(fieldnames, rows)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


SYSTEM_PROMPT = (
    "You are a senior data analyst writing business-facing documentation "
    "for a procurement analytics warehouse. Your audience is a business "
    "user who knows SAP terminology but not SQL. Given a SQL expression "
    "that computes a target column, write ONE short sentence describing "
    "what the column represents and how it's derived — in plain English, "
    "no SQL function names, no code. Be specific, not vague.\n\n"
    "CRITICAL: If the SQL contains ANY function — CASE WHEN, CAST, SUBSTR, "
    "DATE_TRUNC, COALESCE, SUM, MIN, MAX, COUNT, AVG, ROUND, arithmetic "
    "(+, -, *, /), or any other transformation — NEVER describe the column "
    "as 'pass-through', 'direct', 'without transformation', 'unchanged', "
    "or 'flows through unchanged'. Describe what the function actually does "
    "(e.g. 'converts the date format', 'maps codes to labels', 'calculates "
    "the difference between two dates').\n\n"
    "If it's a CASE WHEN mapping codes to labels, list a couple of the "
    "mappings. If it's a date conversion (CAST to DATE), say 'converts from "
    "the internal format to a standard date'. If it's a date subtraction, "
    "say what the two dates are. Return ONLY the sentence — no preamble, "
    "no quotes, no trailing notes."
)

PASSTHROUGH_SYSTEM_PROMPT = (
    "You are a senior data analyst writing business-facing documentation. "
    "The target column below is a direct pass-through from an SAP source "
    "field — no transformation runs anywhere in the pipeline. Write ONE "
    "short sentence explaining what the column carries, phrased as a "
    "pass-through ('carries...', 'flows through unchanged from...', "
    "'direct copy of...'). Mention the SAP field name once. No SQL, no "
    "code, no preamble, no quotes. Return only the sentence."
)

USER_TEMPLATE = """Business term: {term}
Source:        {source_table}.{source_field}
Target column: {target_model}.{target_column}

SQL expression:
{sql}

One-sentence plain-English description:"""

PASSTHROUGH_USER_TEMPLATE = """Business term: {term}
SAP source:    {source_table}.{source_field} — {source_description}
Target column: {target_model}.{target_column}

This column has no transformation — it is a direct pass-through of the
SAP source field through staging, vault, and mart layers unchanged.

One-sentence plain-English description:"""


def _post_claude(api_key, system_prompt, user_prompt, timeout=30, max_tokens=120):
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    r = requests.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        data=json.dumps(body),
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    for block in payload.get("content") or []:
        if block.get("type") == "text":
            return (block.get("text") or "").strip()
    return ""


def generate_plain_description(api_key, *, sql, source_table, source_field,
                               target_model, target_column, business_term,
                               timeout=30):
    """Generate a plain-English description for a row with real SQL."""
    return _post_claude(
        api_key,
        SYSTEM_PROMPT,
        USER_TEMPLATE.format(
            term=business_term or "(unnamed)",
            source_table=source_table or "(unknown)",
            source_field=source_field or "(unknown)",
            target_model=target_model or "(unknown)",
            target_column=target_column or "(unknown)",
            sql=sql,
        ),
        timeout=timeout,
    )


def generate_passthrough_description(api_key, *, source_table, source_field,
                                     source_description, target_model,
                                     target_column, business_term, timeout=30):
    """Generate a plain-English description for a pass-through column.
    These have empty transformation_logic_sql because the sync cleared
    them — the column carries its SAP source value unchanged."""
    return _post_claude(
        api_key,
        PASSTHROUGH_SYSTEM_PROMPT,
        PASSTHROUGH_USER_TEMPLATE.format(
            term=business_term or "(unnamed)",
            source_table=source_table or "(unknown)",
            source_field=source_field or "(unknown)",
            source_description=source_description or "(no description)",
            target_model=target_model or "(unknown)",
            target_column=target_column or "(unknown)",
        ),
        timeout=timeout,
    )


BUSINESS_SYSTEM_PROMPT = (
    "You are writing for a business user who has never seen SAP or a data "
    "warehouse. Write ONE short sentence (1-2 sentences max) describing "
    "what this data column means in the business process.\n\n"
    "FORBIDDEN — never use any of these:\n"
    "- Table names: EKKO, MKPF, MSEG, EBAN, EKPO, EKET, EQUI, EQBS, MARD, RBKP, RSEG\n"
    "- Field names: LIFNR, BEDAT, BUDAT, EBELN, MENGE, BWART, NETWR, EQUNR, MATNR, LABST\n"
    "- Technical terms: pipeline, staging, vault, hash key, CTE, join, "
    "pass-through, data warehouse, ETL, transformation, aggregation, "
    "mart, OBT, schema, model, column lineage\n"
    "- SAP jargon without explanation: movement type, document type, "
    "transaction code, posting date (say 'date recorded' instead)\n\n"
    "GOOD examples:\n"
    "- 'The date when the purchase order was created'\n"
    "- 'Which supplier is fulfilling this order'\n"
    "- 'Earliest date goods were received at the warehouse'\n"
    "- 'Number of days between placing the order and receiving goods'\n\n"
    "Return ONLY the sentence — no preamble, no quotes, no trailing notes."
)

BUSINESS_USER_TEMPLATE = """Business term: {term}
Target column name: {target_column}
What the column computes (technical): {sql}

Write a business-friendly description (no technical terms):"""


CHAIN_SYSTEM_PROMPT = (
    "You are a senior data analyst writing business-friendly labels for "
    "transformation steps in a data pipeline. For each SQL expression, "
    "write a SHORT label (5-10 words max) that a business user would "
    "understand. No SQL terminology, no function names. Return ONLY "
    "the labels separated by semicolons, one per input expression, in "
    "the same order. No preamble, no numbering, no quotes."
)

CHAIN_USER_TEMPLATE = """Column: {target_model}.{target_column}
Business term: {term}

Transformation steps (from source to target):
{chain_steps}

Write a short business-friendly label (5-10 words) for each step, separated by semicolons:"""


def generate_chain_plain(api_key, *, chain_steps, target_model, target_column,
                         business_term, timeout=30):
    """Generate plain-language labels for each step in a transformation chain."""
    return _post_claude(
        api_key,
        CHAIN_SYSTEM_PROMPT,
        CHAIN_USER_TEMPLATE.format(
            target_model=target_model or "(unknown)",
            target_column=target_column or "(unknown)",
            term=business_term or "(unnamed)",
            chain_steps=chain_steps,
        ),
        timeout=timeout,
    )


def generate_business_description(api_key, *, sql, target_column, business_term,
                                  timeout=30):
    """Generate a business-audience description with no technical terms."""
    return _post_claude(
        api_key,
        BUSINESS_SYSTEM_PROMPT,
        BUSINESS_USER_TEMPLATE.format(
            term=business_term or "(unnamed)",
            target_column=target_column or "(unknown)",
            sql=sql or "(no SQL)",
        ),
        timeout=timeout,
    )


SCOPE_SYSTEM_PROMPT = (
    "You are writing for a business user who has never seen SAP or a data "
    "warehouse. Given a SQL model and a list of specific columns from that "
    "model, describe ONLY the joins and filters that are relevant to "
    "computing those specific columns.\n\n"
    "FORBIDDEN — never use: table names, field names, SQL keywords (JOIN, "
    "WHERE, CTE, GROUP BY), technical terms (pipeline, staging, vault, "
    "hash key, movement type code).\n\n"
    "Return ONLY valid JSON with this exact structure:\n"
    '{"how_combined": "one sentence about how data sources are linked '
    'for these columns", "whats_included": "semicolon-separated list of '
    'what data is included/excluded for these columns"}\n\n'
    "If a filter in the SQL only applies to OTHER columns (not in the list), "
    "DO NOT mention it. Only describe what's relevant to the listed columns."
)

SCOPE_USER_TEMPLATE = """Business term: {term}
Columns to describe: {columns}

Full compiled model SQL:
{sql}

Current "How data is combined" (may include irrelevant joins): {current_joins}
Current "What's included" (may include irrelevant filters): {current_filters}

Return JSON with term-scoped descriptions for ONLY the listed columns:"""


def generate_scoped_join_filter(api_key, *, term, columns, sql,
                                current_joins, current_filters, timeout=30):
    """Generate term-scoped join/filter descriptions via LLM."""
    text = _post_claude(
        api_key,
        SCOPE_SYSTEM_PROMPT,
        SCOPE_USER_TEMPLATE.format(
            term=term or "(unnamed)",
            columns=columns or "(unknown)",
            sql=sql[:4000] or "(no SQL)",
            current_joins=current_joins or "(none)",
            current_filters=current_filters or "(none)",
        ),
        timeout=timeout,
        max_tokens=300,
    )
    try:
        import json as _json
        # Strip markdown fences if present
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        clean = clean.strip()
        result = _json.loads(clean)
        return result.get("how_combined", ""), result.get("whats_included", "")
    except Exception:
        return "", ""


def _sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _passthrough_hash(source_table: str, source_field: str,
                      target_model: str, target_column: str) -> str:
    """Cache key for pass-through rows (no SQL to hash). Includes a
    prefix so a later SQL change for the same row invalidates the
    cache and vice-versa."""
    key = f"passthrough:{source_table}|{source_field}|{target_model}|{target_column}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "your-api-key-here":
        print("ANTHROPIC_API_KEY not set; skipping plain-language sync.")
        return

    s2t = load_csv("s2t_mapping")
    fieldnames = list(s2t[0].keys()) if s2t else []
    cache = _load_cache()

    changes = []
    skipped_fresh = 0
    errors = 0

    for row in s2t:
        row_id = row.get("id", "")
        sql = (row.get("transformation_logic_sql") or "").strip()
        old_plain = (row.get("transformation_logic_plain") or "").strip()
        source_table = row.get("source_table", "")
        source_field = row.get("source_field", "")
        target_model = row.get("target_model", "")
        target_column = row.get("target_column", "")

        # Two code paths — SQL rows use the SQL hash for stability;
        # pass-through rows (no SQL) use a composite hash of the
        # source/target fields. Either way the cache prevents
        # re-hitting the API on unchanged rows.
        if sql:
            current_hash = _sql_hash(sql)
            if cache.get(row_id) == current_hash and old_plain:
                skipped_fresh += 1
                continue
            try:
                new_plain = generate_plain_description(
                    api_key,
                    sql=sql,
                    source_table=source_table,
                    source_field=source_field,
                    target_model=target_model,
                    target_column=target_column,
                    business_term=row.get("business_term_name", ""),
                )
            except Exception as e:
                errors += 1
                print(f"  [warn] {row_id} API call failed: {e}")
                continue
        else:
            # Pass-through: no SQL, but the column still deserves a
            # business-friendly description. Cache key is based on
            # source/target so a future column rename or target swap
            # invalidates the entry.
            current_hash = _passthrough_hash(
                source_table, source_field, target_model, target_column
            )
            if cache.get(row_id) == current_hash and old_plain:
                skipped_fresh += 1
                continue
            try:
                new_plain = generate_passthrough_description(
                    api_key,
                    source_table=source_table,
                    source_field=source_field,
                    source_description=row.get("source_description", ""),
                    target_model=target_model,
                    target_column=target_column,
                    business_term=row.get("business_term_name", ""),
                )
            except Exception as e:
                errors += 1
                print(f"  [warn] {row_id} API call failed: {e}")
                continue

        if new_plain and new_plain != old_plain:
            changes.append({
                "id": row_id,
                "target": f"{target_model}.{target_column}",
                "old": (old_plain[:80] + ("…" if len(old_plain) > 80 else "")),
                "new": (new_plain[:80] + ("…" if len(new_plain) > 80 else "")),
                "kind": "sql" if sql else "passthrough",
            })
            row["transformation_logic_plain"] = new_plain

        cache[row_id] = current_hash

    if changes:
        print(f"{len(changes)} plain descriptions updated:\n")
        for c in changes:
            print(f"  {c['id']:<5} {c['target']} [{c['kind']}]")
            print(f"    OLD: {c['old']}")
            print(f"    NEW: {c['new']}")
            print()
        save_csv("s2t_mapping", s2t, fieldnames)
        print(f"Updated dbt/seeds/s2t_mapping.csv ({len(changes)} rows)")
    else:
        print("All plain descriptions are up to date.")

    _save_cache(cache)

    if skipped_fresh:
        print(f"(skipped {skipped_fresh} rows — unchanged since last sync)")
    if errors:
        print(f"({errors} rows failed — see warnings above)")

    # --- Generate business-audience descriptions (Rule 7: separate from technical) ---
    biz_changes = 0
    biz_skipped = 0
    for row in s2t:
        row_id = row.get("id", "")
        sql = (row.get("transformation_logic_sql") or "").strip()
        old_plain = (row.get("transformation_logic_plain") or "").strip()
        old_biz = (row.get("transformation_logic_plain_business") or "").strip()
        target_column = row.get("target_column", "")
        business_term = row.get("business_term_name", "")

        # Use technical plain as input context if no SQL
        input_text = sql if sql else old_plain
        if not input_text:
            row.setdefault("transformation_logic_plain_business", "")
            continue

        cache_key = f"biz_{row_id}"
        current_hash = _sql_hash(f"biz:{input_text}")
        if cache.get(cache_key) == current_hash and old_biz:
            biz_skipped += 1
            continue

        try:
            new_biz = generate_business_description(
                api_key,
                sql=input_text,
                target_column=target_column,
                business_term=business_term,
            )
        except Exception as e:
            errors += 1
            print(f"  [warn] {row_id} business desc failed: {e}")
            continue

        if new_biz and new_biz != old_biz:
            row["transformation_logic_plain_business"] = new_biz
            cache[cache_key] = current_hash
            biz_changes += 1
        else:
            cache[cache_key] = current_hash

    if biz_changes:
        # Refresh fieldnames — the biz loop above adds
        # `transformation_logic_plain_business` to row dicts, which was
        # not present when fieldnames was captured at function entry.
        # dict.fromkeys preserves insertion order so CSV column order
        # stays stable. Without this refresh, csv.DictWriter would
        # raise ValueError mid-write AFTER opening the file in "w"
        # mode, leaving a truncated header-only CSV.
        fieldnames = list(dict.fromkeys([k for r in s2t for k in r.keys()]))
        save_csv("s2t_mapping", s2t, fieldnames)
        print(f"\n{biz_changes} business descriptions generated")
        if biz_skipped:
            print(f"(skipped {biz_skipped} — unchanged)")

    _save_cache(cache)

    # --- Generate term-scoped join/filter descriptions ---
    glossary = load_csv("business_glossary")
    glossary_fieldnames = list(glossary[0].keys()) if glossary else []
    if "business_join_description" not in glossary_fieldnames:
        glossary_fieldnames.extend(["business_join_description", "business_filter_description"])

    # Build term → columns and term → model mapping from s2t
    term_columns = {}
    term_models = {}
    term_joins = {}
    term_filters = {}
    for row in s2t:
        tid = (row.get("business_term_id") or "").strip()
        tc = (row.get("target_column") or "").strip()
        tm = (row.get("target_model") or "").strip()
        jd = (row.get("join_description") or "").strip()
        fd = (row.get("filter_description") or "").strip()
        if tid and tc:
            term_columns.setdefault(tid, []).append(tc)
        if tid and tm:
            term_models[tid] = tm
        if tid and jd:
            term_joins.setdefault(tid, []).append(jd)
        if tid and fd:
            term_filters.setdefault(tid, []).append(fd)

    compiled_dir = ROOT / "dbt" / "target" / "compiled" / "cpe_procurement_analytics" / "models"

    def _read_compiled_sql(model_name):
        if not compiled_dir.exists():
            return ""
        for layer_dir in compiled_dir.iterdir():
            candidate = layer_dir / f"{model_name}.sql"
            if candidate.exists():
                return candidate.read_text(encoding="utf-8")
        return ""

    scope_changes = 0
    scope_skipped = 0
    for g_row in glossary:
        tid = g_row.get("id", "")
        if tid not in term_columns:
            g_row.setdefault("business_join_description", "")
            g_row.setdefault("business_filter_description", "")
            continue

        cols = term_columns[tid]
        model = term_models.get(tid, "")
        compiled_sql = _read_compiled_sql(model)
        if not compiled_sql:
            g_row.setdefault("business_join_description", "")
            g_row.setdefault("business_filter_description", "")
            continue

        current_joins = "; ".join(sorted(set(term_joins.get(tid, []))))
        current_filters = "; ".join(sorted(set(term_filters.get(tid, []))))
        old_bj = (g_row.get("business_join_description") or "").strip()
        old_bf = (g_row.get("business_filter_description") or "").strip()

        cache_key = f"scope_{tid}"
        cache_input = f"{compiled_sql}|{','.join(sorted(cols))}|{current_joins}|{current_filters}"
        current_hash = _sql_hash(cache_input)
        if cache.get(cache_key) == current_hash and old_bj:
            scope_skipped += 1
            continue

        term_name = g_row.get("display_name") or g_row.get("term_name") or tid
        try:
            new_bj, new_bf = generate_scoped_join_filter(
                api_key,
                term=term_name,
                columns=", ".join(cols),
                sql=compiled_sql,
                current_joins=current_joins,
                current_filters=current_filters,
            )
        except Exception as e:
            print(f"  [warn] {tid} scope generation failed: {e}")
            continue

        if new_bj or new_bf:
            g_row["business_join_description"] = new_bj
            g_row["business_filter_description"] = new_bf
            cache[cache_key] = current_hash
            scope_changes += 1

    if scope_changes:
        glossary_path = SEED_DIR / "business_glossary.csv"
        with glossary_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=glossary_fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(glossary)
        print(f"\n{scope_changes} term-scoped join/filter descriptions generated")
        if scope_skipped:
            print(f"(skipped {scope_skipped} — unchanged)")

    _save_cache(cache)

    # --- Part 2: Generate plain-language labels for transformation chains ---
    lineage = load_csv("dbt_column_lineage")
    lineage_fieldnames = list(lineage[0].keys()) if lineage else []
    if "transformation_chain_plain" not in lineage_fieldnames:
        lineage_fieldnames.append("transformation_chain_plain")

    chain_changes = 0
    chain_skipped = 0
    # Build s2t lookup for business_term_name
    s2t_lookup = {}
    for row in s2t:
        key = (row.get("target_model", ""), row.get("target_column", ""))
        s2t_lookup[key] = row.get("business_term_name", "")

    for row in lineage:
        chain = (row.get("transformation_chain") or "").strip()
        old_chain_plain = (row.get("transformation_chain_plain") or "").strip()
        if not chain:
            row.setdefault("transformation_chain_plain", "")
            continue

        model = row.get("model_name", "")
        col = row.get("column_name", "")
        chain_hash = _sql_hash(f"chain:{chain}")
        cache_key = f"chain_{row.get('id', '')}"

        if cache.get(cache_key) == chain_hash and old_chain_plain:
            row.setdefault("transformation_chain_plain", old_chain_plain)
            chain_skipped += 1
            continue

        term = s2t_lookup.get((model, col), "")
        steps = chain.split(";")
        numbered = "\n".join(f"  {i+1}. {s.strip()}" for i, s in enumerate(steps))

        try:
            result = generate_chain_plain(
                api_key,
                chain_steps=numbered,
                target_model=model,
                target_column=col,
                business_term=term,
            )
            if result:
                row["transformation_chain_plain"] = result.strip()
                cache[cache_key] = chain_hash
                chain_changes += 1
            else:
                row.setdefault("transformation_chain_plain", "")
        except Exception:
            row.setdefault("transformation_chain_plain", "")

    if chain_changes:
        lineage_path = SEED_DIR / "dbt_column_lineage.csv"
        with lineage_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=lineage_fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(lineage)
        print(f"\n{chain_changes} transformation chain labels generated")
        if chain_skipped:
            print(f"(skipped {chain_skipped} chains — unchanged)")

    _save_cache(cache)


if __name__ == "__main__":
    main()
