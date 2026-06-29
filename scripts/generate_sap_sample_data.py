"""Generate realistic SAP MM sample data for Helios Telecom CPE procurement.

Creates SAP MM tables with referential integrity and loads into DuckDB.
Data covers 2024-01-01 to 2026-03-31 (27 months of procurement activity).

Usage: python scripts/generate_sap_sample_data.py
"""
import duckdb
import pandas as pd
import numpy as np
from faker import Faker
from datetime import datetime, timedelta, date
from pathlib import Path
import random

random.seed(42)
np.random.seed(42)
fake = Faker('hr_HR')
Faker.seed(42)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "cpe_analytics.duckdb"

# --- GLOBAL PARAMETERS ---
DATE_START = date(2024, 1, 1)
DATE_END = date(2026, 3, 31)
NUM_DAYS = (DATE_END - DATE_START).days

NUM_VENDORS = 8
NUM_MATERIALS = 10
NUM_PLANTS = 4
NUM_STORAGE_LOCS = 7
NUM_PURCHASE_REQS = 2500
NUM_PURCHASE_ORDERS = 2200
NUM_PO_ITEMS = 6500
NUM_GOODS_RECEIPTS = 5500
NUM_INVOICES = 2000
NUM_EQUIPMENT = 45000
NUM_STOCK_SNAPSHOTS = 7
NUM_RESERVATIONS = 800

# --- REFERENCE DATA ---

VENDORS = [
    {"LIFNR": "0000100001", "NAME1": "Huawei Technologies", "LAND1": "CN", "ORT01": "Shenzhen", "STRAS": "Bantian, Longgang District", "lead_time": 45, "quality": "A",  "payment": "NET60", "equipment_types": "router;ont;switch",   "contract_status": "active",   "notes": "Primary CPE supplier for FTTH"},
    {"LIFNR": "0000100002", "NAME1": "ZTE Corporation",     "LAND1": "CN", "ORT01": "Shenzhen", "STRAS": "Keji Road South, Hi-Tech Park", "lead_time": 40, "quality": "B",  "payment": "NET45", "equipment_types": "router;ont",          "contract_status": "active",   "notes": "Secondary supplier"},
    {"LIFNR": "0000100003", "NAME1": "Nokia Networks",      "LAND1": "FI", "ORT01": "Espoo",    "STRAS": "Karakaari 7", "lead_time": 30, "quality": "A",  "payment": "NET30", "equipment_types": "ont;switch",          "contract_status": "active",   "notes": "Premium quality higher price"},
    {"LIFNR": "0000100004", "NAME1": "Sagemcom",            "LAND1": "FR", "ORT01": "Rueil-Malmaison", "STRAS": "250 route de l'Empereur", "lead_time": 35, "quality": "B",  "payment": "NET45", "equipment_types": "router;set_top_box",  "contract_status": "active",   "notes": "STB specialist"},
    {"LIFNR": "0000100005", "NAME1": "Technicolor",         "LAND1": "FR", "ORT01": "Paris",    "STRAS": "8-10 Rue du Renard", "lead_time": 50, "quality": "B",  "payment": "NET60", "equipment_types": "set_top_box;router",  "contract_status": "active",   "notes": "Legacy STB supplier"},
    {"LIFNR": "0000100006", "NAME1": "Iskratel",            "LAND1": "SI", "ORT01": "Kranj",    "STRAS": "Ljubljanska cesta 24a", "lead_time": 15, "quality": "A",  "payment": "NET30", "equipment_types": "ont;switch",          "contract_status": "active",   "notes": "Regional supplier short lead times"},
    {"LIFNR": "0000100007", "NAME1": "Cisco Systems",       "LAND1": "US", "ORT01": "San Jose", "STRAS": "170 West Tasman Drive", "lead_time": 25, "quality": "A+", "payment": "NET30", "equipment_types": "switch;router",       "contract_status": "active",   "notes": "Enterprise grade premium price"},
    {"LIFNR": "0000100008", "NAME1": "CommScope (Arris)",   "LAND1": "US", "ORT01": "Suwanee",  "STRAS": "1100 CommScope Place", "lead_time": 40, "quality": "B",  "payment": "NET45", "equipment_types": "modem;router",        "contract_status": "inactive", "notes": "Contract expired 2025"},
]

MATERIALS = [
    {"MATNR": "CPE-RTR-001", "MAKTX": "Home Gateway Router HG8145V5",   "MTART": "HAWA", "MATKL": "CPE-RTR", "MEINS": "ST", "BRGEW": 0.45, "price": 42.50,  "vendor": "0000100001", "lifecycle_months": 48, "notes": "FTTH standard residential router"},
    {"MATNR": "CPE-RTR-002", "MAKTX": "Home Gateway Router HG8245H",    "MTART": "HAWA", "MATKL": "CPE-RTR", "MEINS": "ST", "BRGEW": 0.52, "price": 55.00,  "vendor": "0000100001", "lifecycle_months": 48, "notes": "Advanced residential with WiFi6"},
    {"MATNR": "CPE-RTR-003", "MAKTX": "Enterprise Router AR161",        "MTART": "HAWA", "MATKL": "CPE-RTR", "MEINS": "ST", "BRGEW": 1.20, "price": 185.00, "vendor": "0000100001", "lifecycle_months": 60, "notes": "Business customer router"},
    {"MATNR": "CPE-ONT-001", "MAKTX": "ONT EG8141A5",                   "MTART": "HAWA", "MATKL": "CPE-ONT", "MEINS": "ST", "BRGEW": 0.30, "price": 28.00,  "vendor": "0000100001", "lifecycle_months": 60, "notes": "Basic GPON ONT"},
    {"MATNR": "CPE-ONT-002", "MAKTX": "ONT EG8145X6",                   "MTART": "HAWA", "MATKL": "CPE-ONT", "MEINS": "ST", "BRGEW": 0.35, "price": 65.00,  "vendor": "0000100001", "lifecycle_months": 60, "notes": "XGS-PON ONT"},
    {"MATNR": "CPE-ONT-003", "MAKTX": "ONT G-240W-F",                   "MTART": "HAWA", "MATKL": "CPE-ONT", "MEINS": "ST", "BRGEW": 0.28, "price": 32.00,  "vendor": "0000100003", "lifecycle_months": 60, "notes": "Nokia GPON ONT alternative"},
    {"MATNR": "CPE-STB-001", "MAKTX": "MAG524w3 IPTV Set-Top Box",      "MTART": "HAWA", "MATKL": "CPE-STB", "MEINS": "ST", "BRGEW": 0.25, "price": 38.00,  "vendor": "0000100004", "lifecycle_months": 36, "notes": "Standard IPTV box"},
    {"MATNR": "CPE-STB-002", "MAKTX": "Apple TV 4K (operator edition)", "MTART": "HAWA", "MATKL": "CPE-STB", "MEINS": "ST", "BRGEW": 0.21, "price": 145.00, "vendor": "0000100007", "lifecycle_months": 48, "notes": "Premium IPTV offering"},
    {"MATNR": "CPE-SWT-001", "MAKTX": "S5735-L24T4S-A Switch",          "MTART": "HAWA", "MATKL": "CPE-SWT", "MEINS": "ST", "BRGEW": 3.50, "price": 320.00, "vendor": "0000100001", "lifecycle_months": 72, "notes": "Business L2 switch"},
    {"MATNR": "CPE-MDM-001", "MAKTX": "DOCSIS 3.1 Cable Modem",         "MTART": "HAWA", "MATKL": "CPE-MDM", "MEINS": "ST", "BRGEW": 0.40, "price": 52.00,  "vendor": "0000100008", "lifecycle_months": 48, "notes": "Cable network CPE (legacy)"},
]

