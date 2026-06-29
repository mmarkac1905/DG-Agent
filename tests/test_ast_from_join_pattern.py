"""8.5.1 Part 3 unit tests for _FROM_JOIN_PATTERN + _strip_function_from_clauses.

Verifies EXTRACT/TRIM/SUBSTRING function-arg FROM clauses don't produce
false table references in the AST audit. Run standalone:
  python tests/test_ast_from_join_pattern.py

Exit 0 on all-pass, 1 on any failure. No pytest dependency.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from run_term_injection import _extract_table_refs  # noqa: E402


CASES: list[tuple[str, list[tuple[str, str, str]]]] = [
    # (SQL, expected list of (schema, table, alias))

    # Part 3 acceptance cases — EXTRACT/TRIM/SUBSTRING should NOT produce
    # table refs for their inner column arguments.
    (
        "SELECT EXTRACT(YEAR FROM date_col) FROM t",
        [("", "t", "")],
    ),
    (
        "SELECT EXTRACT(QUARTER FROM m.goods_receipt_date) "
        "FROM main_marts.fact_goods_movements m",
        [("main_marts", "fact_goods_movements", "m")],
    ),
    (
        "SELECT TRIM(LEADING ' ' FROM col) FROM t",
        [("", "t", "")],
    ),
    (
        "SELECT SUBSTRING(name FROM 1 FOR 3) FROM users",
        [("", "users", "")],
    ),

    # Regression — existing genuine table refs still captured correctly.
    (
        "SELECT col FROM main_staging.stg_sap__ekko",
        [("main_staging", "stg_sap__ekko", "")],
    ),
    (
        "SELECT a.x, b.y FROM t1 AS a JOIN t2 AS b ON a.id = b.id",
        [("", "t1", "a"), ("", "t2", "b")],
    ),
    (
        "SELECT * FROM ekko e JOIN ekpo p ON e.EBELN = p.EBELN",
        [("", "ekko", "e"), ("", "ekpo", "p")],
    ),

    # Combined — both function-FROM and genuine FROM in same query.
    (
        "SELECT EXTRACT(YEAR FROM k.BEDAT) AS yr "
        "FROM main_staging.stg_sap__ekko k "
        "JOIN main_staging.stg_sap__ekpo p ON k.EBELN = p.EBELN",
        [
            ("main_staging", "stg_sap__ekko", "k"),
            ("main_staging", "stg_sap__ekpo", "p"),
        ],
    ),
]


def run() -> int:
    failed = 0
    for i, (sql, expected) in enumerate(CASES, 1):
        got = _extract_table_refs(sql)
        if got == expected:
            print(f"  [pass] case {i}: {expected!r}")
        else:
            failed += 1
            print(f"  [FAIL] case {i}: SQL={sql!r}")
            print(f"           expected={expected!r}")
            print(f"           got     ={got!r}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
