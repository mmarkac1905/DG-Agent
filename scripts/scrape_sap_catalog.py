"""Scrape ~46 SAP table reference pages from sapdatasheet.org into
dbt/seeds/sap_table_catalog.csv at per-table grain.

Phase 1 of C5 catalog (per tasks/c5_design.md Component 1, with calibrated specs
from Q1+Q2: Variant C scope, Table Category=TRANSP filter, top 15-20 fields per
table).

Idempotent: re-running updates existing rows in place rather than duplicating.
Polite throttling: 1-2 req/sec, identifying User-Agent header.

Usage:
    python scripts/scrape_sap_catalog.py             # full scrape, write CSV
    python scripts/scrape_sap_catalog.py --dry-run   # scrape + parse, no write
    python scripts/scrape_sap_catalog.py --throttle 1.0   # custom delay
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT_CSV = ROOT / "dbt/seeds/sap_table_catalog.csv"

USER_AGENT = "cpe-procurement-analytics-c5-catalog-scraper/1.0 (research)"
BASE_URL = "https://www.sapdatasheet.org/abap/tabl/{table}.html"
DEFAULT_THROTTLE_SEC = 0.7
TOP_N_FIELDS = 18  # Q1+Q2 calibrated 15-20 range

# Variant C scope per tasks/c5_design.md (recommended in Q1).
VARIANT_C_TABLES: dict[str, list[str]] = {
    "procurement": ["EKKO", "EKPO", "EBAN", "EBKN", "EKBE", "EKET", "EKKN"],
    "materials": ["MARA", "MARC", "MARD", "MARM", "MAKT", "MSEG", "MKPF",
                  "MVKE", "MCHB", "MSKA"],
    "vendor": ["LFA1", "LFB1", "LFM1"],
    "equipment_serial": ["EQUI", "EQBS", "EQUZ", "OBJK", "SER01", "SER02",
                         "SER03", "SERI", "IFLOT", "ILOA"],
    "inventory_warehouse": ["LQUA", "LTAP", "RKPF", "RESB"],
    "accounting": ["BKPF", "BSEG", "RBKP", "RSEG"],
    "org_master": ["T001", "T001W", "T001L", "T024", "T024E", "T023", "T156"],
}

CSV_COLUMNS = [
    "table_name", "module", "table_category",
    "source_release_stamp", "brief_description",
    "key_fields", "brief_field_descriptions",
    "scrape_source", "scrape_date",
]

# Data-bearing DDIC categories. Q2 originally derived a TRANSP-only rule from
# the single ITOB-as-VIEW counter-example; that rule was too narrow — BSEG is
# CLUSTER (rows stored in RFBLG cluster) but is real, queryable, ingestible.
# Broadened in Phase 1 implementation. Excludes VIEW, STRUCT, INTTAB, APPEND,
# GENERIC and other non-data-bearing categories.
DATA_BEARING_CATEGORIES = {"TRANSP", "CLUSTER", "POOL"}

FIELD_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_/]{0,29}$")


def fetch_page(table: str, timeout: float = 30.0) -> str | None:
    url = BASE_URL.format(table=table.lower())
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as e:
        sys.stderr.write(f"[FETCH-ERR] {table}: {e}\n")
        return None
    if resp.status_code == 404:
        sys.stderr.write(f"[404] {table}: not found\n")
        return None
    if resp.status_code != 200:
        sys.stderr.write(f"[HTTP {resp.status_code}] {table}\n")
        return None
    return resp.text


def parse_page(html: str) -> dict | None:
    """Return parsed catalog row dict, or None if non-TRANSP / unparseable."""
    soup = BeautifulSoup(html, "html.parser")

    # JSON-LD for brief description
    desc = ""
    ld = soup.find("script", type="application/ld+json")
    if ld and ld.string:
        try:
            data = json.loads(ld.string)
            desc = (data.get("alternateName") or "").strip()
        except json.JSONDecodeError:
            pass

    # gui-label / gui-field pairs for category, release stamp
    category = ""
    release = ""
    for lbl in soup.find_all("td", class_="sapds-gui-label"):
        label_text = lbl.get_text(strip=True).rstrip(":")
        nxt = lbl.find_next_sibling("td")
        if not nxt:
            continue
        field_text = nxt.get_text(strip=True)
        if label_text == "Table Category":
            category = field_text
        elif label_text == "Last changed by/on":
            nxt2 = nxt.find_next_sibling("td")
            if nxt2:
                stamp = nxt2.get_text(strip=True)
                if re.fullmatch(r"\d{8}", stamp):
                    release = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"

    if category not in DATA_BEARING_CATEGORIES:
        return {"_filtered_category": category}

    # Field anchors → fields
    fields: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    for a in soup.find_all("a", id=lambda x: x and x.startswith("FIELD_")):
        tr = a.find_parent("tr")
        if not tr:
            continue
        cells = [c.get_text(strip=True) for c in tr.find_all("td")]
        if len(cells) < 9:
            continue
        field_name = cells[1]
        # Skip .INCLUDE / non-canonical rows
        if not FIELD_NAME_RE.match(field_name):
            continue
        if field_name in seen_names:
            continue
        seen_names.add(field_name)
        field_desc = cells[8][:80]  # cap each description
        fields.append((field_name, field_desc))
        if len(fields) >= TOP_N_FIELDS:
            break

    return {
        "table_category": category,
        "source_release_stamp": release,
        "brief_description": desc[:200],
        "key_fields": ", ".join(n for n, _ in fields),
        "brief_field_descriptions": json.dumps(
            {n: d for n, d in fields if d}, ensure_ascii=False
        ),
        "_field_count": len(fields),
    }


def load_existing(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {row["table_name"].upper(): row for row in csv.DictReader(f)}


def write_catalog(path: Path, rows: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=CSV_COLUMNS,
            quoting=csv.QUOTE_MINIMAL, lineterminator="\n",
        )
        w.writeheader()
        for t in sorted(rows):
            row = rows[t]
            w.writerow({c: row.get(c, "") for c in CSV_COLUMNS})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape + parse without writing CSV")
    parser.add_argument("--throttle", type=float, default=DEFAULT_THROTTLE_SEC,
                        help="Seconds between requests (default 0.7)")
    args = parser.parse_args()

    existing = load_existing(OUT_CSV)
    today = datetime.date.today().isoformat()
    rows_to_write: dict[str, dict] = dict(existing)

    succeeded: list[str] = []
    skipped: list[tuple[str, str]] = []  # (table, reason)
    failed: list[str] = []

    for module, tables in VARIANT_C_TABLES.items():
        for t in tables:
            sys.stderr.write(f"[{t}] fetching ({module})...\n")
            html = fetch_page(t)
            if html is None:
                failed.append(t)
                time.sleep(args.throttle)
                continue
            parsed = parse_page(html)
            if parsed is None or "_filtered_category" in parsed:
                cat = parsed.get("_filtered_category", "?") if parsed else "parse_fail"
                sys.stderr.write(
                    f"  SKIP: category={cat!r} (not in {sorted(DATA_BEARING_CATEGORIES)})\n"
                )
                skipped.append((t, cat))
                time.sleep(args.throttle)
                continue
            field_count = parsed.pop("_field_count", 0)
            row = {
                "table_name": t,
                "module": module,
                "scrape_source": "sapdatasheet.org",
                "scrape_date": today,
                **parsed,
            }
            rows_to_write[t] = row
            succeeded.append(t)
            sys.stderr.write(
                f"  OK: stamp={parsed['source_release_stamp']!r} "
                f"desc={parsed['brief_description'][:50]!r} "
                f"fields={field_count}\n"
            )
            time.sleep(args.throttle)

    sys.stderr.write("\n" + "=" * 60 + "\n")
    sys.stderr.write("SUMMARY\n")
    sys.stderr.write(f"  succeeded: {len(succeeded)}\n")
    sys.stderr.write(f"  skipped (non-TRANSP): {len(skipped)}\n")
    for t, cat in skipped:
        sys.stderr.write(f"    {t}: category={cat!r}\n")
    sys.stderr.write(f"  failed (404/error):   {len(failed)} {failed}\n")
    sys.stderr.write(
        f"  total processed: {len(succeeded) + len(skipped) + len(failed)}\n"
    )

    if args.dry_run:
        sys.stderr.write("\n[DRY-RUN] Not writing CSV.\n")
        return 0

    write_catalog(OUT_CSV, rows_to_write)
    sys.stderr.write(f"\nWrote {OUT_CSV} ({len(rows_to_write)} rows)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
