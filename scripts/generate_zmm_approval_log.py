"""8.4.7 — Generate ZMM_APPROVAL_LOG Z-table + 500 correlated sample rows.

Creates `raw_sap.zmm_approval_log` (12 cols, SGTXT not COMMENT per SQL
keyword avoidance). 500 rows correlated with existing raw_sap.rbkp:
- 80% (400 rows) have BELNR/GJAHR matching real rbkp invoices
- 20% (100 rows) have synthetic BELNR simulating approvals for
  out-of-sample invoices

Status distribution: 375 APPROVED (02), 75 REJECTED (03), 35 PENDING
(01), 15 ESCALATED (04). Reason codes weighted per spec. APPR_DATE
derived from invoice BUDAT + 0-14 days (NULL for PENDING). Approver pool:
U0001-U0010. TOL_AMT populated for QTYV/PRCV (70%). SGTXT populated
per-status rates.

No dbt source / no staging model — intentional Layer A target.
"""
from __future__ import annotations

import datetime as dt
import random
import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "cpe_analytics.duckdb"

# Deterministic seed for reproducibility
random.seed(20260421)

DDL = """
DROP TABLE IF EXISTS raw_sap.zmm_approval_log;
CREATE TABLE raw_sap.zmm_approval_log (
    MANDT           VARCHAR,
    APPROVAL_ID     VARCHAR NOT NULL,
    BELNR           VARCHAR,
    GJAHR           VARCHAR,
    EBELN           VARCHAR,
    EBELP           VARCHAR,
    APPR_STATUS     VARCHAR,
    APPR_DATE       VARCHAR,
    APPR_USER       VARCHAR,
    REASON_CODE     VARCHAR,
    TOL_AMT         DECIMAL(13,2),
    SGTXT           VARCHAR
);
"""

APPROVERS = [f"U{str(i).zfill(4)}" for i in range(1, 11)]  # U0001..U0010
STATUS_WEIGHTS = [("02", 375), ("03", 75), ("01", 35), ("04", 15)]  # 500 total
REASON_WEIGHTS = [
    ("QTYV", 200), ("PRCV", 125), ("TOL1", 100),
    ("TOL2", 40), ("TOL3", 25), ("MISC", 10),
]  # 500 total
SGTXT_TEMPLATES = {
    "QTYV": [
        "Quantity variance within tolerance — approved",
        "Qty short by 2% — accepted with vendor confirmation",
        "Delivered 3% over PO — tolerance band accepted",
        "Partial GR reconciled — qty gap documented",
    ],
    "PRCV": [
        "Unit price +2% vs PO — approved per contract clause",
        "Price variance within ±3% band — accepted",
        "Invoice price matches amended PO — OK",
        "FX revaluation on EUR-HRK — accepted",
    ],
    "TOL1": [
        "Within T1 tolerance — auto-approved",
        "Tolerance band 1 exception — no action required",
    ],
    "TOL2": [
        "Tolerance band 2 exception — manager review",
        "T2 band breach — escalated to purchasing",
    ],
    "TOL3": [
        "Tolerance band 3 — rejected pending vendor response",
        "T3 breach — awaiting credit note",
    ],
    "MISC": [
        "Incorrect invoice reference — vendor to reissue",
        "Duplicate invoice detected — rejected",
        "Late submission — escalated to AP",
    ],
}
REJECT_SGTXT = [
    "Invoice rejected — quantity mismatch not reconciled",
    "Invoice rejected — price exceeds contract by >5%",
    "Invoice rejected — missing PO reference",
    "Duplicate invoice — previously paid under different BELNR",
]
ESCALATE_SGTXT = [
    "Escalated to CFO — variance exceeds department threshold",
    "Escalated to Purchasing Lead — vendor dispute",
    "Escalated — contract amendment required before approval",
]


def _weighted_list(weights: list[tuple[str, int]]) -> list[str]:
    out: list[str] = []
    for value, count in weights:
        out.extend([value] * count)
    return out


def _varchar_date_plus(yyyymmdd: str | None, days: int) -> str | None:
    if not yyyymmdd or len(yyyymmdd) != 8:
        return None
    try:
        d = dt.date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:]))
    except ValueError:
        return None
    return (d + dt.timedelta(days=days)).strftime("%Y%m%d")


def _ebelp_from_rbkp(ebeln: str | None) -> str | None:
    """Line number 00010/00020/... drawn uniformly when PO exists."""
    if not ebeln:
        return None
    # Typical EKPO has 1-3 lines per PO
    n = random.randint(1, 3)
    return str(n * 10).zfill(5)


