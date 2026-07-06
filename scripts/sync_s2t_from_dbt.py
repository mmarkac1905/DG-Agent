"""Sync s2t_mapping.csv transformation_logic_sql with actual dbt expressions.

Reads dbt_column_lineage (parsed from real dbt SQL by scan_dbt_models.py)
and updates s2t_mapping rows whose transformation_logic_sql does not match
the actual expression of the target column. dbt code is the single source
of truth — s2t_mapping exists to document it for business users, not to
drift from it.

Before this script existed, s2t_mapping entries were written by hand and
could hallucinate SQL that never ran (e.g. S007 claimed
fact_goods_movements.movement_type was derived via CASE WHEN BWART='122'
THEN 1 ELSE 0 END, but the actual column is a direct pass-through of the
raw movement-type code). Running this after every dbt scan keeps the two
aligned automatically.

IMPORTANT — simple-reference guard: when the real dbt expression is
nothing more than a CTE alias reference like `pv.vendor_id`, that tells
you nothing a reader didn't already know from the column name. The
hand-written staging-level SQL in the seed (e.g. `CAST(LIFNR AS
VARCHAR)`) is strictly more informative in that case. We skip the
overwrite so documentation quality doesn't regress.

Usage: python scripts/sync_s2t_from_dbt.py
"""
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "dbt" / "seeds"

# Import CSV write safeguard from app/.
sys.path.insert(0, str(ROOT / "app"))
from _csv_safeguard import assert_csv_safe, assert_fieldnames_cover_rows  # noqa: E402

import pandas as pd  # noqa: E402


def load_csv(name):
    path = SEED_DIR / f"{name}.csv"
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(name, rows, fieldnames):
    path = SEED_DIR / f"{name}.csv"
    # Schema-drift guard: csv.DictWriter opens the file in "w" mode
    # (truncates to 0) BEFORE validating row keys. Any row carrying a
    # key outside fieldnames would raise ValueError mid-write, leaving
    # the CSV header-only. Check first so the file stays intact on
    # schema drift.
    assert_fieldnames_cover_rows(fieldnames, rows)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


_SIMPLE_REF_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$", re.IGNORECASE)


def is_simple_reference(expr: str) -> bool:
    """True when `expr` is just `alias.column` with no functions, arithmetic,
    CASE WHEN, or other transformation. Such an expression conveys no
    information beyond the column name itself, so we prefer whatever
    human-written SQL the seed already carries."""
    return bool(_SIMPLE_REF_RE.match((expr or "").strip()))


def _trace_all_direct(target_model: str, target_col: str, lineage_index: dict) -> bool:
    """True when the full trace chain from (target_model, target_col) down
    to its deepest origin is nothing but `direct` transformations. Used to
    identify columns that are true pass-throughs with no real SQL anywhere
    in the pipeline."""
    seen = set()
    model, col = target_model, target_col
    hops = 0
    while hops < 8:
        hops += 1
        key = (model, col)
        if key in seen:
            break
        seen.add(key)
        info = lineage_index.get(key)
        if info is None:
            break
        if (info.get("transformation_type") or "").strip() != "direct":
            return False
        nxt_model = (info.get("origin_table") or "").strip()
        nxt_col = (info.get("origin_column") or "").strip()
        if not nxt_model or not nxt_col:
            break
        model, col = nxt_model, nxt_col
    return True