MATERIAL_DEMAND_WEIGHTS = {
    "CPE-RTR-001": 0.30,
    "CPE-RTR-002": 0.15,
    "CPE-RTR-003": 0.03,
    "CPE-ONT-001": 0.20,
    "CPE-ONT-002": 0.08,
    "CPE-ONT-003": 0.05,
    "CPE-STB-001": 0.10,
    "CPE-STB-002": 0.02,
    "CPE-SWT-001": 0.02,
    "CPE-MDM-001": 0.05,
}

PLANTS = [
    {"WERKS": "HT10", "NAME1": "Zagreb Central Warehouse", "ORT01": "Zagreb", "LAND1": "HR", "weight": 0.50},
    {"WERKS": "HT20", "NAME1": "Split Regional Warehouse", "ORT01": "Split", "LAND1": "HR", "weight": 0.20},
    {"WERKS": "HT30", "NAME1": "Osijek Regional Warehouse", "ORT01": "Osijek", "LAND1": "HR", "weight": 0.15},
    {"WERKS": "HT40", "NAME1": "Rijeka Regional Warehouse", "ORT01": "Rijeka", "LAND1": "HR", "weight": 0.15},
]

STORAGE_LOCATIONS = [
    {"WERKS": "HT10", "LGORT": "HT11", "LGOBE": "CPE Main Storage"},
    {"WERKS": "HT10", "LGORT": "HT12", "LGOBE": "CPE Defective/Return"},
    {"WERKS": "HT10", "LGORT": "HT13", "LGOBE": "CPE Ready for Deploy"},
    {"WERKS": "HT20", "LGORT": "HT21", "LGOBE": "CPE Regional Storage"},
    {"WERKS": "HT20", "LGORT": "HT22", "LGOBE": "CPE Regional Returns"},
    {"WERKS": "HT30", "LGORT": "HT31", "LGOBE": "CPE Regional Storage"},
    {"WERKS": "HT40", "LGORT": "HT41", "LGOBE": "CPE Regional Storage"},
]

PURCHASING_GROUPS = ["001", "002", "003", "004"]
PURCHASING_ORG = "HT01"
COMPANY_CODE = "HT00"

MOVEMENT_TYPES = {
    # SAP-native fields per real T156:
    # - BWARK = movement category ('A' receipt, 'B' issue, 'X' transfer)
    # - KZBEW = movement indicator ('B' GR-for-PO, 'F' consumption, 'L'
    #   stock-transport, 'A' receipt-without-ref, 'X' initial)
    # - SHKZG = debit/credit ('S' stock-account debit/increase, 'H' credit/
    #   decrease) — semantics inverted from raw SAP in this synthetic
    #   project; canonical direction derivation uses BWARK.
    # 'text_en' + 'text_hr' move to T156T (the SAP text table) — synthetic
    # weight kept here for the generators that pick by frequency.
    "101": {"weight": 0.55, "BWARK": "A", "KZBEW": "B", "SHKZG": "S",
            "text_en": "Goods Receipt for PO",   "text_hr": "Primka po narudžbenici"},
    "102": {"weight": 0.03, "BWARK": "A", "KZBEW": "B", "SHKZG": "H",
            "text_en": "GR Reversal for PO",     "text_hr": "Storno primke po narudžbenici"},
    "122": {"weight": 0.04, "BWARK": "B", "KZBEW": "B", "SHKZG": "H",
            "text_en": "Return to Vendor",        "text_hr": "Povrat dobavljaču"},
    "161": {"weight": 0.08, "BWARK": "A", "KZBEW": "A", "SHKZG": "S",
            "text_en": "Return from Customer",    "text_hr": "Povrat od korisnika"},
    "201": {"weight": 0.22, "BWARK": "B", "KZBEW": "F", "SHKZG": "H",
            "text_en": "Goods Issue for Cost Center",   "text_hr": "Izdavanje na mjesto troška"},
    "202": {"weight": 0.02, "BWARK": "B", "KZBEW": "F", "SHKZG": "S",
            "text_en": "GI Reversal for Cost Center",   "text_hr": "Storno izdavanja na mjesto troška"},
    "301": {"weight": 0.04, "BWARK": "X", "KZBEW": "L", "SHKZG": "H",
            "text_en": "Plant to Plant Transfer", "text_hr": "Transfer između pogona"},
    "561": {"weight": 0.02, "BWARK": "A", "KZBEW": "X", "SHKZG": "S",
            "text_en": "Initial Stock Posting",   "text_hr": "Početno stanje zaliha"},
}

SAP_USERS = ["MMARKAC", "IPERIC", "TNOVAK", "JKOVIC", "ABANIC", "DVUKOVIC", "MTOMIC", "ZKRALJ"]

MATKL_TO_EKGRP = {"CPE-RTR": "001", "CPE-ONT": "001", "CPE-STB": "002", "CPE-SWT": "003", "CPE-MDM": "004"}


# --- HELPERS ---

def random_date(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, max(0, delta)))


def random_date_after(ref_date: date, min_days: int, max_days: int) -> date:
    days = random.randint(min_days, max_days)
    result = ref_date + timedelta(days=days)
    return min(result, DATE_END)


def weighted_choice(items: list, weights: list):
    return random.choices(items, weights=weights, k=1)[0]


def generate_serial(prefix: str, index: int) -> str:
    return f"SN-{prefix}-{index:06d}"


def format_sap_date(d) -> str:
    if d is None:
        return ""
    return d.strftime("%Y%m%d")


def sap_number(prefix: str, num: int, width: int = 10) -> str:
    return f"{prefix}{num:0{width - len(prefix)}d}"


# --- GENERATORS ---

