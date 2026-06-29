"""Option B Phase 2 — tests for the runtime bridge_coverage gate.

Coverage:
  Pure-function:
    - layer normalization: stg_sap__X -> X (and main_staging-prefixed)
    - join chain extraction: simple two-table + CTE-wrapped
    - filter extraction: =, IN, skip column-to-column EQ, skip range/LIKE
    - F-2 subset-match: DAR via_keys subset of SQL keys -> match
    - F-3 reachability: '=' on unreachable refuses; 'IN' all-unreachable
      refuses; 'IN' mixed warns (doesn't refuse)

  Integration with mocked DARs (dars_override path):
    - BAR-00003 iter-2 SQL pattern with mock DAR-00527-equivalent ->
      gate refuses (OQ-B-canonical case).
    - No DARs in scope -> falls through, returns pass with status
      'skipped_no_dars'.
    - Unparseable SQL -> falls through, returns pass with status
      'skipped_parse_error'.
    - Filter on a table with no matching bridge -> falls through.
"""
from __future__ import annotations

import sys
from pathlib import Path

import sqlglot

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _bridge_coverage_gate as g  # noqa: E402


# ----- DAR fixture builder -------------------------------------------

def _bridge_coverage_dar(
    *,
    dar_id: str = "DAR-00527",
    from_table: str = "seri",
    to_table: str = "mseg",
    via_keys_from: list[str] | None = None,
    via_keys_to: list[str] | None = None,
    filter_column: str = "BWART",
    reachable: list[str] | None = None,
    all_distinct: list[str] | None = None,
) -> dict:
    """Build a Phase-1-shape bridge_coverage_by_filter DAR dict."""
    via_keys_from = via_keys_from or ["MBLNR"]
    via_keys_to = via_keys_to or ["MBLNR"]
    reachable = reachable if reachable is not None else ["101"]
    all_distinct = all_distinct if all_distinct is not None else [
        "101", "122", "161", "201",
    ]
    unreachable = sorted(set(all_distinct) - set(reachable))
    return {
        "_dar_id": dar_id,
        "bridge": {
            "from_table": from_table,
            "via_table": None,
            "to_table": to_table,
            "via_keys_from_to_mid": via_keys_from,
            "via_keys_mid_to_to": [],
            "from_to_mid_to_columns": via_keys_to,
            "schema_discovery_dar_id": "DAR-SD-X",
            "referential_integrity_pct": 100.0,
        },
        "filter_column": {
            "table": to_table,
            "column": filter_column,
            "data_type": "VARCHAR",
        },
        "reachable_values": [
            {"value": v, "row_count_via_bridge": 1} for v in reachable
        ],
        "all_distinct_values": list(all_distinct),
        "unreachable_values": unreachable,
        "value_cardinality": {
            "all_distinct": len(all_distinct),
            "reachable": len(reachable),
            "unreachable": len(unreachable),
        },
        "evidence_query_sql": "SELECT ...",
        "measurement_method": "group_by_through_fk",
        "rationale": "fixture DAR",
    }


# ----- pure-function tests -------------------------------------------

def test_normalize_layer_stg_sap_passthrough():
    assert g._normalize_layer("stg_sap__seri") == "seri"
    assert g._normalize_layer("stg_sap__mseg") == "mseg"


def test_normalize_layer_main_staging_prefix():
    """main_staging.stg_sap__X is parsed by sqlglot into Table.db +
    Table.name; _normalize_layer operates on the bare name. The s2t
    validator's _resolve_join_side already strips main_staging."""
    # Directly testing the helper: it operates on the table name only.
    assert g._normalize_layer("stg_sap__seri") == "seri"
    # Pass-through for non-staging names
    assert g._normalize_layer("equi") == "equi"
    assert g._normalize_layer("EQUI") == "equi"


