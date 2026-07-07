"""Seed-taxonomy consistency invariant.

The Seeds Catalog's taxonomy lived as a hand-maintained dict and drifted
in both directions (a decommissioned seed stayed cataloged; a live seed
was never added). It is now a seed itself (dbt/seeds/seed_taxonomy.csv),
and this test makes any future drift fail CI on the commit that
introduces it: every seed CSV must have a taxonomy row, and every
taxonomy row must have a seed CSV.
"""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEEDS_DIR = ROOT / "dbt" / "seeds"
TAXONOMY_CSV = SEEDS_DIR / "seed_taxonomy.csv"


def _taxonomy_rows() -> list[dict]:
    with open(TAXONOMY_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_every_seed_csv_has_a_taxonomy_row():
    cataloged = {r["seed_name"] for r in _taxonomy_rows()}
    on_disk = {p.stem for p in SEEDS_DIR.glob("*.csv")}
    missing = sorted(on_disk - cataloged)
    assert not missing, (
        f"Seeds with no taxonomy row (add them to seed_taxonomy.csv): {missing}"
    )


def test_every_taxonomy_row_has_a_seed_csv():
    cataloged = {r["seed_name"] for r in _taxonomy_rows()}
    on_disk = {p.stem for p in SEEDS_DIR.glob("*.csv")}
    stale = sorted(cataloged - on_disk)
    assert not stale, (
        f"Taxonomy rows describing seeds that no longer exist "
        f"(remove from seed_taxonomy.csv): {stale}"
    )


def test_taxonomy_rows_are_complete():
    for r in _taxonomy_rows():
        assert r["category"].strip(), f"{r['seed_name']}: empty category"
        assert r["purpose"].strip(), f"{r['seed_name']}: empty purpose"
