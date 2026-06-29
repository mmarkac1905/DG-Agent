"""Build knowledge wiki from seed CSVs.

Reads dbt/seeds/*.csv and writes focused markdown pages to knowledge/.
Idempotent: running twice produces identical output. Seeds are the only source of truth.

Adapted from Signal Flow project for CPE Procurement Analytics domain.
"""
from __future__ import annotations

import csv
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "dbt" / "seeds"
WIKI_DIR = ROOT / "knowledge"
TIMESTAMP = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# --- KEYWORD MAPS (domain-specific) ---

DATA_PRODUCT_KEYWORDS = {
    "procurement_efficiency": ["procurement", "purchase order", "lead time", "po ", "ekko", "ekpo", "eban", "purchase_req", "narudžbenica", "narudžben"],
    "vendor_scorecard": ["vendor", "supplier", "lfa1", "lfb1", "lifnr", "dobavljač", "on-time delivery", "otd", "defect rate"],
    "cpe_lifecycle": ["cpe", "equipment", "equi", "serial", "device", "router", "ont", "set-top", "stb", "modem", "lifecycle", "installed", "returned", "defective"],
    "inventory_optimization": ["inventory", "stock", "mard", "reorder", "safety stock", "turnover", "warehouse", "storage location", "zaliha", "skladišt"],
    "cost_analysis": ["cost", "price", "invoice", "rbkp", "rseg", "netpr", "netwr", "tco", "margin", "budget", "trošak", "cijena"],
}

SAP_TABLE_KEYWORDS = {
    "purchase_orders": ["ekko", "ekpo", "eket", "ekkn", "ekbe", "purchase order", "narudžbenica", "po header", "po item", "dual_source", "ZMM_DUAL_SOURCE", "cost_center_derive"],
    "goods_receipts": ["mkpf", "mseg", "goods receipt", "primka", "material document", "movement type", "bwart", "serial_check", "auto_equi", "quantity_tolerance", "batch_gr", "ZMM_CPE_SERIAL", "ZMM_AUTO_EQUI", "ZMM_GR_QUANTITY"],
    "materials": ["mara", "makt", "marc", "marm", "mvke", "material master", "material number", "matnr", "materijal"],
    "vendors": ["lfa1", "lfb1", "lfm1", "vendor master", "supplier", "lifnr", "dobavljač", "vendor_eval", "vendor_score", "ZHT_VENDOR_SCORES", "ZHT_VENDOR_SPEND"],
    "inventory": ["mard", "mchb", "mska", "stock", "inventory", "labst", "zaliha", "skladišt", "reorder", "capacity", "material_auth", "transfer_rules", "ZHT_REORDER", "ZHT_PLANT_CAPACITY"],
    "equipment": ["equi", "eqbs", "seri", "ser01", "ser03", "objk", "serial", "equipment", "oprema", "serijski", "status_update", "warranty", "provisioning", "ZMM_CPE_STATUS", "ZHT_WARRANTY", "ZHT_CUST_INSTALL"],
    "invoices": ["rbkp", "rseg", "invoice", "faktur", "verification", "three_way_match", "ZMM_INVOICE", "tolerance"],
    "accounting": ["bkpf", "bseg", "accounting", "posting", "knjiženje", "fi document"],
    "purchase_requisitions": ["eban", "ebkn", "requisition", "zahtjev"],
    "org_structure": ["t001", "t001w", "t001l", "t024", "company code", "plant", "storage location", "purchasing org"],
}

DV_KEYWORDS = {
    "hub_design": ["hub_", "business key", "golden id", "hub design", "surrogate key", "hub_material", "hub_vendor", "hub_purchase_order", "hub_equipment"],
    "link_design": ["link_", "relationship", "link design", "many-to-many", "link_po_vendor", "link_po_material", "link_gr_po"],
    "satellite_design": ["sat_", "satellite", "hashdiff", "descriptive", "satellite design", "sat_material", "sat_vendor", "sat_po"],
    "naming_conventions": ["naming", "convention", "prefix", "dv standard", "dv naming"],
}

# Maps DV wiki page name -> entity_type value in data_vault_design.csv
DV_PAGE_TO_ENTITY_TYPE = {
    "hub_design": "hub",
    "link_design": "link",
    "satellite_design": "satellite",
}

INFRA_KEYWORDS = {
    "duckdb": ["duckdb", "database", "cpe_analytics.duckdb"],
    "dbt_project": ["dbt", "model", "seed", "test", "macro", "schema.yml", "dbt_project"],
    "pipeline": ["pipeline", "etl", "ingestion", "refresh", "scheduler"],
    "dashboard": ["dashboard", "streamlit", "visualization", "app/"],
}