def generate_org_tables():
    t001 = pd.DataFrame([{
        "BUKRS": COMPANY_CODE, "BUTXT": "Helios Telecom d.d.",
        "ORT01": "Zagreb", "LAND1": "HR", "WAERS": "EUR", "SPRAS": "H"
    }])

    t001w = pd.DataFrame([{
        "WERKS": p["WERKS"], "NAME1": p["NAME1"], "ORT01": p["ORT01"],
        "LAND1": p["LAND1"], "BUKRS": COMPANY_CODE
    } for p in PLANTS])

    t001l = pd.DataFrame([{
        "WERKS": s["WERKS"], "LGORT": s["LGORT"], "LGOBE": s["LGOBE"]
    } for s in STORAGE_LOCATIONS])

    t024 = pd.DataFrame([
        {"EKGRP": "001", "EKNAM": "CPE Routers & ONTs"},
        {"EKGRP": "002", "EKNAM": "CPE Set-Top Boxes"},
        {"EKGRP": "003", "EKNAM": "CPE Network Equipment"},
        {"EKGRP": "004", "EKNAM": "CPE Legacy & Other"},
    ])

    t024e = pd.DataFrame([{
        "EKORG": PURCHASING_ORG, "EKOTX": "HT Procurement",
        "BUKRS": COMPANY_CODE, "LAND1": "HR"
    }])

    t023 = pd.DataFrame([
        {"MATKL": "CPE-RTR", "WGBEZ": "CPE Routers"},
        {"MATKL": "CPE-ONT", "WGBEZ": "CPE ONT Devices"},
        {"MATKL": "CPE-STB", "WGBEZ": "CPE Set-Top Boxes"},
        {"MATKL": "CPE-SWT", "WGBEZ": "CPE Switches"},
        {"MATKL": "CPE-MDM", "WGBEZ": "CPE Modems"},
    ])

    # T156 — movement-type config (BWARK + KZBEW + SHKZG per real SAP).
    # BTEXT is intentionally NOT in T156; SAP keeps that in T156T below.
    t156 = pd.DataFrame([
        {"BWART": k, "BWARK": v["BWARK"], "KZBEW": v["KZBEW"], "SHKZG": v["SHKZG"]}
        for k, v in MOVEMENT_TYPES.items()
    ])

    # T156T — movement-type text table. One row per (BWART, SPRAS).
    # SPRAS uses SAP's 1-char language codes: E=English, H=Croatian.
    t156t_rows = []
    for k, v in MOVEMENT_TYPES.items():
        t156t_rows.append({"BWART": k, "SPRAS": "E", "BTEXT": v["text_en"], "LTEXT": v["text_en"]})
        t156t_rows.append({"BWART": k, "SPRAS": "H", "BTEXT": v["text_hr"], "LTEXT": v["text_hr"]})
    t156t = pd.DataFrame(t156t_rows)

    return t001, t001w, t001l, t024, t024e, t023, t156, t156t


def generate_vendor_tables():
    lfa1_rows = []
    lfb1_rows = []
    lfm1_rows = []
    zmm_vendor_business_rows = []  # HT-domain Z-table extension keyed on LIFNR

    for v in VENDORS:
        lfa1_rows.append({
            "LIFNR": v["LIFNR"], "NAME1": v["NAME1"], "LAND1": v["LAND1"],
            "ORT01": v["ORT01"], "STRAS": v["STRAS"],
            "TELF1": fake.phone_number()[:16],
            "ADRNR": f"ADR{v['LIFNR'][-4:]}",
            "ERDAT": format_sap_date(DATE_START - timedelta(days=random.randint(365, 1000))),
            "ERNAM": "ADMIN",
        })
        lfb1_rows.append({
            "LIFNR": v["LIFNR"], "BUKRS": COMPANY_CODE,
            "ZTERM": v["payment"],
            "AKONT": "3100000",
            "ZWELS": "T",
            "FDGRV": "01",
        })
        lfm1_rows.append({
            "LIFNR": v["LIFNR"], "EKORG": PURCHASING_ORG,
            "WAERS": "EUR",
            "ZTERM": v["payment"],
            "WEBRE": "X",
            "LEBRE": "X",
        })
        zmm_vendor_business_rows.append({
            "LIFNR": v["LIFNR"],
            "EQUIPMENT_TYPES": v["equipment_types"],
            "CONTRACT_STATUS": v["contract_status"],
            "QUALITY_RATING": v["quality"],
            "NOTES": v["notes"],
        })

    return (
        pd.DataFrame(lfa1_rows),
        pd.DataFrame(lfb1_rows),
        pd.DataFrame(lfm1_rows),
        pd.DataFrame(zmm_vendor_business_rows),
    )


def generate_material_tables():
    mara_rows = []
    makt_rows = []
    marc_rows = []
    marm_rows = []
    zmm_material_business_rows = []  # HT-domain Z-table extension keyed on MATNR

    for m in MATERIALS:
        mara_rows.append({
            "MATNR": m["MATNR"], "MTART": m["MTART"], "MATKL": m["MATKL"],
            "MEINS": m["MEINS"], "BRGEW": m["BRGEW"], "GEWEI": "KG",
            "MSTAE": "01",
            "ERDAT": format_sap_date(DATE_START - timedelta(days=random.randint(30, 365))),
            "ERNAM": "ADMIN", "SPART": "01",
            "PRDHA": f"CPE/{m['MATKL']}/{m['MATNR']}",
        })
        makt_rows.append({"MATNR": m["MATNR"], "SPRAS": "E", "MAKTX": m["MAKTX"]})
        makt_rows.append({"MATNR": m["MATNR"], "SPRAS": "H", "MAKTX": m["MAKTX"]})
        for p in PLANTS:
            plant_slocs = [s["LGORT"] for s in STORAGE_LOCATIONS if s["WERKS"] == p["WERKS"]]
            marc_rows.append({
                "MATNR": m["MATNR"], "WERKS": p["WERKS"],
                "DISMM": "VB", "DISPO": "001",
                "EKGRP": MATKL_TO_EKGRP[m["MATKL"]],
                "BESKZ": "F", "SOBSL": "",
                "LGPRO": plant_slocs[0],
                "PLIFZ": str(next(v["lead_time"] for v in VENDORS if v["LIFNR"] == m["vendor"])),
                "SERNP": "HT01",
            })
        marm_rows.append({
            "MATNR": m["MATNR"], "MEINH": m["MEINS"], "UMREZ": 1, "UMREN": 1
        })
        zmm_material_business_rows.append({
            "MATNR": m["MATNR"],
            "LIFECYCLE_MONTHS": m["lifecycle_months"],
            "PRIMARY_VENDOR_ID": m["vendor"],
            "NOTES": m["notes"],
        })

    return (
        pd.DataFrame(mara_rows),
        pd.DataFrame(makt_rows),
        pd.DataFrame(marc_rows),
        pd.DataFrame(marm_rows),
        pd.DataFrame(),
        pd.DataFrame(zmm_material_business_rows),
    )


