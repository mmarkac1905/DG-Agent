"""Unit tests for join_cardinality candidate enumeration and classification.

External review (round 3) flagged the suffix matcher as the highest-priority
fix with zero tests, and found the classifier labeling avg-0.16 fanout
'catastrophic'. These tests pin both behaviors.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from run_join_cardinality_analysis import (  # noqa: E402
    _classify,
    _suffix_name_keys,
    _SUFFIX_MAX_CANDIDATES,
)


def _cols(*names):
    return {n: "VARCHAR" for n in names}


# ─── _suffix_name_keys ─────────────────────────────────────────────────

def test_suffix_matches_zip_code_prefix():
    """The known_issue #132 case: differently-prefixed zip columns match."""
    out = _suffix_name_keys(
        _cols("customer_zip_code_prefix", "customer_city"),
        _cols("geolocation_zip_code_prefix", "geolocation_lat"),
    )
    assert ("CUSTOMER_ZIP_CODE_PREFIX", "GEOLOCATION_ZIP_CODE_PREFIX") in out


def test_suffix_excludes_exact_name_matches():
    """Exact-name columns are Source A's job — never emitted here."""
    out = _suffix_name_keys(_cols("order_id"), _cols("order_id"))
    assert out == []


def test_suffix_requires_two_tokens():
    """A single trailing token (e.g. bare 'id') must not match — that
    would pair every *_id with every other *_id."""
    out = _suffix_name_keys(_cols("customer_id"), _cols("product_id"))
    assert out == []


def test_suffix_inert_without_underscores():
    """SAP-style names (MATNR, LIFNR) produce no suffixes — Source D
    is inert on such schemas."""
    out = _suffix_name_keys(_cols("MATNR", "LIFNR"), _cols("WERKS", "EBELN"))
    assert out == []


def test_suffix_candidate_cap():
    t1 = _cols(*[f"a{i}_zip_code_prefix" for i in range(15)])
    t2 = _cols(*[f"b{i}_zip_code_prefix" for i in range(15)])
    out = _suffix_name_keys(t1, t2)
    assert len(out) <= _SUFFIX_MAX_CANDIDATES


# ─── _classify ─────────────────────────────────────────────────────────

def _m(avg, stddev, ratio, sampled=500):
    return {
        "avg_fanout": avg, "stddev_fanout": stddev,
        "matched_keys_ratio": ratio, "sampled_keys": sampled,
        "matched_keys": int(sampled * ratio), "max_fanout": int(avg * 10) or 1,
        "sample_saturated": False, "distinct_keys_smaller": sampled,
    }


def test_classify_sub_one_average_is_never_catastrophic():
    """Review round 3: avg 0.16 with spiky stddev was labeled catastrophic.
    A sub-1 average fanout cannot multiply rows."""
    assert _classify(_m(avg=0.16, stddev=0.9, ratio=0.5)) != "catastrophic_fanout"


def test_classify_high_average_is_catastrophic():
    assert _classify(_m(avg=443.0, stddev=730.0, ratio=1.0)) == "catastrophic_fanout"


def test_classify_spiky_moderate_fanout_is_catastrophic():
    """stddev > avg with genuine multiplication stays conservative."""
    assert _classify(_m(avg=4.13, stddev=19.1, ratio=1.0)) == "catastrophic_fanout"


def test_classify_per_record_key():
    assert _classify(_m(avg=1.0, stddev=0.0, ratio=1.0)) == "per_record_key"


def test_classify_header_detail():
    assert _classify(_m(avg=13.1, stddev=9.0, ratio=0.96)) == "header_detail"


def test_classify_low_match_is_no_signal():
    assert _classify(_m(avg=1.0, stddev=0.0, ratio=0.05)) == "no_signal"


# ─── citation-audit keyword coverage (BG034 root cause) ────────────────

def test_window_frame_keywords_not_flagged_as_columns():
    """BG034 hard-stopped because the citation audit flagged UNBOUNDED and
    PRECEDING (window-frame keywords) as unknown columns. Pin the full
    window vocabulary into the audit's keyword stoplist."""
    from run_term_injection import _SQL_KEYWORDS
    for kw in ("unbounded", "preceding", "following", "current", "range",
               "groups", "exclude", "ties", "window", "qualify"):
        assert kw in _SQL_KEYWORDS, f"missing window keyword: {kw}"


