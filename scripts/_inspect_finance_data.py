"""Throwaway inspection script — finance data inventory for the
KI-71 walkthrough conversation. Safe to delete."""
import duckdb

conn = duckdb.connect("cpe_analytics.duckdb", read_only=True)

print("=== HKONT (GL account) values in BSEG ===")
for r in conn.execute("""
    SELECT HKONT, count(*) AS lines, sum(CAST(DMBTR AS DOUBLE)) AS total_amount,
           sum(CASE WHEN SHKZG = 'S' THEN 1 ELSE 0 END) AS debit_lines,
           sum(CASE WHEN SHKZG = 'H' THEN 1 ELSE 0 END) AS credit_lines
    FROM raw_sap.bseg GROUP BY HKONT ORDER BY lines DESC
""").fetchall():
    print(f"  account={r[0]}  lines={r[1]:>6}  total={r[2]:>14,.0f} EUR  debit/credit={r[3]}/{r[4]}")

print()
print("=== Sample BSEG rows with material context ===")
for r in conn.execute("""
    SELECT b.BELNR, b.BUZEI, b.HKONT, b.DMBTR, b.SHKZG, b.MATNR,
           m.MTART AS mtype, m.MATKL AS mgroup
    FROM raw_sap.bseg b
    LEFT JOIN raw_sap.mara m ON m.MATNR = b.MATNR
    WHERE b.MATNR IS NOT NULL AND b.MATNR != ''
    LIMIT 5
""").fetchall():
    print(f"  doc={r[0]}/{r[1]}  acct={r[2]}  amt={r[3]:>10}  d/c={r[4]}  mat={r[5]}  type={r[6]}  group={r[7]}")

print()
print("=== MARA — material attributes (drives CPE classification) ===")
for r in conn.execute("SELECT MATNR, MTART, MATKL, MEINS FROM raw_sap.mara").fetchall():
    print(f"  {r[0]:12s}  type={r[1]:6s}  group={r[2]:8s}  unit={r[3]}")

print()
print("=== Sum of invoice line amounts per material (RSEG -> EKPO -> MARA) ===")
for r in conn.execute("""
    SELECT ep.MATNR,
           sum(CAST(r.WRBTR AS DOUBLE)) AS invoiced_amount,
           count(*) AS invoice_lines
    FROM raw_sap.rseg r
    JOIN raw_sap.ekpo ep ON ep.EBELN = r.EBELN AND ep.EBELP = r.EBELP
    WHERE ep.MATNR IS NOT NULL AND ep.MATNR != ''
    GROUP BY ep.MATNR ORDER BY invoiced_amount DESC
""").fetchall():
    print(f"  material={r[0]:12s}  total_invoiced={r[1]:>14,.0f} EUR  lines={r[2]}")

print()
print("=== Existing marts that touch money / amounts ===")
for r in conn.execute("""
    SELECT table_name, column_name FROM information_schema.columns
    WHERE table_schema = 'main_marts'
      AND (column_name ILIKE '%amount%' OR column_name ILIKE '%value%'
        OR column_name ILIKE '%price%' OR column_name ILIKE '%cost%'
        OR column_name ILIKE '%eur%' OR column_name ILIKE '%spend%')
    ORDER BY table_name, column_name
""").fetchall():
    print(f"  {r[0]}.{r[1]}")

print()
print("=== OBT views that touch money / amounts ===")
for r in conn.execute("""
    SELECT table_name, column_name FROM information_schema.columns
    WHERE table_schema = 'main_obt'
      AND (column_name ILIKE '%amount%' OR column_name ILIKE '%value%'
        OR column_name ILIKE '%price%' OR column_name ILIKE '%cost%'
        OR column_name ILIKE '%eur%' OR column_name ILIKE '%spend%')
    ORDER BY table_name, column_name
""").fetchall():
    print(f"  {r[0]}.{r[1]}")

conn.close()
