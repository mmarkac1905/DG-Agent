"""
Additive FI accounting-shadow generator — Path 3 infra build, Stage 1b.

Every logistics document in SAP has an FI "shadow" — the journal entry that the
posting engine creates automatically when the document is released. Our synthetic
data only ever had the goods-receipt shadow (BLART='WE'). This script adds the two
missing shadows, keyed to their source documents via AWTYP/AWKEY:

  RE (Rechnungseingang) — vendor-invoice shadow of MM-IV (RBKP/RSEG).
      The COST/buy side: debit GR/IR clearing, credit vendor payables.
      One BKPF per existing RBKP invoice.  AWTYP='RMRP'.

  RV (Faktura)          — billing shadow of SD (VBRK/VBRP).
      The REVENUE/sell side: debit customer AR, credit service revenue + output VAT.
      One BKPF per NON-CANCELLED VBRK billing document.  AWTYP='VBRK'.

This is ADDITIVE: new rows are appended to raw_sap.bkpf / raw_sap.bseg in fresh
BELNR ranges; existing WE rows keep their values untouched. Two nullable columns
(AWTYP, AWKEY) are added to bkpf and populated only on the new rows.

Also writes the governed reference seed dbt/seeds/gl_account_master.csv mapping each
GL account to a flow category (cost / revenue / neutral) so a finance fact can roll
revenue and cost up cleanly.

Run:  python scripts/generate_fi_shadows.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "cpe_analytics.duckdb"
SEED_PATH = ROOT / "dbt" / "seeds" / "gl_account_master.csv"

COMPANY_CODE = "HT00"

# BELNR ranges chosen to not collide with existing WE docs (max 100,031,963)
RE_BELNR_START = 200000001
RV_BELNR_START = 300000001

# --- GL accounts (must agree with gl_account_master seed below) ---
GL_GRIR = "1900000"       # GR/IR clearing (neutral)
GL_VENDOR_AP = "1600000"  # trade payables - vendors (neutral)
GL_CUST_AR = "1410000"    # trade receivables - customers (neutral)
GL_REVENUE = "8000000"    # service revenue - broadband (revenue)
GL_OUTPUT_VAT = "1750000" # output VAT payable (neutral)

GL_ACCOUNT_MASTER = [
    # gl_account, account_name, account_type, flow_category, notes
    ("1400000", "Inventory - CPE stock", "asset", "neutral",
     "Stock valuation debited at goods receipt (WE shadow). Not a P&L account."),
    ("1410000", "Trade receivables - customers", "asset", "neutral",
     "Customer AR debited by the RV billing shadow. Balance-sheet, not margin."),
    ("1600000", "Trade payables - vendors", "liability", "neutral",
     "Vendor AP credited by the RE invoice shadow. Balance-sheet, not margin."),
    ("1750000", "Output VAT payable", "liability", "neutral",
     "VAT collected on service billing (RV shadow). Pass-through, excluded from margin."),
    ("1900000", "GR/IR clearing", "liability", "neutral",
     "Goods-receipt/invoice-receipt clearing. Debited by RE, credited by WE. Nets to zero."),
    ("5000000", "COGS - CPE device cost", "expense", "cost",
     "Categorisation slot for device cost of goods. In this MVP the margin model sources "
     "device cost from procurement (RSEG) amortised over the 24-month contract, not from a "
     "standalone COGS posting."),
    ("8000000", "Service revenue - broadband", "revenue", "revenue",
     "Monthly service subscription revenue credited by the RV billing shadow. The revenue "
     "side of CPE net margin."),
]


def write_seed():
    fields = ["gl_account", "account_name", "account_type", "flow_category", "notes"]
    # validate before opening for write (RULE 37 — no truncate-then-fail)
    rows = [dict(zip(fields, r)) for r in GL_ACCOUNT_MASTER]
    with open(SEED_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {SEED_PATH.relative_to(ROOT)} ({len(rows)} accounts)")


def ensure_awkey_columns(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info('raw_sap.bkpf')").fetchall()]
    if "AWTYP" not in cols:
        conn.execute("ALTER TABLE raw_sap.bkpf ADD COLUMN AWTYP VARCHAR")
    if "AWKEY" not in cols:
        conn.execute("ALTER TABLE raw_sap.bkpf ADD COLUMN AWKEY VARCHAR")


def generate_re(conn):
    """Vendor-invoice (cost) shadow from RBKP/RSEG."""
    rbkp = conn.execute(
        "SELECT BELNR, GJAHR, BUDAT, BLDAT, LIFNR, WAERS, RMWWR, XBLNR, USNAM "
        "FROM raw_sap.rbkp ORDER BY BELNR"
    ).fetchdf()
    rseg = conn.execute(
        "SELECT BELNR, GJAHR, BUZEI, MATNR, WRBTR FROM raw_sap.rseg ORDER BY BELNR, BUZEI"
    ).fetchdf()
    rseg_by_inv = {}
    for r in rseg.itertuples(index=False):
        rseg_by_inv.setdefault((r.BELNR, r.GJAHR), []).append(r)

    bkpf_rows, bseg_rows = [], []
    belnr = RE_BELNR_START
    for inv in rbkp.itertuples(index=False):
        fi_belnr = str(belnr)
        belnr += 1
        gjahr = str(inv.GJAHR)
        bkpf_rows.append({
            "BUKRS": COMPANY_CODE, "BELNR": fi_belnr, "GJAHR": gjahr,
            "BLART": "RE", "BUDAT": inv.BUDAT, "BLDAT": inv.BLDAT,
            "USNAM": inv.USNAM, "XBLNR": inv.XBLNR,
            "BKTXT": f"Vendor invoice {inv.XBLNR}"[:25],
            "AWTYP": "RMRP", "AWKEY": f"{inv.BELNR}{gjahr}",
        })
        line = 1
        # debit GR/IR clearing, one line per invoice item (carries MATNR)
        for it in rseg_by_inv.get((inv.BELNR, inv.GJAHR), []):
            amt = round(float(it.WRBTR), 2)
            bseg_rows.append({
                "BUKRS": COMPANY_CODE, "BELNR": fi_belnr, "GJAHR": gjahr,
                "BUZEI": f"{line:03d}", "BSCHL": "40", "HKONT": GL_GRIR,
                "DMBTR": amt, "WRBTR": amt, "SHKZG": "S", "WAERS": "EUR",
                "MATNR": it.MATNR, "WERKS": "",
            })
            line += 1
        # credit vendor payables (total invoice amount)
        total = round(float(inv.RMWWR), 2)
        bseg_rows.append({
            "BUKRS": COMPANY_CODE, "BELNR": fi_belnr, "GJAHR": gjahr,
            "BUZEI": f"{line:03d}", "BSCHL": "31", "HKONT": GL_VENDOR_AP,
            "DMBTR": total, "WRBTR": total, "SHKZG": "H", "WAERS": "EUR",
            "MATNR": "", "WERKS": "",
        })
    return pd.DataFrame(bkpf_rows), pd.DataFrame(bseg_rows)


def generate_rv(conn):
    """Service-billing (revenue) shadow from non-cancelled VBRK/VBRP."""
    bills = conn.execute(
        """
        SELECT k.VBELN, k.FKDAT, k.NETWR, k.MWSBK,
               p.MATNR, p.WERKS
        FROM raw_sap.vbrk k
        JOIN raw_sap.vbrp p ON k.VBELN = p.VBELN
        WHERE k.FKSTO = ''
        ORDER BY k.VBELN
        """
    ).fetchdf()

    bkpf_rows, bseg_rows = [], []
    belnr = RV_BELNR_START
    for b in bills.itertuples(index=False):
        fi_belnr = str(belnr)
        belnr += 1
        gjahr = str(b.FKDAT)[:4]
        net = round(float(b.NETWR), 2)
        tax = round(float(b.MWSBK), 2)
        gross = round(net + tax, 2)
        bkpf_rows.append({
            "BUKRS": COMPANY_CODE, "BELNR": fi_belnr, "GJAHR": gjahr,
            "BLART": "RV", "BUDAT": b.FKDAT, "BLDAT": b.FKDAT,
            "USNAM": "SD_BILLING", "XBLNR": b.VBELN,
            "BKTXT": f"Service billing {b.VBELN}"[:25],
            "AWTYP": "VBRK", "AWKEY": f"{b.VBELN}{gjahr}",
        })
        # debit customer AR (gross)
        bseg_rows.append({
            "BUKRS": COMPANY_CODE, "BELNR": fi_belnr, "GJAHR": gjahr,
            "BUZEI": "001", "BSCHL": "01", "HKONT": GL_CUST_AR,
            "DMBTR": gross, "WRBTR": gross, "SHKZG": "S", "WAERS": "EUR",
            "MATNR": "", "WERKS": "",
        })
        # credit service revenue (net, carries service-plan MATNR + plant)
        bseg_rows.append({
            "BUKRS": COMPANY_CODE, "BELNR": fi_belnr, "GJAHR": gjahr,
            "BUZEI": "002", "BSCHL": "50", "HKONT": GL_REVENUE,
            "DMBTR": net, "WRBTR": net, "SHKZG": "H", "WAERS": "EUR",
            "MATNR": b.MATNR, "WERKS": b.WERKS,
        })
        # credit output VAT
        bseg_rows.append({
            "BUKRS": COMPANY_CODE, "BELNR": fi_belnr, "GJAHR": gjahr,
            "BUZEI": "003", "BSCHL": "50", "HKONT": GL_OUTPUT_VAT,
            "DMBTR": tax, "WRBTR": tax, "SHKZG": "H", "WAERS": "EUR",
            "MATNR": "", "WERKS": "",
        })
    return pd.DataFrame(bkpf_rows), pd.DataFrame(bseg_rows)


def append(conn, table, df):
    if df.empty:
        return
    conn.register("df_tmp", df)
    conn.execute(f"INSERT INTO {table} BY NAME SELECT * FROM df_tmp")
    conn.unregister("df_tmp")


def generate():
    write_seed()
    conn = duckdb.connect(str(DB_PATH))

    before_bkpf = conn.execute("SELECT COUNT(*) FROM raw_sap.bkpf").fetchone()[0]
    before_bseg = conn.execute("SELECT COUNT(*) FROM raw_sap.bseg").fetchone()[0]

    ensure_awkey_columns(conn)

    re_bkpf, re_bseg = generate_re(conn)
    print(f"RE (vendor-invoice cost shadow):  {len(re_bkpf):,} docs / {len(re_bseg):,} lines")
    rv_bkpf, rv_bseg = generate_rv(conn)
    print(f"RV (service-billing revenue shadow): {len(rv_bkpf):,} docs / {len(rv_bseg):,} lines")

    for df in (re_bkpf, rv_bkpf):
        append(conn, "raw_sap.bkpf", df)
    for df in (re_bseg, rv_bseg):
        append(conn, "raw_sap.bseg", df)

    after_bkpf = conn.execute("SELECT COUNT(*) FROM raw_sap.bkpf").fetchone()[0]
    after_bseg = conn.execute("SELECT COUNT(*) FROM raw_sap.bseg").fetchone()[0]
    dist = conn.execute(
        "SELECT BLART, COUNT(*) FROM raw_sap.bkpf GROUP BY BLART ORDER BY 1"
    ).fetchall()
    rev = conn.execute(
        f"SELECT ROUND(SUM(DMBTR),2) FROM raw_sap.bseg WHERE HKONT='{GL_REVENUE}' AND SHKZG='H'"
    ).fetchone()[0]
    conn.close()

    print(f"\nbkpf: {before_bkpf:,} -> {after_bkpf:,}   bseg: {before_bseg:,} -> {after_bseg:,}")
    print(f"BLART distribution: {dist}")
    print(f"Total revenue posted to {GL_REVENUE}: EUR {rev:,.2f}")
    print("Stage 1b (FI shadows) complete.")


if __name__ == "__main__":
    generate()