DOMAIN_KEYWORDS = {
    "procure_to_deploy": ["procure-to-deploy", "procure to deploy", "p2d", "procurement process", "end-to-end", "nabava", "lifecycle"],
    "cpe_lifecycle": ["cpe lifecycle", "device lifecycle", "installed", "returned", "defective", "in-stock", "deployed", "provisioned"],
    "provisioning": ["provisioning", "service activation", "network activation", "dslam", "olt", "port assignment", "ip assignment"],
    "goods_receipt": ["goods receipt", "primka", "gr posting", "migo", "movement type 101", "warehouse receipt"],
}

ALL_KEYWORD_MAPS = {
    "data_products": DATA_PRODUCT_KEYWORDS,
    "sap_tables": SAP_TABLE_KEYWORDS,
    "data_vault": DV_KEYWORDS,
    "infrastructure": INFRA_KEYWORDS,
    "domain": DOMAIN_KEYWORDS,
}

ANTI_PATTERN_MARKERS = [
    "do not", "never ", "no edge", "destructive", "harmful",
    "disabled", "not predictive", "dead", "revert", "failed",
]


def load_csv(name: str) -> list[dict]:
    path = SEED_DIR / f"{name}.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def decision_haystack(d: dict) -> str:
    return " ".join(
        str(d.get(k, "") or "")
        for k in ("decision", "hypothesis", "what_we_did", "what_happened", "conclusion")
    ).lower()


def relationship_haystack(r: dict) -> str:
    return " ".join(
        str(r.get(k, "") or "")
        for k in ("source", "relationship", "target", "evidence_summary", "gotchas")
    ).lower()


def issue_haystack(i: dict) -> str:
    return " ".join(
        str(i.get(k, "") or "") for k in ("title", "description", "resolution")
    ).lower()


def match_any(hay: str, keywords: list[str]) -> bool:
    return any(k.lower() in hay for k in keywords)


def truthy(val: str) -> bool:
    return str(val).strip().lower() in ("true", "1", "yes", "t")


def md_escape(s: str) -> str:
    if s is None:
        return ""
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def header(title: str, subtitle: str | None = None) -> str:
    lines = [f"# {title}", "", f"_Last generated: {TIMESTAMP}_", ""]
    if subtitle:
        lines += [subtitle, ""]
    return "\n".join(lines)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")


def decision_line(d: dict) -> str:
    summary = md_escape(d.get("conclusion") or d.get("what_happened") or d.get("decision"))
    flags = []
    if truthy(d.get("never_repeat")):
        flags.append("NEVER_REPEAT")
    if truthy(d.get("reverted")):
        flags.append("REVERTED")
    flag_str = f" **[{' '.join(flags)}]**" if flags else ""
    date = d.get("date", "")
    return f"- **#{d['id']}** ({date}){flag_str} — {md_escape(d['decision'])}: {summary}"


def relationship_line(r: dict) -> str:
    n = r.get("backtest_n") or ""
    wr = r.get("backtest_wr") or ""
    ret = r.get("backtest_avg_return") or ""
    stats = " · ".join([s for s in [f"N={n}" if n else "", f"WR={wr}%" if wr else "", f"avg={ret}%" if ret else ""] if s])
    evidence = md_escape(r.get("evidence_summary"))
    gotcha = md_escape(r.get("gotchas"))
    parts = [
        f"- **#{r['id']}** `{r['source']}` → `{r['target']}` [{r['relationship']}] — **{r['status']}**"
    ]
    if stats:
        parts.append(f"  - {stats}")
    if evidence:
        parts.append(f"  - Evidence: {evidence}")
    if gotcha:
        parts.append(f"  - Gotcha: {gotcha}")
    return "\n".join(parts)


def issue_line(i: dict) -> str:
    pri = i.get("priority", "")
    status = i.get("status", "")
    desc = md_escape(i.get("description"))
    if len(desc) > 280:
        desc = desc[:280] + "…"
    return f"- **#{i['id']}** [{status}/{pri}] {md_escape(i['title'])} — {desc}"


def filter_by_keywords(rows: list[dict], haystack_fn, keywords: list[str]) -> list[dict]:
    out = []
    for r in rows:
        hay = haystack_fn(r)
        if match_any(hay, keywords):
            out.append(r)
    return sorted(out, key=lambda x: int(x.get("id", 0) or 0))


