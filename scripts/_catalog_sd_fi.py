"""
Stage 1f: append SD + FI-extension field rows to sap_data_dictionary.csv so the
new tables are visible to the S2T engine's Stage A scope derivation.

One-time catalog step for the Path-3 infra build. Idempotent: skips any
(table_name, field_name) already present.
"""
import csv
from pathlib import Path

SEED = Path(__file__).resolve().parents[1] / "dbt" / "seeds" / "sap_data_dictionary.csv"

FIELDS = ["table_name", "field_name", "data_type", "length", "description_en",
          "description_hr", "business_meaning", "example_value", "domain_area",
          "description_source", "needs_review"]

# (table, field, type, len, desc_en, desc_hr, business_meaning, example, domain)
ROWS = [
    # --- KNA1 (customer master) ---
    ("KNA1", "KUNNR", "CHAR", "10", "Unique customer number.", "Broj kupca.",
     "Primary key for customer master. Identifies the subscriber who holds a service contract and the deployed CPE device. Links billing and sales documents to a person/business.", "1000000001", "sales"),
    ("KNA1", "NAME1", "CHAR", "35", "Customer name.", "Naziv kupca.",
     "Display name of the residential subscriber or business customer.", "Ivan Horvat", "sales"),
    ("KNA1", "LAND1", "CHAR", "3", "Country key.", "Šifra zemlje.",
     "Country of the customer; HR for Croatian subscribers.", "HR", "sales"),
    ("KNA1", "ORT01", "CHAR", "35", "City.", "Grad.",
     "City of the customer premises where the CPE is installed.", "Zagreb", "sales"),
    ("KNA1", "STRAS", "CHAR", "35", "Street and house number.", "Ulica i kućni broj.",
     "Street address of the service location.", "Ilica 1", "sales"),
    ("KNA1", "PSTLZ", "CHAR", "10", "Postal code.", "Poštanski broj.",
     "Postal code of the customer premises.", "10000", "sales"),
    ("KNA1", "KTOKD", "CHAR", "4", "Customer account group.", "Knjigovodstvena grupa kupca.",
     "Classifies the customer: 0001 residential, 0002 business. Drives segmentation of revenue and service plan.", "0001", "sales"),
    ("KNA1", "ERDAT", "DATS", "8", "Record creation date.", "Datum kreiranja zapisa.",
     "Date the customer master record was created, aligned with first service activation.", "20250115", "sales"),
    ("KNA1", "SPRAS", "LANG", "1", "Language key.", "Šifra jezika.",
     "Communication language for the customer.", "HR", "sales"),

    # --- VBAK (sales order header = service contract) ---
    ("VBAK", "VBELN", "CHAR", "10", "Sales document number.", "Broj prodajnog dokumenta.",
     "Primary key of the sales order (the service contract). Billing documents reference it via AUBEL.", "0000010001", "sales"),
    ("VBAK", "ERDAT", "DATS", "8", "Record creation date.", "Datum kreiranja.",
     "Date the sales order was created.", "20250115", "sales"),
    ("VBAK", "AUDAT", "DATS", "8", "Document date.", "Datum dokumenta.",
     "Contract/order date; equals the date the CPE device was deployed to the customer. Anchors the 24-month contract and cost amortisation window.", "20250115", "sales"),
    ("VBAK", "AUART", "CHAR", "4", "Sales document type.", "Vrsta prodajnog dokumenta.",
     "Order type; TA = standard service order.", "TA", "sales"),
    ("VBAK", "VKORG", "CHAR", "4", "Sales organization.", "Prodajna organizacija.",
     "Sales organization responsible for the contract.", "HT01", "sales"),
    ("VBAK", "VTWEG", "CHAR", "2", "Distribution channel.", "Distribucijski kanal.",
     "Distribution channel for the service.", "10", "sales"),
    ("VBAK", "SPART", "CHAR", "2", "Division.", "Sektor.",
     "Product division for the service (broadband/CPE).", "10", "sales"),
    ("VBAK", "KUNNR", "CHAR", "10", "Sold-to customer.", "Kupac (naručitelj).",
     "Foreign key to KNA1; the subscriber on the contract.", "1000000001", "sales"),
    ("VBAK", "NETWR", "CURR", "15", "Net value of the order.", "Neto vrijednost narudžbe.",
     "Net contract value in document currency.", "24.99", "sales"),
    ("VBAK", "WAERK", "CUKY", "5", "Document currency.", "Valuta dokumenta.",
     "Currency of the order amounts.", "EUR", "sales"),

    # --- VBAP (sales order item = service line + device link) ---
    ("VBAP", "VBELN", "CHAR", "10", "Sales document number.", "Broj prodajnog dokumenta.",
     "Foreign key to VBAK header.", "0000010001", "sales"),
    ("VBAP", "POSNR", "NUMC", "6", "Sales document item number.", "Broj stavke.",
     "Line item number within the sales order.", "000010", "sales"),
    ("VBAP", "MATNR", "CHAR", "18", "Service-plan material number.", "Broj materijala (usluga).",
     "The service plan being sold (e.g. SVC-FIB-500 fiber subscription). NOT the CPE device material — the device is linked via SERNR.", "SVC-FIB-500", "sales"),
    ("VBAP", "ARKTX", "CHAR", "40", "Item short text.", "Kratki tekst stavke.",
     "Service plan description shown on the order.", "Optika 500 Mbit/s", "sales"),
    ("VBAP", "MATKL", "CHAR", "9", "Material group.", "Grupa materijala.",
     "Service category group (SVC-FIB / SVC-TV / SVC-CBL / SVC-BIZ).", "SVC-FIB", "sales"),
    ("VBAP", "KWMENG", "QUAN", "15", "Order quantity.", "Količina narudžbe.",
     "Quantity of the service line (1 subscription).", "1", "sales"),
    ("VBAP", "VRKME", "UNIT", "3", "Sales unit.", "Prodajna jedinica.",
     "Unit of the service line (MON = monthly).", "MON", "sales"),
    ("VBAP", "NETWR", "CURR", "15", "Net value of the item.", "Neto vrijednost stavke.",
     "Net value of the service line.", "34.99", "sales"),
    ("VBAP", "WERKS", "CHAR", "4", "Plant.", "Pogon.",
     "Plant servicing the contract.", "HT10", "sales"),
    ("VBAP", "SERNR", "CHAR", "18", "Serial number of the deployed CPE device.", "Serijski broj uređaja.",
     "Serial number of the physical CPE device installed at the customer. Joins to EQUI.SERGE to resolve the router model (EQUI.MATNR) — the key tie from service revenue to device cost.", "SN-001-022045", "sales"),

    # --- VBRK (billing header) ---
    ("VBRK", "VBELN", "CHAR", "10", "Billing document number.", "Broj dokumenta fakture.",
     "Primary key of the monthly service invoice.", "9000000001", "sales"),
    ("VBRK", "FKART", "CHAR", "4", "Billing type.", "Vrsta fakture.",
     "Billing document type; F2 = standard invoice.", "F2", "sales"),
    ("VBRK", "VBTYP", "CHAR", "1", "SD document category.", "Kategorija SD dokumenta.",
     "Document category; M = invoice.", "M", "sales"),
    ("VBRK", "FKDAT", "DATS", "8", "Billing date.", "Datum fakturiranja.",
     "Date of the billing document; the calendar month of recognised service revenue. Drives the material x month grain.", "20250131", "sales"),
    ("VBRK", "ERDAT", "DATS", "8", "Record creation date.", "Datum kreiranja.",
     "Date the billing document was created.", "20250131", "sales"),
    ("VBRK", "KUNRG", "CHAR", "10", "Payer.", "Platiša.",
     "Foreign key to KNA1; the customer billed.", "1000000001", "sales"),
    ("VBRK", "KUNAG", "CHAR", "10", "Sold-to party.", "Naručitelj.",
     "Sold-to customer on the billing document.", "1000000001", "sales"),
    ("VBRK", "NETWR", "CURR", "15", "Net value of the billing document.", "Neto vrijednost fakture.",
     "Net service revenue for the month (excludes VAT). Primary revenue measure for the margin term.", "34.99", "sales"),
    ("VBRK", "MWSBK", "CURR", "15", "Tax amount.", "Iznos poreza.",
     "Output VAT (PDV 25%) on the service billing; excluded from margin.", "8.75", "sales"),
    ("VBRK", "WAERK", "CUKY", "5", "Document currency.", "Valuta dokumenta.",
     "Currency of the billing amounts.", "EUR", "sales"),
    ("VBRK", "VKORG", "CHAR", "4", "Sales organization.", "Prodajna organizacija.",
     "Sales organization issuing the invoice.", "HT01", "sales"),
    ("VBRK", "FKSTO", "CHAR", "1", "Billing document is cancelled.", "Faktura je stornirana.",
     "Cancellation flag; 'X' = cancelled billing document, excluded from revenue. Empty = valid.", "X", "sales"),
    ("VBRK", "BUKRS", "CHAR", "4", "Company code.", "Šifra poduzeća.",
     "Company code of the billing document.", "HT00", "sales"),

    # --- VBRP (billing item) ---
    ("VBRP", "VBELN", "CHAR", "10", "Billing document number.", "Broj dokumenta fakture.",
     "Foreign key to VBRK header.", "9000000001", "sales"),
    ("VBRP", "POSNR", "NUMC", "6", "Billing item number.", "Broj stavke fakture.",
     "Line item number within the billing document.", "000010", "sales"),
    ("VBRP", "FKIMG", "QUAN", "15", "Billed quantity.", "Fakturirana količina.",
     "Quantity billed (1 monthly subscription).", "1", "sales"),
    ("VBRP", "VRKME", "UNIT", "3", "Sales unit.", "Prodajna jedinica.",
     "Unit of the billed quantity (MON).", "MON", "sales"),
    ("VBRP", "NETWR", "CURR", "15", "Net value of the billing item.", "Neto vrijednost stavke.",
     "Net service revenue at line level; equals VBRK.NETWR for single-line service invoices.", "34.99", "sales"),
    ("VBRP", "MATNR", "CHAR", "18", "Service-plan material number.", "Broj materijala (usluga).",
     "Service plan billed.", "SVC-FIB-500", "sales"),
    ("VBRP", "ARKTX", "CHAR", "40", "Item short text.", "Kratki tekst stavke.",
     "Service plan description.", "Optika 500 Mbit/s", "sales"),
    ("VBRP", "MATKL", "CHAR", "9", "Material group.", "Grupa materijala.",
     "Service category group.", "SVC-FIB", "sales"),
    ("VBRP", "WERKS", "CHAR", "4", "Plant.", "Pogon.",
     "Plant servicing the contract.", "HT10", "sales"),
    ("VBRP", "AUBEL", "CHAR", "10", "Sales order reference.", "Referenca prodajne narudžbe.",
     "Foreign key to VBAK; the originating sales order. Used to traverse billing -> sales order -> device.", "0000010001", "sales"),
    ("VBRP", "AUPOS", "NUMC", "6", "Sales order item reference.", "Referenca stavke narudžbe.",
     "Item reference back to the sales order line.", "000010", "sales"),
    ("VBRP", "PRSDT", "DATS", "8", "Pricing date.", "Datum cijene.",
     "Date used for pricing the billing line.", "20250131", "sales"),

    # --- BKPF extension (FI <-> source document linkage) ---
    ("BKPF", "AWTYP", "CHAR", "5", "Reference transaction type.", "Vrsta referentne transakcije.",
     "Identifies the source logistics document type behind the FI posting: RMRP = MM invoice (RE), VBRK = SD billing (RV), MKPF = material document (WE). With AWKEY, links the journal entry to its originating document.", "VBRK", "finance"),
    ("BKPF", "AWKEY", "CHAR", "20", "Reference key.", "Referentni ključ.",
     "Concatenated key of the source document (e.g. VBELN+fiscal year for billing, BELNR+GJAHR for MM invoice, MBLNR+MJAHR for material document). Resolves the FI posting back to its operational document.", "9000000001 2025", "finance"),
]


def main():
    # read existing keys
    existing = set()
    with open(SEED, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            existing.add((r["table_name"], r["field_name"]))

    new_rows = []
    for t, fld, dt, ln, en, hr, bm, ex, dom in ROWS:
        if (t, fld) in existing:
            continue
        new_rows.append({
            "table_name": t, "field_name": fld, "data_type": dt, "length": ln,
            "description_en": en, "description_hr": hr, "business_meaning": bm,
            "example_value": ex, "domain_area": dom,
            "description_source": "sap_standard", "needs_review": "0",
        })

    if not new_rows:
        print("Nothing to add (all rows already present).")
        return

    # validate before write (RULE 37): every row has all fields
    for r in new_rows:
        assert set(r.keys()) == set(FIELDS), r

    with open(SEED, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n", quoting=csv.QUOTE_ALL)
        w.writerows(new_rows)

    print(f"Appended {len(new_rows)} dictionary rows.")
    by_t = {}
    for r in new_rows:
        by_t[r["table_name"]] = by_t.get(r["table_name"], 0) + 1
    for t, n in by_t.items():
        print(f"  {t}: {n}")


if __name__ == "__main__":
    main()
