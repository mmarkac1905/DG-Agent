"""One-command loader for the Olist second-source demo.

Downloads the public Brazilian E-Commerce dataset (Olist) from a GitHub
mirror and loads all nine tables into the `raw_olist` schema of
cpe_analytics.duckdb — the reproducible first step of the README's
"Pointing it at another source" recipe.

Dataset: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce
(CC BY-NC-SA 4.0, Olist). Mirror used for unauthenticated download:
https://github.com/Ganesh7699/Brazilian-E-Commerce-OList

Usage:
    python scripts/load_olist_source.py               # download + load
    python scripts/load_olist_source.py --skip-download   # reuse cached CSVs

Idempotent: re-running replaces the raw_olist tables. Downloads are
cached under data/olist_source/ (git-ignored).

After loading, continue the recipe:
    python scripts/generate_olist_data_dictionary.py
    cd dbt && dbt seed --select sap_data_dictionary && cd ..
    # then run the analyzers / stages with DG_SOURCE_SCHEMA=raw_olist
    # and build the demo models with DG_ENABLE_OLIST=true
"""
import argparse
import csv
import datetime
import sys
import urllib.request
import zipfile
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "olist_source"
DB = ROOT / "cpe_analytics.duckdb"
SCHEMA = "raw_olist"

MIRROR = "https://raw.githubusercontent.com/Ganesh7699/Brazilian-E-Commerce-OList/main"

# table name in raw_olist -> file in the mirror
FILES: dict[str, str] = {
    "orders": "olist_orders_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "order_payments": "olist_order_payments_dataset.csv",
    "order_reviews": "olist_order_reviews_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "category_translation": "product_category_name_translation.csv",
}
GEO_ZIP = "Geolocation%20Dataset.zip"

# Minimum plausible row counts — a truncated or HTML-error download
# should fail loudly here, not produce a silently-wrong source.
EXPECTED_MIN_ROWS = {
    "orders": 99_000, "order_items": 112_000, "order_payments": 103_000,
    "order_reviews": 99_000, "customers": 99_000, "sellers": 3_000,
    "products": 32_000, "category_translation": 70, "geolocation": 1_000_000,
}


def _download(url: str, dest: Path) -> None:
    print(f"  downloading {dest.name} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "dg-ai-agent-olist-loader"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        f.write(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-download", action="store_true",
                    help="reuse CSVs already in data/olist_source/")
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)

    if not args.skip_download:
        for fn in FILES.values():
            _download(f"{MIRROR}/{fn}", CACHE / fn)
        _download(f"{MIRROR}/{GEO_ZIP}", CACHE / "geolocation.zip")
        with zipfile.ZipFile(CACHE / "geolocation.zip") as z:
            z.extractall(CACHE)

    geo_csvs = sorted(CACHE.rglob("*geolocation*.csv"))
    if not geo_csvs:
        print("ERROR: geolocation CSV not found after extraction", file=sys.stderr)
        return 1

    tables = dict(FILES)
    con = duckdb.connect(str(DB))
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    failures = []
    for table, fn in list(tables.items()) + [("geolocation", None)]:
        path = (geo_csvs[0] if table == "geolocation" else CACHE / fn).as_posix()
        con.execute(
            f"CREATE OR REPLACE TABLE {SCHEMA}.{table} AS "
            f"SELECT * FROM read_csv_auto('{path}', header=true)"
        )
        n = con.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{table}").fetchone()[0]
        ok = n >= EXPECTED_MIN_ROWS[table]
        print(f"  {table:22s} {n:>9,} rows {'OK' if ok else '!! BELOW EXPECTED MINIMUM'}")
        if not ok:
            failures.append(table)
    con.close()

    if failures:
        print(f"\nERROR: suspicious row counts for: {', '.join(failures)} — "
              f"check the downloads in {CACHE}", file=sys.stderr)
        return 1

    # Record the load in ingestion_log so freshness governance sees it,
    # same as every other ingester in the repo.
    _log = ROOT / "dbt" / "seeds" / "ingestion_log.csv"
    try:
        with open(_log, newline="", encoding="utf-8") as f:
            _rows = list(csv.DictReader(f))
            _cols = list(_rows[0].keys())
        _now = datetime.datetime.now(datetime.timezone.utc)
        _stamp = _now.strftime("%Y-%m-%dT%H:%M:%SZ")
        _day = _now.strftime("%Y%m%d")
        _seq = 1 + sum(1 for r in _rows if r["run_id"].startswith(f"ING-{_day}"))
        total_rows = 0
        con = duckdb.connect(str(DB), read_only=True)
        for t in list(FILES) + ["geolocation"]:
            total_rows += con.execute(
                f"SELECT COUNT(*) FROM {SCHEMA}.{t}").fetchone()[0]
        con.close()
        _rows.append({
            "run_id": f"ING-{_day}-{_seq:03d}",
            "started_at_utc": _stamp, "finished_at_utc": _stamp,
            "source_type": "public_dataset_loader",
            "row_count_total": str(total_rows),
            "tables_touched": ",".join(sorted(list(FILES) + ["geolocation"])),
            "trigger_user": "load_olist_source.py",
            "notes": "Olist public dataset loaded into raw_olist (see README: Pointing it at another source).",
        })
        with open(_log, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_cols, lineterminator="\n")
            w.writeheader()
            w.writerows(_rows)
        print(f"ingestion_log: recorded ING-{_day}-{_seq:03d} "
              f"({total_rows:,} rows). Reload with: cd dbt && dbt seed --select ingestion_log")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not record ingestion_log entry: {e}", file=sys.stderr)

    print(f"\nraw_olist loaded into {DB.name}. Next steps:\n"
          "  python scripts/generate_olist_data_dictionary.py\n"
          "  cd dbt && dbt seed --select sap_data_dictionary\n"
          "  DG_ENABLE_OLIST=true  (to build dbt/models/olist)\n"
          "  DG_SOURCE_SCHEMA=raw_olist  (to point the pipeline here)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