# ─── Source E: value-overlap probing (known_issue #137) ────────────────

def _overlap_conn():
    import duckdb
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE TABLE raw_sap.custs (cust_id VARCHAR, name VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.orders (customer_ref VARCHAR, amt DOUBLE)")
    conn.execute(
        "INSERT INTO raw_sap.custs "
        "SELECT 'C' || LPAD(CAST(i AS VARCHAR), 5, '0'), 'n' || i "
        "FROM range(1, 501) t(i)")
    conn.execute(
        "INSERT INTO raw_sap.orders "
        "SELECT 'C' || LPAD(CAST((i % 500) + 1 AS VARCHAR), 5, '0'), i * 1.5 "
        "FROM range(1, 2001) t(i)")
    return conn


def test_value_overlap_finds_differently_named_key():
    """The #137 case: cust_id <-> customer_ref share values but no name
    tokens. Value probing must find them; name heuristics cannot."""
    from run_join_cardinality_analysis import (
        _value_overlap_keys, _table_columns, _suffix_name_keys,
    )
    conn = _overlap_conn()
    c1 = _table_columns(conn, "custs")
    c2 = _table_columns(conn, "orders")
    # sanity: the naming heuristics are blind to this pair
    assert _suffix_name_keys(c1, c2) == []
    hits = _value_overlap_keys(conn, "custs", c1, "orders", c2, set())
    assert ("CUST_ID", "CUSTOMER_REF") in hits
    # non-key columns must not pair (name vs amt: different families)
    assert all(h[0] != "NAME" for h in hits)
    conn.close()


def test_value_overlap_end_to_end_candidate_and_classification():
    """Full path: Source E candidate enters the standard measurement and
    classifies header_detail (each customer has ~4 orders)."""
    from run_join_cardinality_analysis import _direct_candidates, _measure_direct
    conn = _overlap_conn()
    cands = _direct_candidates(conn, "custs", "orders")
    ve = [c for c in cands if "value_overlap" in c["source"]]
    assert any(c["key_columns_t1"] == ["CUST_ID"] and
               c["key_columns_t2"] == ["CUSTOMER_REF"] for c in ve)
    m = _measure_direct(conn, "custs", ["cust_id"], "orders", ["customer_ref"])
    from run_join_cardinality_analysis import _classify
    assert _classify(m) == "header_detail"
    conn.close()


def test_value_overlap_respects_already_covered_pairs():
    from run_join_cardinality_analysis import _value_overlap_keys, _table_columns
    conn = _overlap_conn()
    c1 = _table_columns(conn, "custs")
    c2 = _table_columns(conn, "orders")
    hits = _value_overlap_keys(conn, "custs", c1, "orders", c2,
                               {("CUST_ID", "CUSTOMER_REF")})
    assert ("CUST_ID", "CUSTOMER_REF") not in hits
    conn.close()


def test_value_overlap_excludes_measures_and_small_ints():
    """First live sweep lesson: float measures (payment_value vs weights)
    and small-int sequences (payment_sequential vs photo counts) overlap
    by coincidence. Floats never qualify; ints need key cardinality."""
    import duckdb
    from run_join_cardinality_analysis import _keyish_columns, _table_columns
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("""CREATE TABLE raw_sap.pay (
        pay_key VARCHAR, seq INTEGER, installments INTEGER, amount DOUBLE)""")
    conn.execute(
        "INSERT INTO raw_sap.pay "
        "SELECT 'P' || i, (i % 25) + 1, (i % 12) + 1, i * 1.37 "
        "FROM range(1, 2001) t(i)")
    cols = _table_columns(conn, "pay")
    keyish = _keyish_columns(conn, "pay", cols)
    assert "pay_key" in keyish            # string key qualifies
    assert "seq" not in keyish            # small-int sequence: below floor
    assert "installments" not in keyish   # small-int: below floor
    assert "amount" not in keyish         # float measure: never
    conn.close()