def test_extract_join_chain_simple_two_table_join():
    sql = """
        SELECT s.MBLNR, m.BWART
        FROM raw_sap.seri s
        JOIN raw_sap.mseg m ON s.MBLNR = m.MBLNR AND s.ZEILE = m.ZEILE
    """
    joins = g._extract_join_chain(sql)
    assert len(joins) == 1
    j = joins[0]
    assert j["left_table"] == "seri"
    assert j["right_table"] == "mseg"
    keypairs = {(a.upper(), b.upper()) for (a, b) in j["join_keys"]}
    assert ("MBLNR", "MBLNR") in keypairs
    assert ("ZEILE", "ZEILE") in keypairs


def test_extract_join_chain_cte_wrapped():
    """BAR-00003-iter-2-shaped CTE: seri-mseg join inside a CTE body,
    referenced from the outer SELECT. F-5: sqlglot's flattening makes
    the inner join visible to extraction."""
    sql = """
        WITH latest_movements AS (
            SELECT s.MBLNR, m.BWART, m.ZEILE
            FROM main_staging.stg_sap__seri s
            JOIN main_staging.stg_sap__mseg m
              ON s.MBLNR = m.MBLNR AND s.ZEILE = m.ZEILE
            WHERE m.BWART IN ('101', '122', '161', '201')
        )
        SELECT lm.BWART
        FROM latest_movements lm
        WHERE lm.BWART = '201'
    """
    joins = g._extract_join_chain(sql)
    pairs = {
        (j["left_table"], j["right_table"]) for j in joins
    }
    assert ("seri", "mseg") in pairs


def test_extract_equality_filters_eq():
    sql = """
        SELECT m.BWART
        FROM raw_sap.seri s
        JOIN raw_sap.mseg m ON s.MBLNR = m.MBLNR
        WHERE m.BWART = '201'
    """
    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    flat = g._flatten_ctes(parsed)
    alias_map = g._build_alias_to_table(flat)
    filters = g._extract_equality_filters(flat, alias_map)
    eq = [f for f in filters if f["operator"] == "="]
    assert len(eq) == 1
    assert eq[0]["raw_table"] == "mseg"
    assert eq[0]["column"] == "BWART"
    assert eq[0]["values"] == ["201"]


def test_extract_equality_filters_in():
    sql = """
        SELECT m.BWART
        FROM raw_sap.seri s
        JOIN raw_sap.mseg m ON s.MBLNR = m.MBLNR
        WHERE m.BWART IN ('101', '122', '161', '201')
    """
    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    flat = g._flatten_ctes(parsed)
    alias_map = g._build_alias_to_table(flat)
    filters = g._extract_equality_filters(flat, alias_map)
    in_filters = [f for f in filters if f["operator"] == "IN"]
    assert len(in_filters) == 1
    f = in_filters[0]
    assert f["raw_table"] == "mseg"
    assert f["column"] == "BWART"
    assert set(f["values"]) == {"101", "122", "161", "201"}


def test_extract_equality_filters_skips_like_range_and_non_allowlist():
    """Range, LIKE, and non-allowlist columns aren't extracted."""
    sql = """
        SELECT s.MBLNR
        FROM raw_sap.seri s
        JOIN raw_sap.mseg m ON s.MBLNR = m.MBLNR
        WHERE m.MBLNR LIKE 'GR%'
          AND m.MJAHR > '2024'
          AND m.SOMETHING = 'x'
    """
    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    flat = g._flatten_ctes(parsed)
    alias_map = g._build_alias_to_table(flat)
    filters = g._extract_equality_filters(flat, alias_map)
    # MBLNR LIKE: not extracted (LIKE not handled).
    # MJAHR > '2024': not extracted (range).
    # m.SOMETHING = 'x': in allowlist? SOMETHING is not. Skipped.
    assert filters == []


