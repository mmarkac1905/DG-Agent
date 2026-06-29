"""Direction F.3 — tests for the post-generation S2T cardinality validator.

Covers:
  - SQL parser handles dbt {{ ref(...) }} and multi-key joins.
  - Direct DAR lookup matches on pair + keys (F10-aware).
  - validate_s2t_sql passes per_record_key, rejects catastrophic_fanout.
  - Lazy on-demand analysis is invoked when no DAR exists.
  - Rejection format includes the catastrophic DAR id and a recommended
    bridge when one is available.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _s2t_cardinality_validator as v  # noqa: E402


# ─── Fixture ──────────────────────────────────────────────────────────

@pytest.fixture
def fixture_conn():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE SCHEMA main_seeds")
    for t in ("equi", "objk", "seri", "mseg", "mkpf"):
        conn.execute(f"CREATE TABLE raw_sap.{t} (col VARCHAR)")
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR, analysis_type VARCHAR, executed_at_utc TIMESTAMP,
            result_json VARCHAR, status VARCHAR, superseded_by VARCHAR,
            source_tables VARCHAR
        )
    """)
    yield conn
    conn.close()


def _add_dar(conn, dar_id: str, source_tables: str, finding: dict,
             status: str = "success", superseded_by: str = ""):
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results "
        "(id, analysis_type, executed_at_utc, result_json, status, "
        " superseded_by, source_tables) "
        "VALUES (?, 'join_cardinality', CURRENT_TIMESTAMP, ?, ?, ?, ?)",
        [dar_id, json.dumps(finding), status, superseded_by, source_tables],
    )


def _seed_bg027_minimal(conn):
    """Direct DARs: equi-objk per_record_key (EQUNR), equi-mseg
    catastrophic (MATNR direct). Bridge: equi-mseg per_record_key via SERI."""
    _add_dar(conn, "DAR-PRK-EO", "equi,objk", {
        "t1": "equi", "t2": "objk", "kind": "direct",
        "key_columns_t1": ["EQUNR"], "key_columns_t2": ["EQUNR"],
        "fanout_class": "per_record_key",
        "avg_fanout": 1.0, "stddev_fanout": 0.0, "matched_keys_ratio": 1.0,
    })
    _add_dar(conn, "DAR-CAT-EM", "equi,mseg", {
        "t1": "equi", "t2": "mseg", "kind": "direct",
        "key_columns_t1": ["MATNR"], "key_columns_t2": ["MATNR"],
        "fanout_class": "catastrophic_fanout",
        "avg_fanout": 4500.0, "stddev_fanout": 6488.22,
        "matched_keys_ratio": 1.0,
    })
    _add_dar(conn, "DAR-BRK-EM", "equi,mseg", {
        "t1": "equi", "t2": "mseg", "kind": "bridge", "bridge_via": "seri",
        "key_columns_t1": ["EQUNR"], "key_columns_t2": ["MBLNR"],
        "bridge_keys_left": ["EQUNR"], "bridge_keys_right": ["MBLNR"],
        "fanout_class": "per_record_key",
        "avg_fanout": 1.0, "stddev_fanout": 0.0, "matched_keys_ratio": 1.0,
    })


# ─── SQL parser tests ────────────────────────────────────────────────

def test_extract_joins_handles_dbt_refs():
    sql = """
    SELECT * FROM {{ ref('stg_sap__equi') }} eq
    INNER JOIN {{ ref('stg_sap__objk') }} obj ON eq.EQUNR = obj.EQUNR
    """
    joins = v.extract_joins_from_sql(sql)
    assert len(joins) == 1
    j = joins[0]
    assert j["left_table"] == "equi"
    assert j["right_table"] == "objk"
    assert j["join_keys"] == [("EQUNR", "EQUNR")]


def test_extract_joins_handles_multi_key():
    sql = """
    SELECT * FROM {{ ref('stg_sap__mseg') }} m
    INNER JOIN {{ ref('stg_sap__mkpf') }} k
      ON m.MBLNR = k.MBLNR AND m.MJAHR = k.MJAHR
    """
    joins = v.extract_joins_from_sql(sql)
    assert len(joins) == 1
    keys = sorted(joins[0]["join_keys"])
    assert keys == [("MBLNR", "MBLNR"), ("MJAHR", "MJAHR")]


