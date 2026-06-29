"""Throwaway — clarify the MM-IV (RBKP/RSEG) vs FI (BKPF/BSEG) split
in actual loaded data. Safe to delete."""
import duckdb

conn = duckdb.connect("cpe_analytics.duckdb", read_only=True)

print("=== BKPF document types (BLART) actually in the data ===")
for r in conn.execute("""
    SELECT BLART, count(*) AS docs
    FROM raw_sap.bkpf
    GROUP BY BLART ORDER BY docs DESC
""").fetchall():
    print(f"  BLART={r[0]}  docs={r[1]:,}")

print()
print("=== Row counts side by side ===")
for tbl, descr in [
    ("rbkp", "MM-IV: invoice header (one per vendor invoice)"),
    ("rseg", "MM-IV: invoice line (links to PO line via EBELN+EBELP)"),
    ("bkpf", "FI: accounting doc header (one per FI posting event)"),
    ("bseg", "FI: accounting doc line (the GL entries)"),
    ("mkpf", "MM: material doc header (GR / movement)"),
    ("mseg", "MM: material doc line"),
]:
    n = conn.execute(f"SELECT count(*) FROM raw_sap.{tbl}").fetchone()[0]
    print(f"  raw_sap.{tbl:<6} {n:>7,}  {descr}")

print()
print("=== Does our BKPF link to RBKP (invoice) or MKPF (GR)? ===")
# Try to match BKPF.XBLNR (external reference) to MKPF.MBLNR or RBKP.BELNR
r = conn.execute("""
    SELECT count(*) AS bkpf_total,
           sum(CASE WHEN XBLNR IN (SELECT MBLNR FROM raw_sap.mkpf) THEN 1 ELSE 0 END)
             AS xblnr_matches_mkpf,
           sum(CASE WHEN XBLNR IN (SELECT BELNR FROM raw_sap.rbkp) THEN 1 ELSE 0 END)
             AS xblnr_matches_rbkp
    FROM raw_sap.bkpf
""").fetchone()
print(f"  total BKPF: {r[0]:,}")
print(f"  BKPF.XBLNR matches an MKPF document: {r[1]:,}")
print(f"  BKPF.XBLNR matches an RBKP document: {r[2]:,}")

print()
print("=== Does RBKP have a financial-side reference back to BKPF? ===")
cols = conn.execute("""
    SELECT column_name FROM information_schema.columns
    WHERE table_schema = 'raw_sap' AND table_name = 'rbkp'
""").fetchall()
print(f"  raw_sap.rbkp columns: {[c[0] for c in cols]}")

conn.close()
