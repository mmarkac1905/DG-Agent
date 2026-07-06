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
