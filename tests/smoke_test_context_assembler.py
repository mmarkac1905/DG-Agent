"""Gate A integration smoke test — run against the live project DB.

Invocation: python tests/smoke_test_context_assembler.py

Not a pytest unit test. Exercises the helper against real seeds +
real raw_sap so we know the plumbing works with actual data. The unit
tests use fixture DBs and are pytest-driven.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _context_assembler import assemble_context  # noqa: E402


def main() -> int:
    bundle = assemble_context(
        purpose="eda_sql_generation",
        scope_tables=["ekpo"],
        max_tokens=20_000,
        strict=True,
        include_debug_metadata=True,
    )

    failures: list[str] = []
    if bundle.token_count <= 0:
        failures.append(f"token_count {bundle.token_count} <= 0")
    if "static" not in bundle.layer_summary:
        failures.append("layer_summary missing 'static'")
    elif bundle.layer_summary["static"] <= 0:
        failures.append(
            f"layer_summary['static'] = {bundle.layer_summary['static']} "
            "(expected > 0 because source_column_roles has ekpo rows from piece 3)"
        )
    expected_strategies = {"explicit", "s1", "s2", "s3"}
    if bundle.scope_resolution["strategy_used"] not in expected_strategies:
        failures.append(
            f"strategy_used {bundle.scope_resolution['strategy_used']!r} "
            f"not in {expected_strategies}"
        )

    print("=" * 60)
    print("Gate A smoke test")
    print("=" * 60)
    print(f"purpose:          eda_sql_generation")
    print(f"scope:            ['ekpo']")
    print(f"strategy_used:    {bundle.scope_resolution['strategy_used']}")
    print(f"resolved_tables:  {bundle.scope_resolution['resolved_tables']}")
    print(f"total tokens:     {bundle.token_count}")
    print(f"layer_summary:    {bundle.layer_summary}")
    print(f"fingerprint:      {bundle.debug['fingerprint']}")
    print(f"static details:   {bundle.debug['layer_details']['static']}")
    print(f"dynamic details:  {bundle.debug['layer_details']['dynamic']}")
    print(f"examples details: {bundle.debug['layer_details']['examples']}")
    print(f"business details: {bundle.debug['layer_details']['business']}")
    print()

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — all 4 smoke-test assertions held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