def generate_purchase_requisitions():
    eban_rows = []
    ebkn_rows = []

    mat_list = list(MATERIAL_DEMAND_WEIGHTS.keys())
    mat_weights = list(MATERIAL_DEMAND_WEIGHTS.values())

    for i in range(1, NUM_PURCHASE_REQS + 1):
        banfn = sap_number("10", i)
        bnfpo = "00010"
        mat = weighted_choice(mat_list, mat_weights)
        mat_info = next(m for m in MATERIALS if m["MATNR"] == mat)
        plant = weighted_choice(PLANTS, [p["weight"] for p in PLANTS])
        plant_sloc = [s["LGORT"] for s in STORAGE_LOCATIONS if s["WERKS"] == plant["WERKS"]][0]
        qty = random.choice([50, 100, 200, 500, 1000]) if mat_info["MATKL"] in ("CPE-RTR", "CPE-ONT") else random.choice([10, 25, 50, 100])
        req_date = random_date(DATE_START, DATE_END - timedelta(days=60))

        eban_rows.append({
            "BANFN": banfn, "BNFPO": bnfpo, "MATNR": mat,
            "WERKS": plant["WERKS"],
            "LGORT": plant_sloc,
            "MENGE": float(qty), "MEINS": mat_info["MEINS"],
            "PREIS": mat_info["price"],
            "BADAT": format_sap_date(req_date),
            "FRGDT": format_sap_date(req_date + timedelta(days=random.randint(1, 5))),
            "ERNAM": random.choice(SAP_USERS),
            "ESTKZ": random.choice(["", "", "", "B"]),
            "STATU": "N" if i > NUM_PURCHASE_ORDERS else "B",
            "EKGRP": MATKL_TO_EKGRP[mat_info["MATKL"]],
            "EKORG": PURCHASING_ORG,
        })
        ebkn_rows.append({
            "BANFN": banfn, "BNFPO": bnfpo,
            "SAKTO": "6300100",
            "KOSTL": f"CC-{plant['WERKS']}",
        })

    return pd.DataFrame(eban_rows), pd.DataFrame(ebkn_rows)


def generate_purchase_orders(eban_df):
    converted_prs = eban_df[eban_df["STATU"] == "B"].to_dict("records")

    ekko_rows = []
    ekpo_rows = []
    eket_rows = []
    ekkn_rows = []

    po_number = 4500000001

    for pr in converted_prs:
        mat_info = next(m for m in MATERIALS if m["MATNR"] == pr["MATNR"])
        vendor = next(v for v in VENDORS if v["LIFNR"] == mat_info["vendor"])

        if random.random() < 0.15:
            alt_vendors = [v for v in VENDORS if v["LIFNR"] != mat_info["vendor"] and v["quality"] in ("A", "B")]
            if alt_vendors:
                vendor = random.choice(alt_vendors)

        pr_date = datetime.strptime(pr["BADAT"], "%Y%m%d").date()
        po_date = pr_date + timedelta(days=random.randint(1, 7))
        delivery_date = po_date + timedelta(days=vendor["lead_time"] + random.randint(-5, 10))

        ebeln = str(po_number)
        po_number += 1
        ebelp = "00010"

        unit_price = round(mat_info["price"] * (1 + random.uniform(-0.05, 0.05)), 2)
        qty = pr["MENGE"]
        net_value = round(unit_price * qty, 2)

        ekko_rows.append({
            "EBELN": ebeln, "BUKRS": COMPANY_CODE,
            "BSTYP": "F", "BSART": "NB",
            "LIFNR": vendor["LIFNR"],
            "EKORG": PURCHASING_ORG,
            "EKGRP": pr["EKGRP"],
            "BEDAT": format_sap_date(po_date),
            "KDATB": format_sap_date(po_date),
            "KDATE": format_sap_date(po_date + timedelta(days=365)),
            "WAERS": "EUR",
            "WKURS": 1.0,
            "ERNAM": random.choice(SAP_USERS),
            "AEDAT": format_sap_date(po_date + timedelta(days=random.randint(0, 3))),
            "PROCSTAT": "05",
            "RLWRT": net_value,
            "BANFN": pr["BANFN"],
        })

        ekpo_rows.append({
            "EBELN": ebeln, "EBELP": ebelp,
            "MATNR": pr["MATNR"],
            "TXZ01": mat_info["MAKTX"][:40],
            "MENGE": qty, "MEINS": mat_info["MEINS"],
            "NETPR": unit_price, "NETWR": net_value,
            "WERKS": pr["WERKS"],
            "LGORT": pr["LGORT"],
            "MATKL": mat_info["MATKL"],
            "PSTYP": "0",
            "BANFN": pr["BANFN"], "BNFPO": pr["BNFPO"],
            "BPRME": mat_info["MEINS"],
            "ELIKZ": "",
        })

        eket_rows.append({
            "EBELN": ebeln, "EBELP": ebelp, "ETENR": "0001",
            "EINDT": format_sap_date(delivery_date),
            "MENGE": qty,
            "WEMNG": 0.0,
        })

        ekkn_rows.append({
            "EBELN": ebeln, "EBELP": ebelp, "ZEKKN": "01",
            "SAKTO": "6300100", "KOSTL": f"CC-{pr['WERKS']}",
        })

    return (pd.DataFrame(ekko_rows), pd.DataFrame(ekpo_rows),
            pd.DataFrame(eket_rows), pd.DataFrame(ekkn_rows))