def test_match_dar_subset_match_via_keys():
    """F-2: DAR via_keys MBLNR is a subset of SQL join_keys
    {MBLNR, ZEILE}; subset-match passes -> DAR is returned."""
    bridge = {
        "left_table": "seri",
        "right_table": "mseg",
        "join_keys": [("MBLNR", "MBLNR"), ("ZEILE", "ZEILE")],
    }
    dar_mblnr = _bridge_coverage_dar(
        dar_id="DAR-MBLNR", via_keys_from=["MBLNR"],
        via_keys_to=["MBLNR"],
    )
    dar_zeile = _bridge_coverage_dar(
        dar_id="DAR-ZEILE", via_keys_from=["ZEILE"],
        via_keys_to=["ZEILE"], reachable=["101", "122", "161", "201"],
    )
    dar_other = _bridge_coverage_dar(
        dar_id="DAR-OTHER", via_keys_from=["MATNR"],
        via_keys_to=["MATNR"],
    )
    matches = g._match_dar(
        [dar_mblnr, dar_zeile, dar_other], bridge, "BWART",
    )
    ids = {m["_dar_id"] for m in matches}
    assert "DAR-MBLNR" in ids
    assert "DAR-ZEILE" in ids
    assert "DAR-OTHER" not in ids  # MATNR not in SQL keys


def test_check_reachability_eq_unreachable_refuses():
    dar = _bridge_coverage_dar()  # reachable=['101'], unreachable rest
    action, msg = g._check_reachability(dar, "=", ["201"])
    assert action == "refuse"
    assert "unreachable" in msg.lower()
    assert "DAR-00527" in msg


def test_check_reachability_eq_reachable_passes():
    dar = _bridge_coverage_dar()
    action, msg = g._check_reachability(dar, "=", ["101"])
    assert action is None


def test_check_reachability_in_all_unreachable_refuses():
    dar = _bridge_coverage_dar()  # unreachable=['122','161','201']
    action, msg = g._check_reachability(
        dar, "IN", ["122", "161", "201"],
    )
    assert action == "refuse"
    assert "ALL values unreachable" in msg


def test_check_reachability_in_mixed_warns_doesnt_refuse():
    dar = _bridge_coverage_dar()  # reachable=['101'], unreachable rest
    action, msg = g._check_reachability(
        dar, "IN", ["101", "122", "161", "201"],
    )
    assert action == "warn"
    assert "unreachable value(s)" in msg


# ----- integration tests via dars_override --------------------------

def test_gate_against_BAR_00003_iter2_pattern_refuses():
    """OQ-B-canonical case: SQL filters BWART='201' through seri-mseg
    join (single MBLNR key). Mock DAR-00527-equivalent shows BWART='201'
    unreachable. Gate refuses."""
    sql = """
        SELECT m.BWART
        FROM raw_sap.seri s
        JOIN raw_sap.mseg m ON s.MBLNR = m.MBLNR
        WHERE m.BWART = '201'
    """
    dars = [_bridge_coverage_dar()]
    passed, violations, status = g.bridge_coverage_gate(
        sql, ["seri", "mseg"], conn=None, dars_override=dars,
    )
    assert passed is False
    assert status == "fail"
    assert len(violations) == 1
    assert "201" in violations[0]
    assert "DAR-00527" in violations[0]


def test_gate_BAR_00003_iter2_cte_wrapped_pattern_refuses():
    """Full BAR-00003-iter-2 shape: CTE wraps the seri-mseg join +
    inner BWART IN clause; outer filter BWART='201'. Gate refuses on
    the outer = filter (the IN list is mixed-warn, doesn't trigger
    refuse on its own)."""
    sql = """
        WITH lm AS (
            SELECT s.MBLNR, m.BWART, m.ZEILE
            FROM main_staging.stg_sap__seri s
            JOIN main_staging.stg_sap__mseg m
              ON s.MBLNR = m.MBLNR AND s.ZEILE = m.ZEILE
            WHERE m.BWART IN ('101', '122', '161', '201')
        )
        SELECT lm.BWART
        FROM lm
        WHERE lm.BWART = '201'
    """
    dars = [_bridge_coverage_dar()]
    passed, violations, status = g.bridge_coverage_gate(
        sql, ["seri", "mseg"], conn=None, dars_override=dars,
    )
    assert passed is False
    assert status == "fail"
    assert any("201" in v for v in violations)


