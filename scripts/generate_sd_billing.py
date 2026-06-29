"""
Additive SD (Sales & Distribution) revenue generator — Path 3 infra build, Stage 1a.

Generates the SD operational layer (KNA1, VBAK, VBAP, VBRK, VBRP) on top of the
EXISTING raw_sap data. This script is ADDITIVE: it reads the 27,000 already-deployed
CPE devices (movement type 201) from the live DuckDB and appends only the new SD
tables. It never touches existing raw_sap tables, so the dashboard and all existing
business terms stay byte-identical.

Business model (matches HT's real consumer package):
  - The CPE router is FREE to the customer, bundled into a 24-month service contract.
  - The customer pays a monthly SERVICE subscription (fiber / IPTV / business).
  - The device itself generates no direct revenue — its profitability is judged by
    attributing the customer's service revenue, against the (amortised) device cost.

So billing lines carry a SERVICE-PLAN material; the CPE router links in via the
customer and the device serial:

  VBRP.NETWR (service revenue)
    -> VBRK (billing header, FKDAT = month)
    -> KUNNR (customer)
    -> VBAK (sales order / contract)
    -> VBAP.SERNR (the deployed device serial)
    -> equi.SERGE -> equi.MATNR  (the CPE router model)

Customer<->device is 1:1 (each deployed device = one customer with one subscription)
so service revenue attributes cleanly to a single CPE material.

Run:  python scripts/generate_sd_billing.py
Then: rebuild staging/vault/marts (Stage 1c+).
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

try:
    from faker import Faker
    Faker.seed(42)
    _fake = Faker("hr_HR")
except Exception:  # pragma: no cover - faker optional
    _fake = None

random.seed(42)

DB_PATH = Path(__file__).resolve().parents[1] / "cpe_analytics.duckdb"

DATE_END = date(2026, 3, 31)
CONTRACT_MONTHS = 24
CANCEL_RATE = 0.02          # share of billing docs cancelled (FKSTO='X')
PDV_RATE = 0.25             # Croatian VAT

COMPANY_CODE = "HT00"
SALES_ORG = "HT01"
DISTR_CHANNEL = "10"        # VTWEG — direct
DIVISION = "10"            # SPART — CPE/broadband

# --- service plan reference (ARPU drives per-material margin variation) ---
SERVICE_PLANS = {
    "SVC-FIB-100": {"desc": "Optika 100 Mbit/s",  "arpu": 24.99, "matkl": "SVC-FIB"},
    "SVC-FIB-500": {"desc": "Optika 500 Mbit/s",  "arpu": 34.99, "matkl": "SVC-FIB"},
    "SVC-FIB-1G":  {"desc": "Optika 1 Gbit/s",    "arpu": 44.99, "matkl": "SVC-FIB"},
    "SVC-IPTV":    {"desc": "MAXtv + Internet",   "arpu": 49.99, "matkl": "SVC-TV"},
    "SVC-CABLE":   {"desc": "Kabelski internet",  "arpu": 29.99, "matkl": "SVC-CBL"},
    "SVC-BIZ":     {"desc": "Poslovni internet",  "arpu": 89.99, "matkl": "SVC-BIZ"},
}

HR_CITIES = ["Zagreb", "Split", "Rijeka", "Osijek", "Zadar", "Velika Gorica",
             "Slavonski Brod", "Pula", "Karlovac", "Varaždin", "Šibenik", "Dubrovnik"]


def assign_plan(cpe_matkl: str) -> str:
    if cpe_matkl in ("CPE-RTR", "CPE-ONT"):
        return random.choices(
            ["SVC-FIB-100", "SVC-FIB-500", "SVC-FIB-1G"], weights=[50, 35, 15]
        )[0]
    if cpe_matkl == "CPE-STB":
        return "SVC-IPTV"
    if cpe_matkl == "CPE-MDM":
        return "SVC-CABLE"
    if cpe_matkl == "CPE-SWT":
        return "SVC-BIZ"
    return "SVC-FIB-100"


# --- month helpers ---
def month_floor(d: date) -> date:
    return date(d.year, d.month, 1)


def add_month(d: date, k: int) -> date:
    m = d.month - 1 + k
    y = d.year + m // 12
    return date(y, m % 12 + 1, 1)


def month_end(d: date) -> date:
    return add_month(month_floor(d), 1) - timedelta(days=1)


def sap_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def load_deployed_devices(conn) -> pd.DataFrame:
    """Reconstruct deployed devices from existing raw_sap.

    Serial is parsed from mkpf.BKTXT ('Deploy SN-...'); CPE material from mseg.MATNR
    (cross-checked against equi). Customer-return date (mvt 161) caps the billing
    window per device.
    """
    return conn.execute(
        """
        WITH dep AS (
            SELECT
                REPLACE(m.BKTXT, 'Deploy ', '') AS serial,
                s.MATNR AS cpe_matnr,
                s.WERKS AS werks,
                CAST(strptime(m.BUDAT, '%Y%m%d') AS DATE) AS deploy_date
            FROM raw_sap.mseg s
            JOIN raw_sap.mkpf m ON s.MBLNR = m.MBLNR AND s.MJAHR = m.MJAHR
            WHERE s.BWART = '201'
        ),
        ret AS (
            SELECT
                REPLACE(m.BKTXT, 'Customer return ', '') AS serial,
                MIN(CAST(strptime(m.BUDAT, '%Y%m%d') AS DATE)) AS return_date
            FROM raw_sap.mseg s
            JOIN raw_sap.mkpf m ON s.MBLNR = m.MBLNR AND s.MJAHR = m.MJAHR
            WHERE s.BWART = '161'
            GROUP BY 1
        )
        SELECT
            dep.serial, dep.cpe_matnr, dep.werks, dep.deploy_date,
            ma.MATKL AS cpe_matkl, ret.return_date
        FROM dep
        JOIN raw_sap.mara ma ON dep.cpe_matnr = ma.MATNR
        LEFT JOIN ret ON dep.serial = ret.serial
        ORDER BY dep.serial
        """
    ).fetchdf()


def generate():
    conn = duckdb.connect(str(DB_PATH))
    devices = load_deployed_devices(conn)
    print(f"Deployed devices read from live DB: {len(devices):,}")

    kna1_rows, vbak_rows, vbap_rows, vbrk_rows, vbrp_rows = [], [], [], [], []

    cust_seq = 1
    so_seq = 1
    bill_seq = 1

    for dev in devices.itertuples(index=False):
        deploy_date = dev.deploy_date
        if isinstance(deploy_date, datetime):
            deploy_date = deploy_date.date()
        return_date = dev.return_date
        if pd.notna(return_date) and isinstance(return_date, datetime):
            return_date = return_date.date()
        elif not (isinstance(return_date, date) and not pd.isna(return_date)):
            return_date = None

        plan_id = assign_plan(dev.cpe_matkl)
        plan = SERVICE_PLANS[plan_id]
        arpu = plan["arpu"]

        is_business = plan_id in ("SVC-BIZ",)
        kunnr = f"{1000000000 + cust_seq}"
        cust_seq += 1
        city = random.choice(HR_CITIES)
        if _fake is not None:
            name = _fake.company() if is_business else _fake.name()
            street = _fake.street_address()
            plz = _fake.postcode()
        else:
            name = f"{'Tvrtka' if is_business else 'Korisnik'} {kunnr}"
            street = f"Ulica {random.randint(1, 200)}"
            plz = f"{random.randint(10000, 53000)}"

        kna1_rows.append({
            "KUNNR": kunnr, "NAME1": name, "LAND1": "HR", "ORT01": city,
            "STRAS": street, "PSTLZ": str(plz),
            "KTOKD": "0002" if is_business else "0001",  # account group: biz / resi
            "ERDAT": sap_date(deploy_date), "SPRAS": "HR",
        })

        # --- sales order (the contract) ---
        vbeln_so = f"00{so_seq:08d}"
        so_seq += 1
        vbak_rows.append({
            "VBELN": vbeln_so, "ERDAT": sap_date(deploy_date),
            "AUDAT": sap_date(deploy_date), "AUART": "TA",
            "VKORG": SALES_ORG, "VTWEG": DISTR_CHANNEL, "SPART": DIVISION,
            "KUNNR": kunnr, "NETWR": round(arpu, 2), "WAERK": "EUR",
        })
        vbap_rows.append({
            "VBELN": vbeln_so, "POSNR": "000010",
            "MATNR": plan_id, "ARKTX": plan["desc"], "MATKL": plan["matkl"],
            "KWMENG": 1.0, "VRKME": "MON", "NETWR": round(arpu, 2),
            "WERKS": dev.werks, "SERNR": dev.serial,          # <-- tie to CPE device
        })

        # --- monthly service billing ---
        start_m = month_floor(deploy_date)
        end_cap = min(add_month(start_m, CONTRACT_MONTHS - 1), month_floor(DATE_END))
        if return_date is not None:
            end_cap = min(end_cap, add_month(month_floor(return_date), -1))

        m = start_m
        while m <= end_cap:
            fkdat = month_end(m)
            net = round(arpu, 2)
            tax = round(net * PDV_RATE, 2)
            cancelled = random.random() < CANCEL_RATE
            vbeln_bill = f"90{bill_seq:08d}"
            bill_seq += 1

            vbrk_rows.append({
                "VBELN": vbeln_bill, "FKART": "F2", "VBTYP": "M",
                "FKDAT": sap_date(fkdat), "ERDAT": sap_date(fkdat),
                "KUNRG": kunnr, "KUNAG": kunnr,
                "NETWR": net, "MWSBK": tax, "WAERK": "EUR",
                "VKORG": SALES_ORG, "FKSTO": "X" if cancelled else "",
                "BUKRS": COMPANY_CODE,
            })
            vbrp_rows.append({
                "VBELN": vbeln_bill, "POSNR": "000010",
                "FKIMG": 1.0, "VRKME": "MON",
                "NETWR": net, "MATNR": plan_id, "ARKTX": plan["desc"],
                "MATKL": plan["matkl"], "WERKS": dev.werks,
                "AUBEL": vbeln_so, "AUPOS": "000010",
                "PRSDT": sap_date(fkdat),
            })
            m = add_month(m, 1)

    tables = {
        "raw_sap.kna1": pd.DataFrame(kna1_rows),
        "raw_sap.vbak": pd.DataFrame(vbak_rows),
        "raw_sap.vbap": pd.DataFrame(vbap_rows),
        "raw_sap.vbrk": pd.DataFrame(vbrk_rows),
        "raw_sap.vbrp": pd.DataFrame(vbrp_rows),
    }

    print(f"\nLoading SD tables into {DB_PATH} (additive — existing tables untouched)...")
    for name, df in tables.items():
        conn.execute(f"DROP TABLE IF EXISTS {name}")
        conn.register("df_tmp", df)
        conn.execute(f"CREATE TABLE {name} AS SELECT * FROM df_tmp")
        conn.unregister("df_tmp")
        print(f"  {name}: {len(df):,} rows")

    # quick revenue sanity rollup
    rev = conn.execute(
        "SELECT ROUND(SUM(NETWR), 2) FROM raw_sap.vbrp "
        "WHERE VBELN IN (SELECT VBELN FROM raw_sap.vbrk WHERE FKSTO = '')"
    ).fetchone()[0]
    conn.close()
    print(f"\nTotal non-cancelled service revenue (VBRP.NETWR): EUR {rev:,.2f}")
    print("Stage 1a (SD operational layer) complete.")


if __name__ == "__main__":
    generate()