def test_extract_joins_handles_multiple_joins():
    sql = """
    SELECT * FROM {{ ref('stg_sap__equi') }} eq
    INNER JOIN {{ ref('stg_sap__objk') }} obj ON eq.EQUNR = obj.EQUNR
    INNER JOIN {{ ref('stg_sap__seri') }} s ON obj.SERNR = s.SERNR
    """
    joins = v.extract_joins_from_sql(sql)
    pairs = sorted((j["left_table"], j["right_table"]) for j in joins)
    assert ("equi", "objk") in pairs
    # Second join's left can resolve as objk OR seri depending on alias chain
    assert any(p in pairs for p in (("objk", "seri"), ("seri", "objk")))


def test_extract_joins_returns_empty_on_unparseable():
    sql = "this is not valid sql ;;;;;"
    joins = v.extract_joins_from_sql(sql)
    assert joins == []


def test_extract_joins_returns_empty_on_no_joins():
    sql = "SELECT * FROM {{ ref('stg_sap__equi') }} eq"
    joins = v.extract_joins_from_sql(sql)
    assert joins == []


# ─── lookup_cardinality_dar tests ────────────────────────────────────

def test_lookup_finds_per_record_key_dar(fixture_conn):
    _seed_bg027_minimal(fixture_conn)
    dar = v.lookup_cardinality_dar(
        "equi", "objk", [("EQUNR", "EQUNR")], fixture_conn,
    )
    assert dar is not None
    assert dar.get("_dar_id") == "DAR-PRK-EO"
    assert dar.get("fanout_class") == "per_record_key"


def test_lookup_finds_catastrophic_dar(fixture_conn):
    _seed_bg027_minimal(fixture_conn)
    dar = v.lookup_cardinality_dar(
        "equi", "mseg", [("MATNR", "MATNR")], fixture_conn,
    )
    assert dar is not None
    assert dar.get("_dar_id") == "DAR-CAT-EM"
    assert dar.get("fanout_class") == "catastrophic_fanout"


def test_lookup_returns_none_when_keys_dont_match(fixture_conn):
    _seed_bg027_minimal(fixture_conn)
    dar = v.lookup_cardinality_dar(
        "equi", "objk", [("MATNR", "MATNR")], fixture_conn,
    )
    assert dar is None  # different keys than the seeded direct DAR


def test_lookup_skips_bridge_dars(fixture_conn):
    """Bridges are returned by find_recommended_bridge, not by
    lookup_cardinality_dar (which gates direct joins).
    """
    _seed_bg027_minimal(fixture_conn)
    dar = v.lookup_cardinality_dar(
        "equi", "mseg", [("EQUNR", "MBLNR")], fixture_conn,
    )
    # The bridge DAR has those keys but kind=bridge, so lookup ignores it.
    assert dar is None


# ─── find_recommended_bridge ─────────────────────────────────────────

def test_find_recommended_bridge_returns_per_record_key(fixture_conn):
    _seed_bg027_minimal(fixture_conn)
    bridge = v.find_recommended_bridge("equi", "mseg", fixture_conn)
    assert bridge is not None
    assert bridge.get("_dar_id") == "DAR-BRK-EM"
    assert bridge.get("fanout_class") == "per_record_key"
    assert bridge.get("bridge_via") == "seri"


def test_find_recommended_bridge_returns_none_when_no_bridges(fixture_conn):
    _seed_bg027_minimal(fixture_conn)
    bridge = v.find_recommended_bridge("equi", "objk", fixture_conn)
    assert bridge is None


# ─── validate_s2t_sql — pass/reject ──────────────────────────────────

def test_validates_per_record_key_passes(fixture_conn):
    _seed_bg027_minimal(fixture_conn)
    sql = """
    SELECT * FROM {{ ref('stg_sap__equi') }} eq
    INNER JOIN {{ ref('stg_sap__objk') }} obj ON eq.EQUNR = obj.EQUNR
    """
    res = v.validate_s2t_sql(sql, ["equi", "objk"], fixture_conn)
    assert res["status"] == "passed", res


