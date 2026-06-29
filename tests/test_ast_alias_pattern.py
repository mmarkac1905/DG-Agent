"""8.4.8 Part 1 unit tests for _OUTPUT_ALIAS_PATTERN.

Verifies the tightened regex rejects numeric tokens from CAST type args
while still matching real SELECT output aliases. Run standalone:
  python tests/test_ast_alias_pattern.py

Exit 0 on all-pass, 1 on any failure. No pytest dependency — keeps the
test harness trivially portable.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

# Import the pattern from the runner (freshly-compiled regex)
from run_term_injection import _OUTPUT_ALIAS_PATTERN  # noqa: E402


CASES: list[tuple[str, list[str]]] = [
    # (SQL, expected alias names captured by _OUTPUT_ALIAS_PATTERN)
    # Part 1 acceptance cases (new regressions)
    (
        "SELECT CAST(x AS DECIMAL(13,2)) AS metric FROM t",
        ["DECIMAL", "metric"],
        # Note: 'DECIMAL' is still matched because it starts with alpha;
        # but '0', '13', '2' are NOT matched (numeric). This satisfies
        # the Part 1 goal — no numeric false-positives reaching the audit.
    ),
    (
        "SELECT CAST(y AS INTEGER) AS counter FROM t",
        ["INTEGER", "counter"],
    ),
    (
        "SELECT CAST(z AS VARCHAR(100)) FROM t",
        ["VARCHAR"],
    ),
    (
        "SELECT AVG(x) AS avg_x FROM t",
        ["avg_x"],
    ),
    (
        "SELECT CAST(v AS DOUBLE) AS dollars, CAST(w AS INT) AS cents FROM t",
        ["DOUBLE", "dollars", "INT", "cents"],
    ),
    # Regression — real alias patterns from BG028 three-way-match SQL
    (
        "SELECT ekko.LIFNR AS vendor_id, SUM(ekpo.MENGE) AS po_qty FROM t",
        ["vendor_id", "po_qty"],
    ),
    # 8.4.7 actual SQL fragment that tripped the regex pre-fix
    (
        "CAST(STRFTIME(ekko.BEDAT, '%Y-%m-01') AS DATE) AS month",
        ["DATE", "month"],
    ),
    # NO false positive on numeric-only alias
    (
        "SELECT 1 AS 100 FROM t",   # invalid SQL but confirms regex skips it
        [],
    ),
]


def run() -> int:
    failed = 0
    for i, (sql, expected) in enumerate(CASES, 1):
        matches = _OUTPUT_ALIAS_PATTERN.findall(sql)
        if matches == expected:
            print(f"  [pass] case {i}: {expected!r}")
        else:
            failed += 1
            print(f"  [FAIL] case {i}: SQL={sql!r}")
            print(f"           expected={expected!r}")
            print(f"           got     ={matches!r}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