def generate_goods_receipts(ekpo_df, ekko_df):
    mkpf_rows = []
    mseg_rows = []
    ekbe_rows = []

    mblnr_counter = 5000000001

    po_items = ekpo_df.to_dict("records")
    po_headers = {r["EBELN"]: r for r in ekko_df.to_dict("records")}

    all_serials = []
    serial_counter = 1

    for po_item in po_items:
        ebeln = po_item["EBELN"]
        po_header = po_headers[ebeln]
        po_date = datetime.strptime(po_header["BEDAT"], "%Y%m%d").date()
        vendor = next(v for v in VENDORS if v["LIFNR"] == po_header["LIFNR"])

        scenario = random.random()
        if scenario < 0.02:
            continue
        elif scenario < 0.10:
            gr_qty = round(po_item["MENGE"] * random.uniform(0.3, 0.8))
        else:
            gr_qty = po_item["MENGE"]

        lead_time = vendor["lead_time"]
        gr_date = po_date + timedelta(days=lead_time + random.randint(-7, 14))
        if gr_date > DATE_END:
            continue

        mblnr = str(mblnr_counter)
        mblnr_counter += 1

        mkpf_rows.append({
            "MBLNR": mblnr, "MJAHR": str(gr_date.year),
            "BUDAT": format_sap_date(gr_date),
            "BLDAT": format_sap_date(gr_date),
            "USNAM": random.choice(SAP_USERS),
            "BKTXT": f"GR for PO {ebeln}",
            "XBLNR": ebeln,
        })

        mseg_rows.append({
            "MBLNR": mblnr, "MJAHR": str(gr_date.year), "ZEILE": "0001",
            "BWART": "101",
            "MATNR": po_item["MATNR"],
            "WERKS": po_item["WERKS"],
            "LGORT": po_item["LGORT"],
            "MENGE": float(gr_qty), "MEINS": po_item["MEINS"],
            "EBELN": ebeln, "EBELP": po_item["EBELP"],
            "DMBTR": round(float(gr_qty) * float(po_item["NETPR"]), 2),
            "WAERS": "EUR",
            "SERNP": "HT01",
            "LIFNR": po_header["LIFNR"],
        })

        ekbe_rows.append({
            "EBELN": ebeln, "EBELP": po_item["EBELP"],
            "ZEKKN": "01", "VGABE": "1",
            "GJAHR": str(gr_date.year),
            "BELNR": mblnr,
            "BUZEI": "0001",
            "BUDAT": format_sap_date(gr_date),
            "MENGE": float(gr_qty),
            "DMBTR": round(float(gr_qty) * float(po_item["NETPR"]), 2),
            "WAERS": "EUR",
            "BWART": "101",
        })

        if serial_counter <= NUM_EQUIPMENT:
            for s in range(int(gr_qty)):
                if serial_counter > NUM_EQUIPMENT:
                    break
                serial = generate_serial(po_item["MATNR"].split("-")[-1], serial_counter)
                all_serials.append({
                    "serial": serial,
                    "matnr": po_item["MATNR"],
                    "werks": po_item["WERKS"],
                    "lgort": po_item["LGORT"],
                    "gr_date": gr_date,
                    "vendor": po_header["LIFNR"],
                    "mblnr": mblnr,
                    "ebeln": ebeln,
                })
                serial_counter += 1

    # Deployments (mvt 201) — ~60% of serials
    tracked_serials = all_serials[:NUM_EQUIPMENT]
    deployed_serials = random.sample(tracked_serials, min(int(len(tracked_serials) * 0.60), len(tracked_serials)))

    for ser in deployed_serials:
        deploy_date = random_date_after(ser["gr_date"], 1, 60)
        if deploy_date > DATE_END:
            continue

        mblnr = str(mblnr_counter)
        mblnr_counter += 1

        mkpf_rows.append({
            "MBLNR": mblnr, "MJAHR": str(deploy_date.year),
            "BUDAT": format_sap_date(deploy_date),
            "BLDAT": format_sap_date(deploy_date),
            "USNAM": random.choice(SAP_USERS),
            "BKTXT": f"Deploy {ser['serial']}",
            "XBLNR": "",
        })
        mseg_rows.append({
            "MBLNR": mblnr, "MJAHR": str(deploy_date.year), "ZEILE": "0001",
            "BWART": "201", "MATNR": ser["matnr"],
            "WERKS": ser["werks"], "LGORT": ser["lgort"],
            "MENGE": 1.0, "MEINS": "ST",
            "EBELN": "", "EBELP": "",
            "DMBTR": 0.0, "WAERS": "EUR",
            "SERNP": "HT01", "LIFNR": "",
        })
        ser["deployed"] = True
        ser["deploy_date"] = deploy_date

    # Customer returns (mvt 161) — ~8% of deployed
    deployed = [s for s in deployed_serials if s.get("deployed")]
    returned = random.sample(deployed, min(int(len(deployed) * 0.08), len(deployed)))

    for ser in returned:
        return_date = random_date_after(ser.get("deploy_date", ser["gr_date"]), 30, 180)
        if return_date > DATE_END:
            continue

        mblnr = str(mblnr_counter)
        mblnr_counter += 1

        return_slocs = [s["LGORT"] for s in STORAGE_LOCATIONS if s["WERKS"] == ser["werks"] and "Return" in s["LGOBE"]]
        return_lgort = return_slocs[0] if return_slocs else ser["lgort"]

        mkpf_rows.append({
            "MBLNR": mblnr, "MJAHR": str(return_date.year),
            "BUDAT": format_sap_date(return_date),
            "BLDAT": format_sap_date(return_date),
            "USNAM": random.choice(SAP_USERS),
            "BKTXT": f"Customer return {ser['serial']}",
            "XBLNR": "",
        })
        mseg_rows.append({
            "MBLNR": mblnr, "MJAHR": str(return_date.year), "ZEILE": "0001",
            "BWART": "161", "MATNR": ser["matnr"],
            "WERKS": ser["werks"],
            "LGORT": return_lgort,
            "MENGE": 1.0, "MEINS": "ST",
            "EBELN": "", "EBELP": "",
            "DMBTR": 0.0, "WAERS": "EUR",
            "SERNP": "HT01", "LIFNR": "",
        })
        ser["returned"] = True
        ser["return_date"] = return_date

    # Vendor returns (mvt 122) — ~30% of customer returns
    vendor_returns = random.sample(returned, min(int(len(returned) * 0.30), len(returned)))

    for ser in vendor_returns:
        vr_date = random_date_after(ser.get("return_date", ser["gr_date"]), 5, 30)
        if vr_date > DATE_END:
            continue

        mblnr = str(mblnr_counter)
        mblnr_counter += 1

        mkpf_rows.append({
            "MBLNR": mblnr, "MJAHR": str(vr_date.year),
            "BUDAT": format_sap_date(vr_date),
            "BLDAT": format_sap_date(vr_date),
            "USNAM": random.choice(SAP_USERS),
            "BKTXT": f"Vendor return {ser['serial']}",
            "XBLNR": ser["ebeln"],
        })
        mseg_rows.append({
            "MBLNR": mblnr, "MJAHR": str(vr_date.year), "ZEILE": "0001",
            "BWART": "122", "MATNR": ser["matnr"],
            "WERKS": ser["werks"], "LGORT": ser["lgort"],
            "MENGE": 1.0, "MEINS": "ST",
            "EBELN": ser["ebeln"], "EBELP": "00010",
            "DMBTR": 0.0, "WAERS": "EUR",
            "SERNP": "HT01", "LIFNR": ser["vendor"],
        })
        ser["vendor_returned"] = True

    return (pd.DataFrame(mkpf_rows), pd.DataFrame(mseg_rows),
            pd.DataFrame(ekbe_rows), tracked_serials)


def generate_invoices(ekko_df, ekpo_df, ekbe_df):
    rbkp_rows = []
    rseg_rows = []

    if ekbe_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    gr_pos = set(ekbe_df["EBELN"].unique())
    po_headers = {r["EBELN"]: r for r in ekko_df.to_dict("records")}
    po_items = {}
    for r in ekpo_df.to_dict("records"):
        po_items.setdefault(r["EBELN"], []).append(r)

    belnr_counter = 1900000001

    for ebeln in gr_pos:
        if ebeln not in po_headers:
            continue

        po = po_headers[ebeln]
        po_date = datetime.strptime(po["BEDAT"], "%Y%m%d").date()

        inv_date = random_date_after(po_date, 20, 60)
        if inv_date > DATE_END:
            continue

        belnr = str(belnr_counter)
        belnr_counter += 1

        items = po_items.get(ebeln, [])
        total_value = sum(float(it["NETWR"]) for it in items)

        rbkp_rows.append({
            "BELNR": belnr, "GJAHR": str(inv_date.year),
            "BLDAT": format_sap_date(inv_date),
            "BUDAT": format_sap_date(inv_date + timedelta(days=random.randint(0, 3))),
            "LIFNR": po["LIFNR"],
            "WAERS": "EUR",
            "RMWWR": total_value,
            "XBLNR": f"INV-{ebeln[-6:]}",
            "EBELN": ebeln,
            "USNAM": random.choice(SAP_USERS),
        })

        for idx, it in enumerate(items):
            rseg_rows.append({
                "BELNR": belnr, "GJAHR": str(inv_date.year),
                "BUZEI": f"{idx + 1:04d}",
                "EBELN": ebeln, "EBELP": it["EBELP"],
                "MATNR": it["MATNR"],
                "MENGE": float(it["MENGE"]),
                "WRBTR": float(it["NETWR"]),
                "WAERS": "EUR",
            })

    return pd.DataFrame(rbkp_rows), pd.DataFrame(rseg_rows)