def test_rejects_catastrophic_fanout(fixture_conn):
    _seed_bg027_minimal(fixture_conn)
    sql = """
    SELECT * FROM {{ ref('stg_sap__equi') }} eq
    INNER JOIN {{ ref('stg_sap__mseg') }} m ON eq.MATNR = m.MATNR
    """
    res = v.validate_s2t_sql(
        sql, ["equi", "mseg"], fixture_conn,
    )
    assert res["status"] == "rejected_catastrophic_join"
    assert res["pair"] == ("equi", "mseg")
    assert res["catastrophic_dar_id"] == "DAR-CAT-EM"
    assert res["recommended_bridge"] is not None
    assert res["recommended_bridge"]["bridge_via"] == "seri"
    assert "catastrophic_fanout" in res["hint"]
    assert "DAR-BRK-EM" in res["hint"]
    assert "seri" in res["hint"]


def test_validation_passes_on_empty_sql(fixture_conn):
    res = v.validate_s2t_sql("", ["equi"], fixture_conn)
    assert res["status"] == "passed"


def test_validation_passes_on_no_extracted_joins(fixture_conn):
    _seed_bg027_minimal(fixture_conn)
    res = v.validate_s2t_sql(
        "SELECT 1 AS x", ["equi"], fixture_conn,
    )
    assert res["status"] == "passed"


def test_validation_skips_joins_outside_scope(fixture_conn):
    """When scope_tables is provided, joins between non-scope tables are
    skipped. Restricts gating to the relevant pairs.
    """
    _seed_bg027_minimal(fixture_conn)
    sql = """
    SELECT * FROM {{ ref('stg_sap__equi') }} eq
    INNER JOIN {{ ref('stg_sap__mseg') }} m ON eq.MATNR = m.MATNR
    """
    # Pass a scope that EXCLUDES mseg → join is out-of-scope, validator
    # passes through.
    res = v.validate_s2t_sql(sql, ["equi"], fixture_conn)
    assert res["status"] == "passed"


# ─── Lazy on-demand analysis ─────────────────────────────────────────

def test_lazy_analysis_triggered_on_missing_dar(fixture_conn):
    """When no DAR exists for a pair, validate_s2t_sql calls
    trigger_lazy_analysis. We mock the trigger to verify the call site
    fires; lazy analysis returning False means no new DAR is loaded
    and the join passes through.
    """
    # Seed nothing — no DARs at all.
    sql = """
    SELECT * FROM {{ ref('stg_sap__seri') }} s
    INNER JOIN {{ ref('stg_sap__mseg') }} m ON s.MBLNR = m.MBLNR
    """
    with patch.object(v, "trigger_lazy_analysis",
                      return_value=False) as mock_trigger:
        res = v.validate_s2t_sql(
            sql, ["seri", "mseg"], fixture_conn,
        )
    assert mock_trigger.called, (
        "validate_s2t_sql must invoke trigger_lazy_analysis when no "
        "DAR exists for the pair"
    )
    # Without a successful trigger / re-lookup, the join passes through.
    assert res["status"] == "passed"


def test_lazy_analysis_persists_dar_then_revalidates(fixture_conn):
    """Simulate lazy analysis succeeding by injecting the DAR after the
    first lookup. Verifies that the second lookup (post-trigger) sees
    the new DAR and the validator gates accordingly.
    """
    sql = """
    SELECT * FROM {{ ref('stg_sap__seri') }} s
    INNER JOIN {{ ref('stg_sap__mseg') }} m ON s.MBLNR = m.MBLNR
    """

    def _fake_trigger(t1, t2, conn):
        # Pretend the analyzer ran and emitted a catastrophic DAR for
        # this pair on these keys.
        _add_dar(conn, "DAR-LAZY", "mseg,seri", {
            "t1": "mseg", "t2": "seri", "kind": "direct",
            "key_columns_t1": ["MBLNR"], "key_columns_t2": ["MBLNR"],
            "fanout_class": "catastrophic_fanout",
            "avg_fanout": 999.0, "stddev_fanout": 100.0,
            "matched_keys_ratio": 1.0,
        })
        return True

    with patch.object(v, "trigger_lazy_analysis", side_effect=_fake_trigger):
        res = v.validate_s2t_sql(
            sql, ["seri", "mseg"], fixture_conn,
        )
    assert res["status"] == "rejected_catastrophic_join"
    assert res["catastrophic_dar_id"] == "DAR-LAZY"