def test_gate_no_dars_in_scope_falls_through():
    sql = """
        SELECT m.BWART FROM raw_sap.seri s
        JOIN raw_sap.mseg m ON s.MBLNR = m.MBLNR
        WHERE m.BWART = '201'
    """
    passed, violations, status = g.bridge_coverage_gate(
        sql, ["seri", "mseg"], conn=None, dars_override=[],
    )
    assert passed is True
    assert violations == []
    assert status == "skipped_no_dars"


def test_gate_unparseable_sql_falls_through():
    dars = [_bridge_coverage_dar()]
    passed, violations, status = g.bridge_coverage_gate(
        "SELECT FROM WHERE )))) BAD SQL",
        ["seri", "mseg"], conn=None, dars_override=dars,
    )
    assert passed is True
    assert violations == []
    assert status == "skipped_parse_error"


def test_gate_filter_no_matching_bridge_falls_through():
    """Filter on mseg.BWART exists, but there's no join in the SQL
    where right_table=mseg. Falls through (gate cannot disprove what
    it has no evidence for)."""
    sql = """
        SELECT BWART FROM raw_sap.mseg
        WHERE BWART = '201'
    """
    dars = [_bridge_coverage_dar()]
    passed, violations, status = g.bridge_coverage_gate(
        sql, ["seri", "mseg"], conn=None, dars_override=dars,
    )
    assert passed is True
    assert status == "pass"
    assert violations == []


# ----- C3+C4 validation follow-up: unqualified outer column ---------

def test_gate_refuses_in_list_then_narrow_unqualified_outer_filter():
    """C3+C4 validation regression. BAR-00008/BAR-00009 SQL pattern:
    inner CTE with mixed-reachability `IN`-list, outer SELECT with
    unqualified narrow `BWART = '201'`. Pre-fix the gate dropped the
    outer predicate on empty alias and only the inner mixed `IN` fired
    (WARN, no refuse). Post-fix the unqualified outer column resolves
    via single-source CTE projection (m.BWART → mseg.BWART) and the
    `=` branch refuses on the unreachable value."""
    sql = """
        WITH latest_movement_per_equipment AS (
            SELECT
                eq.EQUNR, eq.SERGE, m.BWART, h.BUDAT,
                ROW_NUMBER() OVER (
                    PARTITION BY eq.EQUNR
                    ORDER BY h.BUDAT DESC, m.ZEILE DESC
                ) AS rn
            FROM main_staging.stg_sap__equi eq
            INNER JOIN main_staging.stg_sap__seri seri
                ON eq.EQUNR = seri.EQUNR
            INNER JOIN main_staging.stg_sap__mseg m
                ON seri.MBLNR = m.MBLNR AND seri.ZEILE = m.ZEILE
            INNER JOIN main_staging.stg_sap__mkpf h
                ON m.MBLNR = h.MBLNR AND m.MJAHR = h.MJAHR
            WHERE eq.EQART = 'CPE'
              AND m.BWART IN ('101', '201', '161', '122')
        )
        SELECT COUNT(DISTINCT EQUNR) AS active_deployed_cpe_count
        FROM latest_movement_per_equipment
        WHERE rn = 1 AND BWART = '201'
    """
    dars = [_bridge_coverage_dar()]
    passed, violations, status = g.bridge_coverage_gate(
        sql, ["equi", "mkpf", "mseg", "objk", "seri"],
        conn=None, dars_override=dars,
    )
    assert passed is False
    assert status == "fail"
    # The outer = filter on the unreachable value '201' should refuse.
    assert any(
        "BWART='201'" in v and "unreachable" in v and "DAR-00527" in v
        for v in violations
    ), f"expected BWART='201' refuse violation, got: {violations}"