def generate_equipment(all_serials):
    equi_rows = []
    eqbs_rows = []

    for idx, ser in enumerate(all_serials):
        equnr = f"CPE-{idx + 1:08d}"

        if ser.get("vendor_returned"):
            status = "DLFL"
            status_text = "Defective - Returned to Vendor"
        elif ser.get("returned"):
            status = "RET"
            status_text = "Returned from Customer"
        elif ser.get("deployed"):
            status = "INST"
            status_text = "Installed at Customer"
        else:
            status = "AVLB"
            status_text = "Available in Stock"

        equi_rows.append({
            "EQUNR": equnr,
            "MATNR": ser["matnr"],
            "SERGE": ser["serial"],
            "HERST": next(v["NAME1"] for v in VENDORS if v["LIFNR"] == ser["vendor"]),
            "TYPBZ": next(m["MAKTX"] for m in MATERIALS if m["MATNR"] == ser["matnr"]),
            "INBDT": format_sap_date(ser.get("deploy_date")) if ser.get("deployed") else "",
            "ERDAT": format_sap_date(ser["gr_date"]),
            "ERNAM": "SYSTEM",
            "GEWRK": ser["werks"],
            "EQART": "CPE",
            "STAT_TEXT": status_text,
        })

        eqbs_rows.append({
            "EQUNR": equnr, "BEGDT": format_sap_date(ser["gr_date"]),
            "USTXT": "AVLB", "STAT_DESC": "Available in Stock",
        })
        if ser.get("deployed"):
            eqbs_rows.append({
                "EQUNR": equnr, "BEGDT": format_sap_date(ser["deploy_date"]),
                "USTXT": "INST", "STAT_DESC": "Installed at Customer",
            })
        if ser.get("returned"):
            eqbs_rows.append({
                "EQUNR": equnr, "BEGDT": format_sap_date(ser["return_date"]),
                "USTXT": "RET", "STAT_DESC": "Returned from Customer",
            })
        if ser.get("vendor_returned"):
            vr_effective = ser["return_date"] + timedelta(days=random.randint(5, 20))
            eqbs_rows.append({
                "EQUNR": equnr, "BEGDT": format_sap_date(vr_effective),
                "USTXT": "DLFL", "STAT_DESC": "Defective - Returned to Vendor",
            })

    return pd.DataFrame(equi_rows), pd.DataFrame(eqbs_rows)


def generate_stock(mseg_df):
    mard_rows = []

    inbound_bw = ["101", "161", "202", "561"]
    outbound_bw = ["102", "122", "201"]

    for m in MATERIALS:
        for sl in STORAGE_LOCATIONS:
            mat_movements = mseg_df[
                (mseg_df["MATNR"] == m["MATNR"]) &
                (mseg_df["WERKS"] == sl["WERKS"]) &
                (mseg_df["LGORT"] == sl["LGORT"])
            ]

            inbound = mat_movements[mat_movements["BWART"].isin(inbound_bw)]["MENGE"].sum()
            outbound = mat_movements[mat_movements["BWART"].isin(outbound_bw)]["MENGE"].sum()
            stock = max(0.0, float(inbound) - float(outbound))

            if stock > 0 or random.random() < 0.3:
                mard_rows.append({
                    "MATNR": m["MATNR"],
                    "WERKS": sl["WERKS"],
                    "LGORT": sl["LGORT"],
                    "LABST": float(stock),
                    "INSME": 0.0,
                    "SPEME": 0.0,
                })

    return pd.DataFrame(mard_rows)


def generate_accounting_docs(mkpf_df, mseg_df):
    bkpf_rows = []
    bseg_rows = []

    fi_counter = 100000001

    mseg_by_mblnr = {}
    for r in mseg_df.to_dict("records"):
        key = (r["MBLNR"], r["MJAHR"])
        mseg_by_mblnr.setdefault(key, []).append(r)

    for mkpf in mkpf_df.to_dict("records"):
        belnr = str(fi_counter)
        fi_counter += 1

        budat = mkpf["BUDAT"]
        year = budat[:4]

        bkpf_rows.append({
            "BUKRS": COMPANY_CODE, "BELNR": belnr, "GJAHR": year,
            "BLART": "WE",
            "BUDAT": budat,
            "BLDAT": mkpf["BLDAT"],
            "USNAM": mkpf["USNAM"],
            "XBLNR": mkpf["XBLNR"],
            "BKTXT": (mkpf["BKTXT"] or "")[:25],
        })

        items = mseg_by_mblnr.get((mkpf["MBLNR"], mkpf["MJAHR"]), [])
        line = 1
        for mseg_item in items:
            amount = float(mseg_item.get("DMBTR", 0) or 0)
            if amount <= 0:
                amount = 1.0

            bseg_rows.append({
                "BUKRS": COMPANY_CODE, "BELNR": belnr, "GJAHR": year,
                "BUZEI": f"{line:03d}",
                "BSCHL": "89",
                "HKONT": "1400000",
                "DMBTR": amount, "WRBTR": amount,
                "SHKZG": "S", "WAERS": "EUR",
                "MATNR": mseg_item.get("MATNR", ""),
                "WERKS": mseg_item.get("WERKS", ""),
            })
            line += 1

            bseg_rows.append({
                "BUKRS": COMPANY_CODE, "BELNR": belnr, "GJAHR": year,
                "BUZEI": f"{line:03d}",
                "BSCHL": "96",
                "HKONT": "1900000",
                "DMBTR": amount, "WRBTR": amount,
                "SHKZG": "H", "WAERS": "EUR",
                "MATNR": mseg_item.get("MATNR", ""),
                "WERKS": mseg_item.get("WERKS", ""),
            })
            line += 1

    return pd.DataFrame(bkpf_rows), pd.DataFrame(bseg_rows)


