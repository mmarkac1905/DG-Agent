"""Export context markdown files from DuckDB for chat sessions.

Generates readable summaries from knowledge models.
Usage: python scripts/export_context.py
"""
from __future__ import annotations

import time
import duckdb
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "cpe_analytics.duckdb"
CONTEXT_DIR = ROOT / "context"
TIMESTAMP = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    _start = time.time()
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(DB_PATH), read_only=True)

    pipeline = conn.execute("SELECT * FROM main_knowledge.knowledge_pipeline_summary").fetchdf()
    health = conn.execute("SELECT * FROM main_knowledge.knowledge_procurement_health").fetchdf()

    lines = [
        "# CPE Procurement Analytics — System Overview",
        f"_Generated: {TIMESTAMP}_",
        "",
        "## Pipeline",
        "",
        "| Layer | Models |",
        "| --- | --- |",
    ]
    for _, row in pipeline.iterrows():
        lines.append(f"| {row['layer']} | {row['table_count']} |")

    if not health.empty:
        h = health.iloc[0]
        lines += [
            "",
            "## Procurement Health",
            "",
            f"- **Total POs:** {h.get('total_pos', 'N/A')}",
            f"- **Avg Lead Time:** {h.get('avg_lead_time_days', 'N/A')} days ({h.get('lead_time_health', '?')})",
            f"- **OTD Rate:** {h.get('otd_rate_pct', 'N/A')}% ({h.get('otd_health', '?')})",
            f"- **PO Cycle Time:** {h.get('avg_po_cycle_days', 'N/A')} days ({h.get('cycle_time_health', '?')})",
            f"- **Total Spend:** EUR {h.get('total_spend_eur', 0):,.0f}",
            f"- **Concentration:** {h.get('highest_concentration_vendor', '?')} at {h.get('highest_concentration_pct', '?')}% ({h.get('concentration_health', '?')})",
            f"- **Inventory:** {h.get('total_stock_units', 0):,.0f} units, {h.get('zero_stock_locations', 0)} zero-stock ({h.get('inventory_health', '?')})",
        ]

    (CONTEXT_DIR / "current_state.md").write_text("\n".join(lines), encoding="utf-8")
    print("  Written: context/current_state.md")

    vendors = conn.execute("SELECT * FROM main_knowledge.knowledge_vendor_performance ORDER BY total_spend_eur DESC").fetchdf()
    lines = [
        "# Vendor Performance",
        f"_Generated: {TIMESTAMP}_",
        "",
        "| Vendor | Grade | OTD | Lead Time | Spend | POs |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for _, v in vendors.iterrows():
        lines.append(
            f"| {v['vendor_name']} | {v['performance_grade']} ({v['health_status']}) | "
            f"{v['otd_rate_pct']}% | {v['avg_lead_time_days']}d | "
            f"EUR {v['total_spend_eur']:,.0f} | {v['total_pos']} |"
        )
    (CONTEXT_DIR / "vendor_performance.md").write_text("\n".join(lines), encoding="utf-8")
    print("  Written: context/vendor_performance.md")

    cpe = conn.execute("SELECT * FROM main_knowledge.knowledge_cpe_lifecycle_metrics ORDER BY total_devices DESC").fetchdf()
    lines = [
        "# CPE Lifecycle Metrics",
        f"_Generated: {TIMESTAMP}_",
        "",
        "| Category | Total | Deployed | In Stock | Returned | Defective | Defect Rate | Health |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for _, c in cpe.iterrows():
        lines.append(
            f"| {c['equipment_category']} | {c['total_devices']:,} | {c['deployed']:,} | "
            f"{c['in_stock']:,} | {c['returned']:,} | {c['defective']:,} | "
            f"{c['defect_rate_pct']}% | {c['defect_health']} |"
        )
    (CONTEXT_DIR / "cpe_lifecycle.md").write_text("\n".join(lines), encoding="utf-8")
    print("  Written: context/cpe_lifecycle.md")

    lines = [
        "# Data Quality",
        f"_Generated: {TIMESTAMP}_",
        "",
        "DQ checks are enforced via `dbt test` (8 custom singular tests).",
        "Run `dbt test` to see current results.",
        "",
        "Tests:",
        "- assert_po_has_vendor",
        "- assert_equipment_has_material",
        "- assert_no_negative_lead_time",
        "- assert_no_negative_stock",
        "- assert_invoice_has_po_reference",
        "- assert_equipment_has_lifecycle",
        "- assert_vendor_concentration_below_60pct",
        "- assert_defect_rate_below_5pct",
    ]
    (CONTEXT_DIR / "data_quality.md").write_text("\n".join(lines), encoding="utf-8")
    print("  Written: context/data_quality.md")

    issues = conn.execute("SELECT * FROM main_seeds.known_issues WHERE status = 'open' ORDER BY priority").fetchdf()
    lines = [
        "# Open Issues",
        f"_Generated: {TIMESTAMP}_",
        "",
    ]
    if issues.empty:
        lines.append("No open issues.")
    else:
        for _, i in issues.iterrows():
            desc = str(i.get('description', '') or '')[:200]
            lines.append(f"- **#{i['id']}** [{i['priority']}] {i['title']} — {desc}")
    (CONTEXT_DIR / "open_issues.md").write_text("\n".join(lines), encoding="utf-8")
    print("  Written: context/open_issues.md")

    # --- Sidecar: record DuckDB input state this export was produced against ---
    # Uses the still-open conn before close, per design §2b / §OQ2 resolution.
    # Build failure propagates before this block, leaving the prior sidecar in
    # place (design §4).
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _sidecar import (
        compute_duckdb_content_hash, compute_duckdb_row_count,
        current_git_head_sha, now_iso_utc, overall_hash_from_inputs,
        write_sidecar,
    )

    _inputs = [
        ("main_knowledge", "knowledge_pipeline_summary"),
        ("main_knowledge", "knowledge_procurement_health"),
        ("main_knowledge", "knowledge_vendor_performance"),
        ("main_knowledge", "knowledge_cpe_lifecycle_metrics"),
        ("main_seeds", "known_issues"),
    ]
    _inputs_payload: dict = {}
    _per_input_hashes: dict[str, str] = {}
    for _schema, _table in _inputs:
        _key = f"{_schema}.{_table}"
        _content_hash = compute_duckdb_content_hash(conn, _schema, _table)
        _row_count = compute_duckdb_row_count(conn, _schema, _table)
        _inputs_payload[_key] = {
            "content_sha256": _content_hash,
            "row_count": _row_count,
        }
        _per_input_hashes[_key] = _content_hash

    conn.close()
    print(f"\nContext export complete: 5 files in {CONTEXT_DIR}")

    write_sidecar("context", {
        "artifact": "context",
        "schema_version": 1,
        "built_at_utc": now_iso_utc(),
        "git_head_sha": current_git_head_sha(),
        "inputs": _inputs_payload,
        "overall_hash": overall_hash_from_inputs(_per_input_hashes),
        "build_duration_sec": round(time.time() - _start, 3),
        "warnings": [],
    })


if __name__ == "__main__":
    main()
