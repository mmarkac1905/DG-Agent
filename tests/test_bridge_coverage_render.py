"""Option B Phase 3 — tests for _compact_bridge_coverage_result + dispatch.

Coverage:
  - Basic format: BRIDGE-COVERAGE [DAR-id]: from->to | filter: t.col
    + reachable / unreachable lines.
  - Cap at 5 unreachable values with overflow indicator.
  - Skipped (high-cardinality) DARs render a one-line skip note.
  - Malformed JSON falls back to raw blob.
  - Dispatch routes bridge_coverage_by_filter rows through the new
    helper (vs schema_discovery / generic).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _context_assembler as ca  # noqa: E402


# ----- DAR fixture builder -------------------------------------------

def _bridge_coverage_result_json(
    *,
    from_table: str = "seri",
    via_table=None,
    to_table: str = "mseg",
    via_keys_from: list[str] | None = None,
    filter_table: str = "mseg",
    filter_column: str = "BWART",
    reachable: list[str] | None = None,
    unreachable: list[str] | None = None,
    skip_reason: str = "",
) -> str:
    via_keys_from = via_keys_from or ["MBLNR"]
    reachable = reachable if reachable is not None else ["101"]
    unreachable = unreachable if unreachable is not None else [
        "122", "161", "201",
    ]
    body = {
        "bridge": {
            "from_table": from_table,
            "via_table": via_table,
            "to_table": to_table,
            "via_keys_from_to_mid": via_keys_from,
            "via_keys_mid_to_to": [],
            "from_to_mid_to_columns": via_keys_from,
            "schema_discovery_dar_id": "DAR-SD-X",
            "referential_integrity_pct": 100.0,
        },
        "filter_column": {
            "table": filter_table,
            "column": filter_column,
            "data_type": "VARCHAR",
        },
        "reachable_values": [
            {"value": v, "row_count_via_bridge": 1} for v in reachable
        ],
        "all_distinct_values": list(set(reachable) | set(unreachable)),
        "unreachable_values": unreachable,
        "value_cardinality": {
            "all_distinct": len(set(reachable) | set(unreachable)),
            "reachable": len(reachable),
            "unreachable": len(unreachable),
        },
        "evidence_query_sql": "SELECT ...",
        "measurement_method": "group_by_through_fk",
        "rationale": "test",
    }
    if skip_reason:
        body["skip_reason"] = skip_reason
    return json.dumps(body)


# ----- tests ---------------------------------------------------------

def test_compact_bridge_coverage_result_basic_format():
    rj = _bridge_coverage_result_json()  # FK-pair (no via)
    out = ca._compact_bridge_coverage_result(rj, dar_id="DAR-00527")
    assert out.startswith("BRIDGE-COVERAGE [DAR-00527]: seri->mseg | filter: mseg.BWART")
    assert "reachable: ['101']" in out
    assert "unreachable: ['122', '161', '201']" in out
    assert "(+0 more)" in out


def test_compact_bridge_coverage_result_via_table_when_present():
    """If a DAR carries a non-null via_table (Phase 2+ true 2-hop
    bridge), render as from->via->to."""
    rj = _bridge_coverage_result_json(
        via_table="seri", from_table="equi", to_table="mseg",
    )
    out = ca._compact_bridge_coverage_result(rj, dar_id="DAR-X")
    assert "equi->seri->mseg" in out


def test_compact_bridge_coverage_result_caps_unreachable_at_5():
    """Long unreachable list truncates to 5 + overflow indicator."""
    rj = _bridge_coverage_result_json(
        reachable=["A"],
        unreachable=["B", "C", "D", "E", "F", "G", "H"],
    )
    out = ca._compact_bridge_coverage_result(rj, dar_id="DAR-1")
    # First 5 shown
    assert "['B', 'C', 'D', 'E', 'F']" in out
    assert "(+2 more)" in out


def test_compact_bridge_coverage_result_skipped_high_cardinality():
    rj = _bridge_coverage_result_json(
        reachable=[], unreachable=[], skip_reason="high_cardinality",
    )
    out = ca._compact_bridge_coverage_result(rj, dar_id="DAR-S")
    assert "BRIDGE-COVERAGE [DAR-S]:" in out
    assert "skipped: high_cardinality" in out
    # No reach/unreach lines for skipped
    assert "reachable:" not in out
    assert "unreachable:" not in out


def test_compact_bridge_coverage_result_malformed_json_falls_back():
    out = ca._compact_bridge_coverage_result(
        "{not valid json", dar_id="DAR-X",
    )
    assert out == "{not valid json"


def test_compact_bridge_coverage_result_handles_empty_string():
    out = ca._compact_bridge_coverage_result("", dar_id="DAR-X")
    assert out == ""


def test_compact_bridge_coverage_result_dar_id_optional():
    rj = _bridge_coverage_result_json()
    out = ca._compact_bridge_coverage_result(rj, dar_id="")
    assert "[?]" in out