def build_generic_page(
    title: str,
    keywords: list[str],
    decisions: list[dict],
    relationships: list[dict],
    issues: list[dict],
    extra_sections: str = "",
) -> str:
    related_decisions = filter_by_keywords(decisions, decision_haystack, keywords)
    related_rels = filter_by_keywords(relationships, relationship_haystack, keywords)
    related_issues = [
        i for i in filter_by_keywords(issues, issue_haystack, keywords) if i["status"] == "open"
    ]

    lines = [
        header(title),
        f"Keywords: `{', '.join(keywords)}`",
        "",
    ]

    if extra_sections:
        lines.append(extra_sections)
        lines.append("")

    lines += [f"## Related Decisions ({len(related_decisions)})", ""]
    if related_decisions:
        for d in related_decisions:
            lines.append(decision_line(d))
    else:
        lines.append("_(none)_")

    lines += ["", f"## Related Domain Relationships ({len(related_rels)})", ""]
    if related_rels:
        for r in related_rels:
            lines.append(relationship_line(r))
    else:
        lines.append("_(none)_")

    lines += ["", f"## Open Issues ({len(related_issues)})", ""]
    if related_issues:
        for i in related_issues:
            lines.append(issue_line(i))
    else:
        lines.append("_(none)_")

    # Anti-patterns specific to this page
    anti = [
        d for d in related_decisions
        if truthy(d.get("never_repeat")) or truthy(d.get("reverted")) or match_any(
            (d.get("conclusion") or "").lower(), ANTI_PATTERN_MARKERS
        )
    ]
    if anti:
        lines += ["", "## DO NOT (Anti-patterns)", ""]
        for d in anti:
            lines.append(decision_line(d))

    return "\n".join(lines)