# ─── Hint format ─────────────────────────────────────────────────────

def test_format_retry_hint_includes_bridge(fixture_conn):
    rejection = {
        "pair": ("equi", "mseg"),
        "catastrophic_keys": [("MATNR", "MATNR")],
        "catastrophic_dar_id": "DAR-X",
        "catastrophic_avg_fanout": 4500.0,
        "recommended_bridge": {
            "bridge_via": "seri",
            "bridge_keys_left": ["EQUNR"],
            "bridge_keys_right": ["MBLNR"],
            "key_columns_t1": ["EQUNR"],
            "key_columns_t2": ["MBLNR"],
            "_dar_id": "DAR-Y",
        },
    }
    hint = v.format_retry_hint(rejection)
    assert "catastrophic_fanout" in hint
    assert "DAR-X" in hint
    assert "Use the per_record_key bridge" in hint
    assert "Bridge via seri" in hint
    assert "DAR-Y" in hint


def test_format_retry_hint_when_no_bridge():
    rejection = {
        "pair": ("a", "b"),
        "catastrophic_keys": [("X", "X")],
        "catastrophic_dar_id": "DAR-Z",
        "catastrophic_avg_fanout": 200.0,
        "recommended_bridge": None,
    }
    hint = v.format_retry_hint(rejection)
    assert "catastrophic_fanout" in hint
    assert "No per_record_key bridge currently exists" in hint


# ─── F.5 — CTE flattening ────────────────────────────────────────────

def test_extract_joins_flattens_simple_cte():
    """Outer join references a CTE alias; flattening must surface the
    join with the CTE's underlying base table.
    """
    sql = """
    WITH x AS (SELECT * FROM {{ ref('stg_sap__mseg') }})
    SELECT * FROM {{ ref('stg_sap__mkpf') }} mkpf
    INNER JOIN x ON mkpf.MBLNR = x.MBLNR
    """
    joins = v.extract_joins_from_sql(sql)
    pairs = [(j["left_table"], j["right_table"], sorted(j["join_keys"]))
             for j in joins]
    assert ("mkpf", "mseg", [("MBLNR", "MBLNR")]) in pairs


def test_extract_joins_flattens_nested_ctes():
    """Nested CTE chain (a -> mseg, b -> a JOIN equi). Flattening must
    recursively inline `a` inside `b` so the (mseg, equi) on MATNR join
    surfaces under raw_sap names.
    """
    sql = """
    WITH a AS (SELECT * FROM {{ ref('stg_sap__mseg') }}),
         b AS (
           SELECT * FROM a INNER JOIN {{ ref('stg_sap__equi') }} equi
                       ON a.MATNR = equi.MATNR
         )
    SELECT * FROM b
    """
    joins = v.extract_joins_from_sql(sql)
    pairs = {(j["left_table"], j["right_table"],
              tuple(sorted(j["join_keys"]))) for j in joins}
    # The inner (a-equi) join, after flattening a -> mseg, surfaces as
    # (mseg, equi) on MATNR=MATNR.
    assert any(
        {p[0], p[1]} == {"mseg", "equi"}
        and p[2] == (("MATNR", "MATNR"),)
        for p in pairs
    ), f"expected (mseg, equi) on MATNR; got {pairs}"


def test_extract_joins_flattens_cte_with_internal_joins():
    """BG028 pattern — CTE body contains a join between two staging
    tables. That internal (ekbe, ekpo) join must be visible to the
    parser so F.3 can gate it.
    """
    sql = """
    WITH gr_agg AS (
      SELECT ekbe.EBELN, ekbe.EBELP
      FROM {{ ref('stg_sap__ekbe') }} ekbe
      INNER JOIN {{ ref('stg_sap__ekpo') }} ekpo
        ON ekbe.EBELN = ekpo.EBELN
    ),
    invoice_agg AS (
      SELECT EBELN FROM {{ ref('stg_sap__rseg') }} rseg
    )
    SELECT * FROM gr_agg
    INNER JOIN invoice_agg ON gr_agg.EBELN = invoice_agg.EBELN
    """
    joins = v.extract_joins_from_sql(sql)
    pairs = {(j["left_table"], j["right_table"],
              tuple(sorted(j["join_keys"]))) for j in joins}
    assert any(
        {p[0], p[1]} == {"ekbe", "ekpo"}
        and p[2] == (("EBELN", "EBELN"),)
        for p in pairs
    ), f"expected (ekbe, ekpo) on EBELN; got {pairs}"