def generate_seri(equi_df, mseg_df):
    """SERI — serial number assignment at goods movement.

    Direction D Defect 1: emit one SERI row per equipment record so the
    equi -> seri -> mseg bridge has full coverage. Each equipment is
    paired with a real GR movement (BWART=101) where mseg.MATNR matches
    equi.MATNR. Pairing is deterministic via per-MATNR round-robin so
    SERI rows distribute across matching GR movements rather than all
    pointing to the same one.
    """
    from collections import defaultdict

    gr_movements = mseg_df[mseg_df["BWART"] == "101"]
    gr_by_matnr: dict[str, list[dict]] = defaultdict(list)
    for gr in gr_movements.to_dict("records"):
        gr_by_matnr[gr["MATNR"]].append(gr)

    matnr_seen: dict[str, int] = defaultdict(int)
    skipped = 0
    seri_rows = []
    for eq in equi_df.to_dict("records"):
        candidates = gr_by_matnr.get(eq["MATNR"]) or []
        if not candidates:
            skipped += 1
            continue
        idx = matnr_seen[eq["MATNR"]] % len(candidates)
        matnr_seen[eq["MATNR"]] += 1
        gr = candidates[idx]
        seri_rows.append({
            "OBKNR": f"SER-{eq['EQUNR']}",
            "MBLNR": gr["MBLNR"],
            "ZEILE": gr["ZEILE"],
            "ACCESSION_DATE": str(gr.get("MJAHR", "2025")) + "0101",
            "SERNR": eq["SERGE"],
            "MATNR": eq["MATNR"],
            "EQUNR": eq["EQUNR"],
        })

    if skipped:
        print(f"  [warn] generate_seri: {skipped} equipment records had no "
              f"matching GR (BWART=101) — should be 0 in current fixture")

    return pd.DataFrame(seri_rows)


def generate_ser01(ekpo_df, equi_df):
    """SER01 — serial number document header linking serials to POs."""
    ser01_rows = []
    for po_item in ekpo_df.to_dict("records"):
        ser01_rows.append({
            "OBKNR": f"S01-{po_item['EBELN']}-{po_item['EBELP']}",
            "OBZAE": 1,
            "SDESSION_TYPE": "01",
            "EBELN": po_item["EBELN"],
            "EBELP": po_item["EBELP"],
            "MATNR": po_item["MATNR"],
            "MENGE": float(po_item["MENGE"]),
            "MEINS": po_item["MEINS"],
        })
    return pd.DataFrame(ser01_rows)


def generate_ser03(mseg_df):
    """SER03 — serial number document header linking serials to material documents (GR)."""
    ser03_rows = []
    gr_items = mseg_df[mseg_df["BWART"] == "101"].to_dict("records")
    for item in gr_items:
        ser03_rows.append({
            "OBKNR": f"S03-{item['MBLNR']}-{item['ZEILE']}",
            "OBZAE": 1,
            "SDESSION_TYPE": "03",
            "MBLNR": item["MBLNR"],
            "ZEILE": item["ZEILE"],
            "MATNR": item["MATNR"],
            "MENGE": float(item["MENGE"]),
            "MEINS": item.get("MEINS", "ST"),
        })
    return pd.DataFrame(ser03_rows)


def generate_objk(equi_df):
    """OBJK — object list linking serial numbers to equipment master records."""
    objk_rows = []
    for eq in equi_df.to_dict("records"):
        objk_rows.append({
            "OBKNR": f"OBJ-{eq['EQUNR']}",
            "OBZAE": 1,
            "OBTYP": "E",
            "OBJNR": eq["EQUNR"],
            "MATNR": eq["MATNR"],
            "SERNR": eq["SERGE"],
            "TASER": "HT01",
            "EQUNR": eq["EQUNR"],
        })
    return pd.DataFrame(objk_rows)


def generate_empty_tables():
    """Create intentionally empty tables that are outside MVP scope.

    Each has correct column structure matching real SAP.
    - MCHB: CPE is serial-managed, not batch-managed.
    - MVKE: CPE procured, not sold via SD module.
    - MSKA: No sales order stock for CPE procurement.
    - LQUA/LTAP: WM module not active at HT.
    """
    mchb = pd.DataFrame(columns=["MATNR", "WERKS", "LGORT", "CHARG", "CLABS", "CINSM", "CSPEM"])
    mvke = pd.DataFrame(columns=["MATNR", "VKORG", "VTWEG", "KONDM", "KTGRM", "MVGR1"])
    mska = pd.DataFrame(columns=["MATNR", "WERKS", "LGORT", "VBELN", "POSNR", "KALAB", "KAINS", "KASPE"])
    lqua = pd.DataFrame(columns=["LGNUM", "LQNUM", "MATNR", "WERKS", "LGTYP", "LGPLA", "GESME", "VERME"])
    ltap = pd.DataFrame(columns=["LGNUM", "TANUM", "TAPOS", "MATNR", "WERKS", "VLTYP", "VLPLA", "NLTYP", "NLPLA", "VSOLM"])
    return {
        "raw_sap.mchb": mchb,
        "raw_sap.mvke": mvke,
        "raw_sap.mska": mska,
        "raw_sap.lqua": lqua,
        "raw_sap.ltap": ltap,
    }


def generate_reservations():
    rkpf_rows = []
    resb_rows = []

    mat_list = list(MATERIAL_DEMAND_WEIGHTS.keys())
    mat_weights = list(MATERIAL_DEMAND_WEIGHTS.values())

    for i in range(1, NUM_RESERVATIONS + 1):
        rsnum = sap_number("", 300000 + i, 10)
        res_date = random_date(DATE_START, DATE_END)
        mat = weighted_choice(mat_list, mat_weights)
        plant = weighted_choice(PLANTS, [p["weight"] for p in PLANTS])
        plant_sloc = [s["LGORT"] for s in STORAGE_LOCATIONS if s["WERKS"] == plant["WERKS"]][0]
        qty = random.choice([1, 2, 5, 10, 20])

        rkpf_rows.append({
            "RSNUM": rsnum,
            "RSDAT": format_sap_date(res_date),
            "USNAM": random.choice(SAP_USERS),
            "BKTXT": f"CPE deployment batch {i}",
        })
        resb_rows.append({
            "RSNUM": rsnum, "RSPOS": "0001",
            "MATNR": mat, "WERKS": plant["WERKS"],
            "LGORT": plant_sloc,
            "BDMNG": float(qty), "MEINS": "ST",
            "BDTER": format_sap_date(random_date_after(res_date, 1, 14)),
        })

    return pd.DataFrame(rkpf_rows), pd.DataFrame(resb_rows)