def dv_entity_table(entities: list[dict]) -> str:
    if not entities:
        return "_(no entities of this type designed yet)_"
    lines = [
        "| ID | Name | Business Key | Source Tables | Grain | Notes | Decided |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in sorted(entities, key=lambda x: int(x.get("id", 0) or 0)):
        lines.append(
            "| #{id} | `{name}` | `{bk}` | `{src}` | {grain} | {notes} | {date} |".format(
                id=e.get("id", ""),
                name=md_escape(e.get("entity_name")),
                bk=md_escape(e.get("business_key")),
                src=md_escape(e.get("source_tables")),
                grain=md_escape(e.get("grain")),
                notes=md_escape(e.get("notes")),
                date=md_escape(e.get("decided_date")),
            )
        )
    return "\n".join(lines)


def build_dv_page(
    name: str,
    keywords: list[str],
    dv_entities: list[dict],
    decisions: list[dict],
    relationships: list[dict],
    issues: list[dict],
) -> str:
    entity_type = DV_PAGE_TO_ENTITY_TYPE.get(name)
    matching = [e for e in dv_entities if (e.get("entity_type") or "").lower() == entity_type] if entity_type else []

    extra_lines = []
    if entity_type:
        extra_lines += [
            f"## Designed {entity_type.capitalize()}s ({len(matching)})",
            "",
            dv_entity_table(matching),
            "",
        ]
    extra = "\n".join(extra_lines)

    return build_generic_page(
        f"Data Vault: {name}",
        keywords,
        decisions,
        relationships,
        issues,
        extra_sections=extra,
    )


def build_glossary_page(
    term: dict,
    s2t_rows: list[dict],
    profile_rows: list[dict],
    decisions: list[dict],
    relationships: list[dict],
    issues: list[dict],
) -> str:
    """Build a business glossary wiki page for one term."""
    term_id = term["id"]
    term_name = term["term_name"]

    mappings = [s for s in s2t_rows if s.get("business_term_id") == term_id]
    profiles = [p for p in profile_rows if p.get("business_term_id") == term_id]
    source_profiles = [p for p in profiles if p.get("profile_type") == "source"]
    target_profiles = [p for p in profiles if p.get("profile_type") == "target"]

    keywords = [term_name, term.get("display_name", ""), term_id]
    keywords = [k for k in keywords if k]
    related_decisions = filter_by_keywords(decisions, decision_haystack, keywords)
    related_issues = [
        i for i in filter_by_keywords(issues, issue_haystack, keywords) if i["status"] == "open"
    ]

    related_terms = term.get("related_terms", "")
    related_links = ""
    if related_terms:
        links = [f"[{t.strip()}]({t.strip()}.md)" for t in related_terms.split(";") if t.strip()]
        related_links = " · ".join(links)

    lines = [
        header(f"Business Term: {term.get('display_name', term_name)}"),
        "## Definition",
        "",
        md_escape(term.get("definition", "")),
        "",
        f"- **ID:** `{term_id}`",
        f"- **Owner:** {term.get('owner', 'TBD')}",
        f"- **Approved by:** {term.get('approved_by', 'TBD')}",
        f"- **Status:** `{term.get('status', 'draft')}`",
        f"- **Unit:** {term.get('unit', 'n/a')}",
        f"- **Grain:** {term.get('grain', 'n/a')}",
        f"- **Domain:** {term.get('domain', 'n/a')}",
    ]

    if related_links:
        lines.append(f"- **Related terms:** {related_links}")

    if term.get("notes"):
        lines += ["", f"**Notes:** {md_escape(term['notes'])}"]

    lines += ["", "## Source-to-Target Mapping", ""]

    if mappings:
        lines += ["### Source Tables (SAP)", "", "| Table | Field | Description |", "| --- | --- | --- |"]
        for m in mappings:
            if m.get("source_table") and m.get("source_field"):
                lines.append(f"| {m['source_table']} | {m['source_field']} | {md_escape(m.get('source_description', ''))} |")

        lines += ["", "### Transformation (plain language)", ""]
        step = 1
        for m in mappings:
            logic = (m.get("transformation_logic_plain") or "").strip()
            if logic:
                lines.append(f"{step}. {logic}")
                join_desc = (m.get("join_description") or "").strip()
                if join_desc:
                    lines.append(f"   - *Join:* {join_desc}")
                filter_desc = (m.get("filter_description") or "").strip()
                if filter_desc:
                    lines.append(f"   - *Filter:* {filter_desc}")
                step += 1

        lines += ["", "### SQL (from dbt models)", ""]
        target_models = set()
        for m in mappings:
            tm = m.get("target_model", "")
            tc = m.get("target_column", "")
            sql = (m.get("transformation_logic_sql") or "").strip()
            if tm:
                target_models.add(tm)
            if sql and tc:
                lines.append(f"**{tm}.{tc}:**")
                lines.append("```sql")
                lines.append(sql)
                lines.append("```")
                lines.append("")

        if target_models:
            lines += ["### Target Models", ""]
            for tm in sorted(target_models):
                lines.append(f"- `{tm}`")
    else:
        lines.append("_(no S2T mapping defined yet)_")

    lines += ["", "## Data Profile", ""]

    if source_profiles:
        lines += ["### Source Profile Config", ""]
        for p in source_profiles:
            lines.append(f"- `{p.get('target_model', '?')}.{p.get('target_column', '?')}` — metrics: {p.get('profile_metrics', 'n/a')}")

    if target_profiles:
        lines += ["", "### Target Profile Config", ""]
        for p in target_profiles:
            alert = (p.get("alert_condition") or "").strip()
            lines.append(f"- `{p.get('target_model', '?')}.{p.get('target_column', '?')}` — metrics: {p.get('profile_metrics', 'n/a')}")
            if alert:
                lines.append(f"  - Alert: `{alert}` -> {md_escape(p.get('alert_message', ''))}")

    if not source_profiles and not target_profiles:
        lines.append("_(no profiling configured yet — will be computed after sample data generation)_")

    lines.append("")
    lines.append("### Live Profile Stats")
    lines.append("")
    lines.append("_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_")

    lines += ["", "## Data Vault Lineage", ""]
    if mappings:
        lines.append("See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.")
        lines.append("")
        lines.append("Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**")
    else:
        lines.append("_(lineage will be documented when dbt models are built)_")

    lines += ["", "## Validation Status", ""]
    status = term.get("status", "draft")
    if status == "approved":
        lines.append(f"APPROVED — Business owner approved definition ({term.get('created_date', '?')})")
    elif status == "draft":
        lines.append("DRAFT — awaiting business owner approval")
    else:
        lines.append(f"Status: `{status}`")

    if related_decisions:
        lines += ["", f"## Related Decisions ({len(related_decisions)})", ""]
        for d in related_decisions:
            lines.append(decision_line(d))

    if related_issues:
        lines += ["", f"## Open Issues ({len(related_issues)})", ""]
        for i in related_issues:
            lines.append(issue_line(i))

    return "\n".join(lines)


def build_data_product_page(
    name: str,
    config: dict | None,
    decisions: list[dict],
    relationships: list[dict],
    issues: list[dict],
) -> str:
    keywords = DATA_PRODUCT_KEYWORDS.get(name, [name])

    extra = ""
    if config:
        extra = "\n".join([
            "## Configuration",
            "",
            f"- **Status:** `{config.get('status', 'unknown')}`",
            f"- **Scope:** {config.get('asset_scope', 'n/a')}",
            f"- **Timeframe:** {config.get('timeframe', 'n/a')}",
            f"- **Visualization:** {config.get('broker', 'n/a')}",
            "",
        ])

    return build_generic_page(
        f"Data Product: {name}",
        keywords,
        decisions,
        relationships,
        issues,
        extra_sections=extra,
    )


_RULE_RE = re.compile(r"^##\s+RULE\s+(\d+):\s*(.+)$", re.MULTILINE)


def _extract_numbered_rules() -> list[tuple[int, str, str]]:
    """Pull `## RULE N: title` blocks out of knowledge/knowledge_rules.md.

    Returns list of (n, title, first_paragraph). Non-fatal on missing
    file — just returns [].
    """
    path = ROOT / "knowledge" / "knowledge_rules.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    matches = list(_RULE_RE.finditer(text))
    out = []
    for idx, m in enumerate(matches):
        n = int(m.group(1))
        title = m.group(2).strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        first_para = body.split("\n\n", 1)[0].strip() if body else ""
        out.append((n, title, first_para))
    return out


def build_anti_patterns(decisions: list[dict], relationships: list[dict]) -> str:
    anti_decisions = []
    for d in decisions:
        concl = (d.get("conclusion") or "").lower()
        dec = (d.get("decision") or "").lower()
        hay = concl + " " + dec
        if truthy(d.get("never_repeat")) or match_any(hay, ANTI_PATTERN_MARKERS):
            anti_decisions.append(d)

    harmful_rels = [
        r for r in relationships
        if (r.get("status") or "") in {"validated_harmful", "confirmed_not_useful", "confirmed_destructive", "validated_no_edge", "validated_dead", "reverted"}
    ]

    lines = [
        header("Anti-Patterns — DO NOT List", "Scannable in 30 seconds. Check this BEFORE proposing or building anything."),
        "## Modeling decisions that failed",
        "",
    ]
    if anti_decisions:
        for d in sorted(anti_decisions, key=lambda x: int(x["id"])):
            lines.append(decision_line(d))
    else:
        lines.append("_(none yet — project just started)_")

    # Numbered rules come from knowledge_rules.md — single source of truth.
    rules = _extract_numbered_rules()
    if rules:
        lines += ["", "## Numbered rules (see knowledge_rules.md for details)", ""]
        for n, title, first_para in sorted(rules):
            summary = first_para.replace("\n", " ")
            if len(summary) > 220:
                summary = summary[:217] + "..."
            lines.append(f"- **RULE {n}** — {title}: {summary}")

    lines += ["", f"## Harmful domain relationships ({len(harmful_rels)})", ""]
    if harmful_rels:
        for r in sorted(harmful_rels, key=lambda x: int(x["id"])):
            lines.append(relationship_line(r))
    else:
        lines.append("_(none yet)_")

    return "\n".join(lines)


REMINDER_DATE_RE = re.compile(r"REMINDER\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def build_reminders(issues: list[dict]) -> str:
    today = datetime.now().date()
    rows = []
    for i in issues:
        if i.get("status") != "open":
            continue
        title = i.get("title", "")
        m = REMINDER_DATE_RE.search(title)
        if not m:
            continue
        try:
            due = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        rows.append((due, i))
    rows.sort(key=lambda x: x[0])

    lines = [
        header("Reminders", "Open issues tagged with `REMINDER YYYY-MM-DD`, sorted by date."),
        "| Due | Status | ID | Title |",
        "| --- | --- | --- | --- |",
    ]
    for due, i in rows:
        status = "**OVERDUE**" if due <= today else "pending"
        lines.append(f"| {due} | {status} | #{i['id']} | {md_escape(i['title'])} |")
    if not rows:
        lines.append("_(no reminders)_")
    return "\n".join(lines)


def build_abap_overview(abap_catalog: list[dict], z_tables: list[dict]) -> str:
    lines = [
        header(
            "ABAP Custom Code Catalog",
            "Custom ABAP programs, user exits, BAdIs, and enhancements in HT's SAP system.\n"
            "In a real engagement, this is auto-populated by Claude scanning exported ABAP source code.",
        ),
        "## Programs by Risk Level",
        "",
    ]

    for risk in ["critical", "high", "medium", "low"]:
        programs = [a for a in abap_catalog if (a.get("risk_level") or "").lower() == risk]
        if programs:
            lines.append(f"### {risk.upper()} ({len(programs)})")
            lines.append("")
            for a in programs:
                tables_r = a.get("tables_read", "") or ""
                tables_w = a.get("tables_written", "") or ""
                desc = (a.get("description") or "")[:120]
                rule = (a.get("business_rule_plain") or "")[:200]
                lines.append(f"- **{a['id']}** `{a['program_name']}` ({a['program_type']}) — {md_escape(desc)}")
                lines.append(f"  - Reads: `{tables_r}` | Writes: `{tables_w}`")
                lines.append(f"  - Rule: {md_escape(rule)}")
                lines.append("")

    lines += ["", "## Z-Tables (Custom Tables)", ""]
    if z_tables:
        lines.append("| Table | Description | Maintained by | Rows | Referenced by |")
        lines.append("| --- | --- | --- | --- | --- |")
        for z in z_tables:
            desc = (z.get("description") or "")[:60]
            lines.append(
                f"| `{z['table_name']}` | {md_escape(desc)} | {md_escape(z.get('maintained_by', ''))} | ~{z.get('rows_estimate', '?')} | {md_escape(z.get('referenced_by_programs', ''))} |"
            )

    lines += [
        "",
        "## Table Dependency Graph",
        "",
        "Which ABAP programs read/write which tables:",
        "",
    ]
    table_readers: dict[str, list[str]] = {}
    table_writers: dict[str, list[str]] = {}
    for a in abap_catalog:
        for t in (a.get("tables_read", "") or "").split(";"):
            t = t.strip()
            if t:
                table_readers.setdefault(t, []).append(a["id"])
        for t in (a.get("tables_written", "") or "").split(";"):
            t = t.strip()
            if t:
                table_writers.setdefault(t, []).append(a["id"])

    all_tables = sorted(set(list(table_readers.keys()) + list(table_writers.keys())))
    lines.append("| Table | Read by | Written by |")
    lines.append("| --- | --- | --- |")
    for t in all_tables:
        readers = ", ".join(table_readers.get(t, []))
        writers = ", ".join(table_writers.get(t, []))
        lines.append(f"| `{t}` | {readers} | {writers} |")

    return "\n".join(lines)


def build_index(
    configs: list[dict],
    decisions: list[dict],
    issues: list[dict],
    relationships: list[dict],
    dv_entities: list[dict],
    glossary: list[dict],
    abap_catalog: list[dict],
    z_tables: list[dict],
) -> str:
    open_issues = [i for i in issues if i["status"] == "open"]
    latest_version = max((d.get("version", "") for d in decisions), default="v0.1.0")

    hub_n = sum(1 for e in dv_entities if (e.get("entity_type") or "").lower() == "hub")
    link_n = sum(1 for e in dv_entities if (e.get("entity_type") or "").lower() == "link")
    sat_n = sum(1 for e in dv_entities if (e.get("entity_type") or "").lower() == "satellite")
    contracts = load_csv("data_contracts")
    daily_contracts = sum(1 for c in contracts if c.get("extraction_frequency") == "daily")
    weekly_contracts = sum(1 for c in contracts if c.get("extraction_frequency") == "weekly")

    model_catalog = load_csv("dbt_model_catalog")
    column_lineage = load_csv("dbt_column_lineage")

    model_lines = []
    if model_catalog:
        layer_counts: dict = {}
        for m in model_catalog:
            layer = m.get("layer", "other")
            layer_counts[layer] = layer_counts.get(layer, 0) + 1
        layer_str = ", ".join(f"{l}: {c}" for l, c in sorted(layer_counts.items()))
        model_lines.append(
            f"- **dbt models scanned:** {len(model_catalog)} ({layer_str})"
        )
    if column_lineage:
        model_lines.append(
            f"- **Column lineage tracked:** {len(column_lineage)} columns across all layers"
        )

    lines = [
        header(
            "CPE Procurement Analytics — Knowledge Wiki",
            "Auto-generated from `dbt/seeds/` — do not edit files in this directory by hand.",
        ),
        "## System Snapshot",
        "",
        f"- **Latest version (decisions seed):** `{latest_version}`",
        f"- **Total decisions:** {len(decisions)}",
        f"- **Total domain relationships:** {len(relationships)}",
        f"- **Total known issues:** {len(issues)} ({len(open_issues)} open)",
        f"- **Data products tracked:** {len(configs)}",
        f"- **Data Vault entities designed:** {len(dv_entities)} ({hub_n} hubs · {link_n} links · {sat_n} satellites)",
        f"- **Business glossary terms:** {len(glossary)} ({sum(1 for g in glossary if g.get('status') == 'approved')} approved · {sum(1 for g in glossary if g.get('status') == 'draft')} draft)",
        f"- **ABAP custom programs:** {len(abap_catalog)} documented ({sum(1 for a in abap_catalog if a.get('risk_level') == 'critical')} critical · {sum(1 for a in abap_catalog if a.get('risk_level') == 'high')} high risk) · **Z-tables:** {len(z_tables)}",
        f"- **Data contracts:** {len(contracts)} ({daily_contracts} daily · {weekly_contracts} weekly)",
        *model_lines,
        "",
        "## Data Products (Analytical Use Cases)",
        "",
    ]
    for c in configs:
        lines.append(f"- [{c['strategy']}](data_products/{c['strategy']}.md) — {c.get('status', '?')} · {c.get('asset_scope', 'n/a')}")

    # Business Glossary
    lines += ["", "## Business Glossary", ""]
    approved = [g for g in glossary if g.get("status") == "approved"]
    draft = [g for g in glossary if g.get("status") == "draft"]
    if approved:
        lines.append("### Approved Terms")
        for g in approved:
            lines.append(f"- [{g.get('display_name', g['term_name'])}](business_glossary/{g['term_name']}.md) — {g.get('domain', '?')} · {g.get('grain', '?')}")
    if draft:
        lines += ["", "### Draft Terms (awaiting approval)"]
        for g in draft:
            lines.append(f"- [{g.get('display_name', g['term_name'])}](business_glossary/{g['term_name']}.md) — {g.get('domain', '?')} · draft")

    lines += ["", "## SAP Tables", ""]
    for name in SAP_TABLE_KEYWORDS:
        lines.append(f"- [{name}](sap_tables/{name}.md)")

    lines += ["", "## Domain Concepts", ""]
    for name in DOMAIN_KEYWORDS:
        lines.append(f"- [{name}](domain/{name}.md)")

    lines += ["", "## Data Vault Design", ""]
    for name in DV_KEYWORDS:
        lines.append(f"- [{name}](data_vault/{name}.md)")

    lines += ["", "## Infrastructure", ""]
    for name in INFRA_KEYWORDS:
        lines.append(f"- [{name}](infrastructure/{name}.md)")

    # ABAP Custom Code
    lines += ["", "## ABAP Custom Code", ""]
    lines.append("- [overview](abap/overview.md) — all custom programs, Z-tables, dependency graph")
    if abap_catalog:
        critical = [a for a in abap_catalog if a.get("risk_level") == "critical"]
        high = [a for a in abap_catalog if a.get("risk_level") == "high"]
        lines.append(f"- Programs: {len(abap_catalog)} total ({len(critical)} critical, {len(high)} high risk)")
    if z_tables:
        lines.append(f"- Z-Tables: {len(z_tables)} custom tables documented")

    lines += [
        "",
        "## Meta Pages",
        "",
        "- [anti_patterns](anti_patterns.md) — DO NOT list, scannable in 30s",
        "- [reminders](reminders.md) — dated open issues, overdue flagged",
        "",
        "## Usage",
        "",
        "1. Before ANY building, change, or suggestion: read `index.md`, the relevant data product page, and `anti_patterns.md`.",
        "2. If the work touches SAP tables, read the relevant `sap_tables/` page.",
        "3. If the work touches Data Vault design, read the relevant `data_vault/` page.",
        "4. If implementing a business metric, read the `business_glossary/` page — it has the approved definition, S2T mapping, transformation logic, and profiling config.",
        "5. Never duplicate content — edit the seed CSV and rerun `python scripts/build_knowledge_wiki.py`.",
    ]
    return "\n".join(lines)


def main() -> None:
    _start = time.time()
    decisions = load_csv("known_decisions")
    # signal_relationships / strategy_configs / data_profile_config dropped
    # 2026-05-12 — decorative-only seeds with no functional consumers. Empty
    # list defaults keep downstream rendering paths working (empty sections
    # render gracefully) without re-threading every function signature.
    relationships: list[dict] = []
    issues = load_csv("known_issues")
    configs: list[dict] = []
    dv_entities = load_csv("data_vault_design")
    glossary = load_csv("business_glossary")
    s2t = load_csv("s2t_mapping")
    profile_cfgs: list[dict] = []
    abap_catalog = load_csv("abap_logic_catalog")
    z_tables = load_csv("z_tables_catalog")

    # Create directories
    for subdir in ["data_products", "sap_tables", "domain", "data_vault", "infrastructure"]:
        (WIKI_DIR / subdir).mkdir(parents=True, exist_ok=True)

    # Index
    write(WIKI_DIR / "index.md", build_index(configs, decisions, issues, relationships, dv_entities, glossary, abap_catalog, z_tables))

    # Data product pages
    config_map = {c["strategy"]: c for c in configs}
    for name in DATA_PRODUCT_KEYWORDS:
        write(
            WIKI_DIR / "data_products" / f"{name}.md",
            build_data_product_page(name, config_map.get(name), decisions, relationships, issues),
        )

    # SAP table pages
    for name, kws in SAP_TABLE_KEYWORDS.items():
        write(WIKI_DIR / "sap_tables" / f"{name}.md", build_generic_page(f"SAP Tables: {name}", kws, decisions, relationships, issues))

    # Domain concept pages
    for name, kws in DOMAIN_KEYWORDS.items():
        write(WIKI_DIR / "domain" / f"{name}.md", build_generic_page(f"Domain: {name}", kws, decisions, relationships, issues))

    # Data Vault design pages
    for name, kws in DV_KEYWORDS.items():
        write(
            WIKI_DIR / "data_vault" / f"{name}.md",
            build_dv_page(name, kws, dv_entities, decisions, relationships, issues),
        )

    # Infrastructure pages
    for name, kws in INFRA_KEYWORDS.items():
        write(WIKI_DIR / "infrastructure" / f"{name}.md", build_generic_page(f"Infrastructure: {name}", kws, decisions, relationships, issues))

    # Business glossary pages
    (WIKI_DIR / "business_glossary").mkdir(parents=True, exist_ok=True)
    for term in glossary:
        write(
            WIKI_DIR / "business_glossary" / f"{term['term_name']}.md",
            build_glossary_page(term, s2t, profile_cfgs, decisions, relationships, issues),
        )

    # ABAP overview page
    (WIKI_DIR / "abap").mkdir(parents=True, exist_ok=True)
    write(WIKI_DIR / "abap" / "overview.md", build_abap_overview(abap_catalog, z_tables))

    # Meta pages
    write(WIKI_DIR / "anti_patterns.md", build_anti_patterns(decisions, relationships))
    write(WIKI_DIR / "reminders.md", build_reminders(issues))

    total_files = 1 + len(DATA_PRODUCT_KEYWORDS) + len(SAP_TABLE_KEYWORDS) + len(DOMAIN_KEYWORDS) + len(DV_KEYWORDS) + len(INFRA_KEYWORDS) + len(glossary) + 3
    print(f"Wiki built: {total_files} files in {WIKI_DIR}")
    print(f"Source: {len(decisions)} decisions, {len(issues)} issues, {len(glossary)} glossary terms, {len(s2t)} S2T mappings, {len(abap_catalog)} ABAP programs, {len(z_tables)} Z-tables")

    # --- Sidecar: record what input state this build was produced against. ---
    # On build failure the exception propagates before reaching this block, so
    # no sidecar is written and the previous sidecar (last successful build)
    # stays put — per design §4.
    #
    # Scanner seeds (dbt_column_lineage, dbt_model_catalog) are NOT tracked
    # here — they are non-deterministic across runs (per-run timestamp in
    # dbt_model_catalog; plain-description churn in dbt_column_lineage).
    # Instead we track the upstream dbt/models/*.sql tree, which is the
    # stable input the scanner is a derivable function of.
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _sidecar import (
        compute_dbt_sql_tree_hash, compute_file_hash, current_git_head_sha,
        now_iso_utc, overall_hash_from_inputs, write_sidecar,
    )

    _seed_inputs = [
        "known_decisions", "known_issues",
        "data_vault_design", "business_glossary",
        "s2t_mapping", "abap_logic_catalog",
        "z_tables_catalog",
        # Also consumed inside build_index():
        "data_contracts",
    ]
    _inputs_payload: dict = {}
    _per_input_hashes: dict[str, str] = {}
    for _name in _seed_inputs:
        _csv_path = SEED_DIR / f"{_name}.csv"
        _sha = compute_file_hash(_csv_path)
        _stat = _csv_path.stat()
        _mtime_iso = datetime.fromtimestamp(_stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with _csv_path.open("r", encoding="utf-8", newline="") as _f:
            _row_count = sum(1 for _ in csv.DictReader(_f))
        _inputs_payload[f"{_name}.csv"] = {
            "sha256": _sha, "mtime": _mtime_iso, "row_count": _row_count,
        }
        _per_input_hashes[f"{_name}.csv"] = _sha

    # Upstream-stable proxy for the scanner outputs: the dbt model SQL tree.
    _models_dir = Path(__file__).resolve().parent.parent / "dbt" / "models"
    _tree_hash, _tree_paths = compute_dbt_sql_tree_hash(_models_dir)
    _preview = _tree_paths[:10]
    if len(_tree_paths) > 10:
        _preview = _preview + [f"... and {len(_tree_paths) - 10} more"]
    _inputs_payload["dbt_models_sql_tree"] = {
        "tree_hash": _tree_hash,
        "file_count": len(_tree_paths),
        "included_paths": _preview,
    }
    _per_input_hashes["dbt_models_sql_tree"] = _tree_hash

    write_sidecar("wiki", {
        "artifact": "wiki",
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