def main():
    s2t = load_csv("s2t_mapping")
    lineage = load_csv("dbt_column_lineage")

    lineage_lookup = {}
    lineage_index = {}
    for row in lineage:
        key = (row.get("model_name", ""), row.get("column_name", ""))
        expr = (row.get("expression", "") or "").strip()
        if expr:
            lineage_lookup[key] = expr
        lineage_index[key] = row

    changes = []
    for row in s2t:
        target_model = (row.get("target_model", "") or "").strip()
        target_col = (row.get("target_column", "") or "").strip()
        old_sql = (row.get("transformation_logic_sql", "") or "").strip()
        actual_sql = lineage_lookup.get((target_model, target_col), "")

        if not target_model or not target_col:
            continue
        if not actual_sql:
            continue
        if actual_sql == old_sql:
            continue

        # Simple CTE alias reference (`pv.vendor_id`) carries no
        # information beyond the column name. Two sub-cases:
        #   a) The full trace chain is ALL `direct` — the column is a
        #      true pass-through with no real SQL anywhere in the
        #      pipeline. Clear the s2t SQL so the page can render
        #      "Direct pass-through (no transformation)" instead of
        #      leaving stale or hallucinated SQL sitting there.
        #   b) Somewhere upstream there IS a real transformation
        #      (e.g. a staging CASE WHEN). Keep whatever the seed
        #      already has — it's the best human-written anchor we've
        #      got for documentation.
        if is_simple_reference(actual_sql):
            if _trace_all_direct(target_model, target_col, lineage_index) and old_sql:
                changes.append({
                    "id": row["id"],
                    "target": f"{target_model}.{target_col}",
                    "old": (old_sql[:80] + ("…" if len(old_sql) > 80 else "")),
                    "new": "(cleared — direct pass-through)",
                })
                row["transformation_logic_sql"] = ""
            continue

        changes.append({
            "id": row["id"],
            "target": f"{target_model}.{target_col}",
            "old": (old_sql[:80] + ("…" if len(old_sql) > 80 else "")),
            "new": (actual_sql[:80] + ("…" if len(actual_sql) > 80 else "")),
        })
        row["transformation_logic_sql"] = actual_sql

    # --- Rule 14 enforcement: delete orphan S2T rows, insert placeholders ---
    # Build index of actual output columns per model from lineage
    model_output_cols = {}
    for row in lineage:
        m = (row.get("model_name", "") or "").strip()
        c = (row.get("column_name", "") or "").strip()
        if m and c:
            model_output_cols.setdefault(m, set()).add(c)

    # Collect all models referenced by s2t_mapping
    s2t_models = set()
    for row in s2t:
        tm = (row.get("target_model", "") or "").strip()
        if tm:
            s2t_models.add(tm)

    # Find orphan rows (target_column not in model output)
    orphan_ids = []
    kept_s2t = []
    for row in s2t:
        tm = (row.get("target_model", "") or "").strip()
        tc = (row.get("target_column", "") or "").strip()
        if tm and tc and tm in model_output_cols and tc not in model_output_cols[tm]:
            orphan_ids.append({
                "id": row.get("id", ""),
                "target": f"{tm}.{tc}",
                "reason": "target_column not in dbt model output",
            })
        else:
            kept_s2t.append(row)

    # Find new model output columns with no S2T row yet
    existing_s2t_cols = set()
    for row in kept_s2t:
        tm = (row.get("target_model", "") or "").strip()
        tc = (row.get("target_column", "") or "").strip()
        if tm and tc:
            existing_s2t_cols.add((tm, tc))

    # Load catalog for layer info
    try:
        catalog = load_csv("dbt_model_catalog")
        model_layer = {r["model_name"]: r.get("layer", "") for r in catalog}
    except Exception:
        model_layer = {}

    # Load glossary to find business_term_id for models already in S2T
    model_to_term = {}
    for row in kept_s2t:
        tm = (row.get("target_model", "") or "").strip()
        bt_id = (row.get("business_term_id", "") or "").strip()
        bt_name = (row.get("business_term_name", "") or "").strip()
        if tm and bt_id:
            model_to_term[tm] = (bt_id, bt_name)

    placeholders = []
    next_id = 0
    for row in kept_s2t:
        rid = row.get("id", "")
        if isinstance(rid, str) and rid.startswith("S"):
            try:
                next_id = max(next_id, int(rid.replace("S", "")))
            except ValueError:
                pass

    # Only insert placeholders for models where orphan rows were deleted
    # (i.e., models with a known S2T/dbt mismatch that needs correction).
    # Without this scope limit, every unmapped column in every referenced
    # model would get a placeholder — hundreds of housekeeping columns.
    orphan_models = set(o["target"].split(".")[0] for o in orphan_ids)
    for model in sorted(orphan_models & set(model_output_cols.keys())):
        for col in sorted(model_output_cols[model]):
            if (model, col) not in existing_s2t_cols:
                next_id += 1
                bt_id, bt_name = model_to_term.get(model, ("", ""))
                # Try to find origin info from lineage
                li = lineage_index.get((model, col), {})
                origin_table = (li.get("origin_table") or "").strip()
                origin_col = (li.get("origin_column") or "").strip()
                # Map origin_table from staging name to SAP name
                source_table = re.sub(r"^stg_\w+?__", "", origin_table).upper() if origin_table else ""
                source_field = origin_col.upper() if origin_col else ""
                placeholder = {
                    "id": f"S{next_id:03d}",
                    "business_term_id": bt_id,
                    "business_term_name": bt_name,
                    "source_table": source_table,
                    "source_field": source_field,
                    "source_description": "",
                    "target_model": model,
                    "target_column": col,
                    "transformation_logic_plain": "",
                    "transformation_logic_sql": "",
                    "join_description": "",
                    "filter_description": "",
                    "notes": "Auto-placeholder: needs business rule authorship",
                }
                placeholders.append(placeholder)
                kept_s2t.append(placeholder)

    # Log all Rule 14 enforcement actions
    log_path = SEED_DIR / "s2t_sync_warnings.log"
    log_lines = []
    if orphan_ids:
        log_lines.append(f"Rule 14 enforcement: deleted {len(orphan_ids)} orphan S2T rows:")
        for o in orphan_ids:
            log_lines.append(f"  {o['id']}: {o['target']} ({o['reason']})")
    if placeholders:
        log_lines.append(f"Rule 14 enforcement: added {len(placeholders)} placeholder S2T rows:")
        for p in placeholders:
            log_lines.append(f"  {p['id']}: {p['target_model']}.{p['target_column']} (needs business rule)")
    if log_lines:
        with log_path.open("w", encoding="utf-8") as lf:
            lf.write("\n".join(log_lines) + "\n")

    # Report
    s2t = kept_s2t
    needs_save = bool(changes or orphan_ids or placeholders)

    if changes:
        print(f"{len(changes)} s2t_mapping rows out of sync with dbt:\n")
        for c in changes:
            print(f"  {c['id']:<5} {c['target']}")
            print(f"    OLD: {c['old']}")
            print(f"    NEW: {c['new']}")
            print()

    if orphan_ids:
        print(f"Rule 14: deleted {len(orphan_ids)} orphan S2T rows (target_column not in dbt output):")
        for o in orphan_ids:
            print(f"  {o['id']}: {o['target']}")
        print()

    if placeholders:
        print(f"Rule 14: added {len(placeholders)} placeholder S2T rows (needs business rule):")
        for p in placeholders:
            print(f"  {p['id']}: {p['target_model']}.{p['target_column']}")
        print()

    if needs_save:
        fieldnames = list(s2t[0].keys()) if s2t else []
        assert_csv_safe(SEED_DIR / "s2t_mapping.csv", pd.DataFrame(s2t))
        save_csv("s2t_mapping", s2t, fieldnames)
        total_changes = len(changes) + len(orphan_ids) + len(placeholders)
        print(f"Updated dbt/seeds/s2t_mapping.csv ({total_changes} changes)")
    else:
        print("s2t_mapping.transformation_logic_sql is in sync with dbt.")


if __name__ == "__main__":
    main()