def _append_ingestion_log(
    *,
    started_at_utc: datetime,
    finished_at_utc: datetime,
    row_count_total: int,
    tables_touched: list,
    trigger_user: str,
):
    """Append one row to dbt/seeds/ingestion_log.csv. Creates the file
    with the canonical header if it does not exist. run_id format is
    ING-YYYYMMDD-NNN with NNN = next sequence for the UTC date.
    """
    import csv as _csv
    import os as _os

    log_path = ROOT / "dbt" / "seeds" / "ingestion_log.csv"
    fieldnames = [
        "run_id", "started_at_utc", "finished_at_utc", "source_type",
        "row_count_total", "tables_touched", "trigger_user", "notes",
    ]
    date_str = finished_at_utc.strftime("%Y%m%d")

    # Read existing rows (if any) to compute next sequence for today.
    existing_today = 0
    if log_path.exists() and log_path.stat().st_size > 0:
        with log_path.open("r", encoding="utf-8", newline="") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                rid = (row.get("run_id") or "").strip()
                if rid.startswith(f"ING-{date_str}-"):
                    existing_today += 1
    else:
        # Write header so dbt seed can load the empty file on first run.
        with log_path.open("w", encoding="utf-8", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            w.writeheader()

    run_id = f"ING-{date_str}-{existing_today + 1:03d}"
    row = {
        "run_id": run_id,
        "started_at_utc": started_at_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "finished_at_utc": finished_at_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_type": "sample_generator",
        "row_count_total": row_count_total,
        "tables_touched": ",".join(sorted(tables_touched)),
        "trigger_user": _os.getenv("USER") or _os.getenv("USERNAME") or "default",
        "notes": "",
    }
    _ = trigger_user  # kept in signature for future flexibility
    with log_path.open("a", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n", quoting=_csv.QUOTE_MINIMAL)
        w.writerow(row)
    print(f"Ingestion log updated: {run_id} ({row['row_count_total']:,} rows across {len(tables_touched)} tables)")


def main():
    from datetime import timezone as _tz

    started_at_utc = datetime.now(_tz.utc)
    print("Generating SAP sample data for Helios Telecom CPE procurement...")
    print(f"Period: {DATE_START} to {DATE_END}")
    print()

    print("1/10 Org structure (T001, T001W, T001L, T024, T024E, T023, T156, T156T)...")
    t001, t001w, t001l, t024, t024e, t023, t156, t156t = generate_org_tables()

    print("2/10 Vendor master (LFA1, LFB1, LFM1) + Z-table extension (ZMM_VENDOR_BUSINESS)...")
    lfa1, lfb1, lfm1, zmm_vendor_business = generate_vendor_tables()

    print("3/10 Material master (MARA, MAKT, MARC, MARM) + Z-table extension (ZMM_MATERIAL_BUSINESS)...")
    mara, makt, marc, marm, mvke, zmm_material_business = generate_material_tables()

    print("4/10 Purchase requisitions (EBAN, EBKN)...")
    eban, ebkn = generate_purchase_requisitions()

    print("5/10 Purchase orders (EKKO, EKPO, EKET, EKKN)...")
    ekko, ekpo, eket, ekkn = generate_purchase_orders(eban)

    print("6/10 Goods receipts & movements (MKPF, MSEG, EKBE) + serial tracking...")
    mkpf, mseg, ekbe, all_serials = generate_goods_receipts(ekpo, ekko)

    print("7/10 Invoices (RBKP, RSEG)...")
    rbkp, rseg = generate_invoices(ekko, ekpo, ekbe)

    print("8/10 Equipment master (EQUI, EQBS)...")
    equi, eqbs = generate_equipment(all_serials)

    print("  Serial tracking (SERI, SER01, SER03, OBJK)...")
    seri = generate_seri(equi, mseg)
    ser01 = generate_ser01(ekpo, equi)
    ser03 = generate_ser03(mseg)
    objk = generate_objk(equi)

    print("9/10 Current stock (MARD)...")
    mard = generate_stock(mseg)

    print("10/10 Accounting docs (BKPF, BSEG) + reservations (RKPF, RESB)...")
    bkpf, bseg = generate_accounting_docs(mkpf, mseg)
    rkpf, resb = generate_reservations()

    print()
    print(f"Loading into {DB_PATH}...")

    conn = duckdb.connect(str(DB_PATH))
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw_sap")

    tables = {
        "raw_sap.t001": t001, "raw_sap.t001w": t001w, "raw_sap.t001l": t001l,
        "raw_sap.t024": t024, "raw_sap.t024e": t024e, "raw_sap.t023": t023,
        "raw_sap.t156": t156, "raw_sap.t156t": t156t,
        "raw_sap.lfa1": lfa1, "raw_sap.lfb1": lfb1, "raw_sap.lfm1": lfm1,
        "raw_sap.zmm_vendor_business": zmm_vendor_business,
        "raw_sap.mara": mara, "raw_sap.makt": makt, "raw_sap.marc": marc,
        "raw_sap.marm": marm,
        "raw_sap.zmm_material_business": zmm_material_business,
        "raw_sap.eban": eban, "raw_sap.ebkn": ebkn,
        "raw_sap.ekko": ekko, "raw_sap.ekpo": ekpo, "raw_sap.eket": eket,
        "raw_sap.ekkn": ekkn,
        "raw_sap.mkpf": mkpf, "raw_sap.mseg": mseg, "raw_sap.ekbe": ekbe,
        "raw_sap.rbkp": rbkp, "raw_sap.rseg": rseg,
        "raw_sap.equi": equi, "raw_sap.eqbs": eqbs,
        "raw_sap.seri": seri, "raw_sap.ser01": ser01,
        "raw_sap.ser03": ser03, "raw_sap.objk": objk,
        "raw_sap.mard": mard,
        "raw_sap.bkpf": bkpf, "raw_sap.bseg": bseg,
        "raw_sap.rkpf": rkpf, "raw_sap.resb": resb,
    }

    loaded = 0
    for table_name, df in tables.items():
        if df is None or df.empty:
            print(f"  SKIP {table_name} (empty)")
            continue
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")
        conn.register("df_temp", df)
        conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM df_temp")
        conn.unregister("df_temp")
        loaded += 1
        print(f"  {table_name}: {len(df):,} rows")

    # Empty tables (outside MVP scope — schema only for completeness)
    print("  Empty tables (schema only — outside MVP scope)...")
    empty_tables = generate_empty_tables()
    for table_name, df in empty_tables.items():
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")
        cols = ", ".join([f"{c} VARCHAR" for c in df.columns])
        conn.execute(f"CREATE TABLE {table_name} ({cols})")
        loaded += 1
        print(f"  {table_name}: 0 rows ({len(df.columns)} cols, schema only)")

    conn.close()

    print()
    print("=" * 60)
    print("SAP SAMPLE DATA GENERATION COMPLETE")
    print("=" * 60)
    total_rows = sum(len(df) for df in tables.values() if df is not None and not df.empty)
    print(f"Total tables: {loaded}")
    print(f"Total rows: {total_rows:,}")
    print(f"Equipment (CPE devices): {len(equi):,}")
    print(f"Purchase orders: {len(ekko):,}")
    print(f"Goods receipts: {len(mkpf):,}")
    print(f"Material movements: {len(mseg):,}")
    print(f"Database: {DB_PATH}")
    print()

    # Phase 11: stamp the ingestion event so the Streamlit freshness
    # banner has a baseline. Tables actually populated (non-empty) +
    # the 5 empty-schema tables are all included in tables_touched.
    from datetime import timezone as _tz
    finished_at_utc = datetime.now(_tz.utc)
    tables_populated = [
        name.replace("raw_sap.", "") for name, df in tables.items()
        if df is not None and not df.empty
    ]
    tables_empty = [name.replace("raw_sap.", "") for name in empty_tables.keys()]
    _append_ingestion_log(
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        row_count_total=total_rows,
        tables_touched=tables_populated + tables_empty,
        trigger_user="",  # resolved inside helper via env
    )

    print()
    print("Run `dbt run` to build staging and vault models on top of this data.")


if __name__ == "__main__":
    main()