def test_extract_joins_dedupes_after_flattening():
    """Multi-referenced CTE produces duplicate internal joins after
    flattening (each reference inlines the same body). The dedup on
    (left_table, right_table, sorted keys) must collapse them.
    """
    sql = """
    WITH x AS (
      SELECT m.MATNR, eq.EQUNR
      FROM {{ ref('stg_sap__mseg') }} m
      INNER JOIN {{ ref('stg_sap__equi') }} eq ON m.MATNR = eq.MATNR
    )
    SELECT * FROM x x1 INNER JOIN x x2 ON x1.MATNR = x2.MATNR
    """
    joins = v.extract_joins_from_sql(sql)
    # The internal (mseg, equi) on MATNR appears in BOTH x1 and x2's
    # inlined bodies after flattening. Dedup must collapse to one.
    mseg_equi = [
        j for j in joins
        if {j["left_table"], j["right_table"]} == {"mseg", "equi"}
        and sorted(j["join_keys"]) == [("MATNR", "MATNR")]
    ]
    assert len(mseg_equi) == 1, (
        f"expected 1 deduped (mseg, equi) join; got {len(mseg_equi)}: "
        f"{joins}"
    )


def test_extract_joins_no_cte_unchanged():
    """Regression guard — SQL with no CTEs must produce identical
    output before/after F.5.
    """
    sql = """
    SELECT * FROM {{ ref('stg_sap__equi') }} eq
    INNER JOIN {{ ref('stg_sap__objk') }} obj ON eq.EQUNR = obj.EQUNR
    INNER JOIN {{ ref('stg_sap__mseg') }} m
      ON eq.MATNR = m.MATNR AND eq.MANDT = m.MANDT
    """
    joins = v.extract_joins_from_sql(sql)
    pairs = sorted((j["left_table"], j["right_table"]) for j in joins)
    assert ("equi", "objk") in pairs
    assert ("equi", "mseg") in pairs
    em = next(
        j for j in joins
        if (j["left_table"], j["right_table"]) == ("equi", "mseg")
    )
    assert sorted(em["join_keys"]) == [
        ("MANDT", "MANDT"), ("MATNR", "MATNR"),
    ]


def test_validator_rejects_catastrophic_join_inside_cte(fixture_conn):
    """F.5 acceptance gate — adversarial SQL hides a catastrophic
    obj.MATNR=m.MATNR join inside a CTE. The validator must surface and
    reject it citing the catastrophic DAR. This is the regression guard
    for the parser visibility gap discovered during BG028 e2e.
    """
    _seed_bg027_minimal(fixture_conn)
    # Add the catastrophic objk-mseg via MATNR DAR the test cites.
    _add_dar(fixture_conn, "DAR-CAT-OM", "mseg,objk", {
        "t1": "mseg", "t2": "objk", "kind": "direct",
        "key_columns_t1": ["MATNR"], "key_columns_t2": ["MATNR"],
        "fanout_class": "catastrophic_fanout",
        "avg_fanout": 8000.0, "stddev_fanout": 9000.0,
        "matched_keys_ratio": 1.0,
    })

    sql = """
    WITH bad AS (
      SELECT obj.EQUNR, m.MATNR
      FROM {{ ref('stg_sap__objk') }} obj
      INNER JOIN {{ ref('stg_sap__mseg') }} m ON obj.MATNR = m.MATNR
    )
    SELECT * FROM {{ ref('stg_sap__equi') }} eq
    INNER JOIN bad ON eq.EQUNR = bad.EQUNR
    """
    res = v.validate_s2t_sql(
        sql, ["equi", "objk", "mseg"], fixture_conn,
    )
    assert res["status"] == "rejected_catastrophic_join", res
    assert {res["pair"][0], res["pair"][1]} == {"objk", "mseg"}, res
    assert res["catastrophic_dar_id"] == "DAR-CAT-OM", res