def generate_rows(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Build 500 rows. Returns list of 12-tuples ready for INSERT."""
    rbkp = conn.execute(
        "SELECT BELNR, GJAHR, EBELN, BUDAT FROM raw_sap.rbkp ORDER BY BELNR"
    ).fetchall()

    # Status and reason assignments
    statuses = _weighted_list(STATUS_WEIGHTS)
    reasons = _weighted_list(REASON_WEIGHTS)
    random.shuffle(statuses)
    random.shuffle(reasons)

    # Pick 400 rbkp rows to correlate with
    correlated_indices = random.sample(range(len(rbkp)), min(400, len(rbkp)))
    correlated_rbkp = [rbkp[i] for i in correlated_indices]

    rows: list[tuple] = []
    for i in range(500):
        approval_id = f"ZAPR{str(i + 1).zfill(6)}"
        status = statuses[i]
        reason = reasons[i]

        # Correlation: first 400 correlated, last 100 synthetic
        if i < len(correlated_rbkp):
            rb_belnr, rb_gjahr, rb_ebeln, rb_budat = correlated_rbkp[i]
            belnr = rb_belnr
            gjahr = rb_gjahr
            ebeln = rb_ebeln
            ebelp = _ebelp_from_rbkp(rb_ebeln) if rb_ebeln else None
            base_date = rb_budat
        else:
            # Synthetic: belnr outside sample range, no ebeln/ebelp link
            belnr = f"19{str(900_000_000 + i).zfill(8)}"
            gjahr = random.choice(["2024", "2025", "2026"])
            ebeln = None
            ebelp = None
            # Synthetic base date: uniform across 2024-03 → 2026-03
            base_date = random.choice(["20240315", "20240815", "20250220",
                                        "20250712", "20251118", "20260210"])

        # APPR_DATE: BUDAT + 0..14 days, NULL if status is pending
        if status == "01":  # PENDING
            appr_date = None
        else:
            appr_date = _varchar_date_plus(base_date, random.randint(0, 14))

        appr_user = random.choice(APPROVERS)

        # TOL_AMT: 70% populated for QTYV/PRCV; NULL otherwise
        if reason in ("QTYV", "PRCV") and random.random() < 0.7:
            # Plausible tolerance amounts: 10 - 2000 EUR
            tol_amt = round(random.uniform(10.0, 2000.0), 2)
        else:
            tol_amt = None

        # SGTXT: rates by status — spec says 100% REJECTED, 60% APPROVED,
        # 100% ESCALATED, 30% PENDING
        sgtxt_rate = {"02": 0.60, "03": 1.00, "01": 0.30, "04": 1.00}[status]
        if random.random() < sgtxt_rate:
            if status == "03":
                sgtxt = random.choice(REJECT_SGTXT)
            elif status == "04":
                sgtxt = random.choice(ESCALATE_SGTXT)
            else:
                sgtxt = random.choice(SGTXT_TEMPLATES[reason])
        else:
            sgtxt = None

        rows.append((
            "100", approval_id, belnr, gjahr, ebeln, ebelp,
            status, appr_date, appr_user, reason, tol_amt, sgtxt,
        ))
    return rows


def main() -> int:
    conn = duckdb.connect(str(_DB))  # read-write
    try:
        print("Dropping + creating raw_sap.zmm_approval_log...")
        conn.execute(DDL)

        print("Generating 500 correlated rows from rbkp...")
        rows = generate_rows(conn)

        print("Inserting rows...")
        conn.executemany(
            "INSERT INTO raw_sap.zmm_approval_log VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

        # Verification
        n = conn.execute("SELECT COUNT(*) FROM raw_sap.zmm_approval_log").fetchone()[0]
        print(f"\n  total rows: {n}")
        print("\n  status distribution:")
        for r in conn.execute(
            "SELECT APPR_STATUS, COUNT(*) FROM raw_sap.zmm_approval_log "
            "GROUP BY APPR_STATUS ORDER BY 1"
        ).fetchall():
            print(f"    {r[0]}  {r[1]}")
        print("\n  reason_code distribution:")
        for r in conn.execute(
            "SELECT REASON_CODE, COUNT(*) FROM raw_sap.zmm_approval_log "
            "GROUP BY REASON_CODE ORDER BY 2 DESC"
        ).fetchall():
            print(f"    {r[0]}  {r[1]}")
        print("\n  correlation with rbkp:")
        corr = conn.execute(
            "SELECT COUNT(*) FROM raw_sap.zmm_approval_log z "
            "WHERE EXISTS (SELECT 1 FROM raw_sap.rbkp r "
            "              WHERE r.BELNR=z.BELNR AND r.GJAHR=z.GJAHR)"
        ).fetchone()[0]
        print(f"    correlated rows: {corr}/500 ({corr/5:.1f}%)")
        print("\n  APPR_DATE null pct (pending-only expected):")
        n_null = conn.execute(
            "SELECT COUNT(*) FROM raw_sap.zmm_approval_log "
            "WHERE APPR_DATE IS NULL"
        ).fetchone()[0]
        print(f"    {n_null}/500 ({n_null/5:.1f}%; expected ~7% = 35 pending)")
        print("\n  TOL_AMT populated for QTYV/PRCV:")
        r = conn.execute(
            "SELECT REASON_CODE, COUNT(*) FILTER (WHERE TOL_AMT IS NOT NULL), COUNT(*) "
            "FROM raw_sap.zmm_approval_log "
            "WHERE REASON_CODE IN ('QTYV','PRCV') "
            "GROUP BY REASON_CODE ORDER BY 1"
        ).fetchall()
        for reason, filled, total in r:
            print(f"    {reason}: {filled}/{total} ({filled/total*100:.1f}%)")
        print("\n  SGTXT populated by status:")
        r = conn.execute(
            "SELECT APPR_STATUS, COUNT(*) FILTER (WHERE SGTXT IS NOT NULL), COUNT(*) "
            "FROM raw_sap.zmm_approval_log GROUP BY APPR_STATUS ORDER BY 1"
        ).fetchall()
        for status, filled, total in r:
            print(f"    {status}: {filled}/{total} ({filled/total*100:.1f}%)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