def test_gate_qualified_outer_reference_still_refuses():
    """Regression for BAR-00003 iter-2 pattern. The unqualified-fix must
    not break the existing qualified-outer path (`lm.BWART = '201'`
    style). This is a near-duplicate of
    test_gate_BAR_00003_iter2_cte_wrapped_pattern_refuses earlier in
    this file, retained here as an explicit regression marker tied to
    the C3+C4 validation fix."""
    sql = """
        WITH lm AS (
            SELECT s.MBLNR, m.BWART, m.ZEILE
            FROM main_staging.stg_sap__seri s
            JOIN main_staging.stg_sap__mseg m
              ON s.MBLNR = m.MBLNR AND s.ZEILE = m.ZEILE
            WHERE m.BWART IN ('101', '122', '161', '201')
        )
        SELECT lm.BWART
        FROM lm
        WHERE lm.BWART = '201'
    """
    dars = [_bridge_coverage_dar()]
    passed, violations, status = g.bridge_coverage_gate(
        sql, ["seri", "mseg"], conn=None, dars_override=dars,
    )
    assert passed is False
    assert status == "fail"
    assert any("201" in v for v in violations)


def test_resolve_unqualified_filter_single_source_resolves():
    """Direct unit test of _resolve_unqualified_filter on the
    BAR-00008 CTE shape. One Subquery projecting BWART from
    `m` (= mseg) → resolver returns 'mseg'."""
    parsed = sqlglot.parse_one(
        """
        WITH cte AS (
            SELECT m.BWART
            FROM main_staging.stg_sap__seri s
            JOIN main_staging.stg_sap__mseg m
              ON s.MBLNR = m.MBLNR
        )
        SELECT BWART FROM cte WHERE BWART = '201'
        """,
        dialect="duckdb",
    )
    from _s2t_cardinality_validator import _flatten_ctes  # noqa: E402
    flat = _flatten_ctes(parsed)
    assert g._resolve_unqualified_filter(flat, "BWART") == "mseg"


def test_resolve_unqualified_filter_ambiguous_returns_none():
    """Defensive: when two Subqueries each project the same column
    name but from DIFFERENT base tables, the helper returns None
    (conservative — never guess). Synthetic SQL: CTE `a` projects
    BWART from a `seri`/`mseg` join (resolves → mseg); CTE `b`
    projects BWART from a hypothetical `equi`-keyed path that
    sqlglot will dutifully parse and the projection-tracer will
    bind to `equi`. Two distinct candidates → None."""
    parsed = sqlglot.parse_one(
        """
        WITH a AS (
            SELECT m.BWART
            FROM main_staging.stg_sap__seri s
            JOIN main_staging.stg_sap__mseg m
              ON s.MBLNR = m.MBLNR
        ),
        b AS (
            SELECT eq.BWART
            FROM main_staging.stg_sap__equi eq
        )
        SELECT BWART FROM (SELECT BWART FROM a UNION ALL SELECT BWART FROM b) u
        WHERE BWART = '201'
        """,
        dialect="duckdb",
    )
    from _s2t_cardinality_validator import _flatten_ctes  # noqa: E402
    flat = _flatten_ctes(parsed)
    # Two candidates — mseg (from CTE a) and equi (from CTE b);
    # resolver returns None.
    assert g._resolve_unqualified_filter(flat, "BWART") is None


def test_resolve_unqualified_filter_no_match_returns_none():
    """Empty candidate set: no Subquery projects the column name —
    resolver returns None (no filter extracted, fall-through)."""
    parsed = sqlglot.parse_one(
        """
        WITH cte AS (
            SELECT m.MENGE
            FROM main_staging.stg_sap__mseg m
        )
        SELECT MENGE FROM cte WHERE BWART = '201'
        """,
        dialect="duckdb",
    )
    from _s2t_cardinality_validator import _flatten_ctes  # noqa: E402
    flat = _flatten_ctes(parsed)
    # No Subquery projects BWART — resolver returns None (no falsey
    # binding, no spurious filter).
    assert g._resolve_unqualified_filter(flat, "BWART") is None
