"""Phase 2a unit tests for scripts/c5_validation.py.

Validates the Component 4 classification matrix + grade synthesis rules
against the empirical Q2 cases (tasks/c5_q2_validation_accuracy.md):
SER01 → C, SER02 → A, SER03 → C, EQUZ → A, ITOB → D, MARA → B, EKKO → B.

All tests use in-memory mock catalog + raw_sap dicts — no DuckDB
connection, no CSV file I/O. Pure-function design (jaccard, classify,
synthesize_grade) makes the validation layer testable without external
state.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from c5_validation import (  # noqa: E402
    JACCARD_THRESHOLD,
    ValidatedRecommendation,
    classify,
    jaccard,
    synthesize_grade,
    validate_recommendations,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal catalog + raw_sap mirroring the Q2 viability cases
# ---------------------------------------------------------------------------

# Catalog rows mirror sap_table_catalog.csv schema (key_fields as comma-
# separated string, brief_description as plain text). Only the fields
# the validation layer reads are populated.
def _row(table: str, desc: str, key_fields: str) -> dict:
    return {
        "table_name": table,
        "brief_description": desc,
        "key_fields": key_fields,
    }


Q2_CATALOG: dict[str, dict] = {
    # SER01 — SD Delivery serial-number doc header. Reference column set
    # is SD-flavored; seed is PO-flavored → low jaccard expected.
    "ser01": _row(
        "SER01",
        "Document Header for Serial Numbers in Deliveries",
        "MANDT, OBKNR, LIEF_NR, POSNR, ANZSN, DATUM, UZEIT, ERNAM",
    ),
    # SER02 — Maint.Contract / SD Order. Not in seed → case A.
    "ser02": _row(
        "SER02",
        "Document Header for Serial Nos for Maint.Contract (SD Order)",
        "MANDT, OBKNR, VBELN, POSNR, ANZSN, DATUM, UZEIT, ERNAM",
    ),
    # SER03 — Goods Movements doc index. Seed has thin slice → divergence.
    "ser03": _row(
        "SER03",
        "Document Header for Serial Numbers for Goods Movements",
        "MANDT, OBKNR, MBLNR, MJAHR, ZEILE, BWART, ANZSN, DATUM, UZEIT, ERNAM",
    ),
    # EQUZ — Equipment time segment. Not in seed → case A.
    "equz": _row(
        "EQUZ",
        "Equipment time segment",
        "MANDT, EQUNR, DATBI, DATAB, INVNR, GROES, BRGEW",
    ),
    # MARA — material master, control case (B).
    "mara": _row(
        "MARA",
        "General Material Data",
        "MANDT, MATNR, ERSDA, ERNAM, LAEDA, AENAM, MTART, MBRSH",
    ),
    # EKKO — PO header, cleanest control (B).
    "ekko": _row(
        "EKKO",
        "Purchasing Document Header",
        "MANDT, EBELN, BUKRS, BSTYP, BSART, LOEKZ, STATU, AEDAT, ERNAM",
    ),
    # ITOB intentionally absent — Q2 finding #1: it's a VIEW filtered by
    # the scraper, so it should NOT appear in the catalog. Case D.
}

Q2_RAW_SAP: dict[str, list[str]] = {
    # Seed SER01 — PO-flavored 4-col slice; 1 of 8 catalog keys overlap → 0.043
    "ser01": ["MANDT", "OBKNR", "OBZAE", "EBELN"],
    # SER02 not in seed (case A).
    # Seed SER03 — thin 8-col slice; 3 of 10 catalog keys overlap → 0.130
    "ser03": ["OBKNR", "MBLNR", "ZEILE", "BWART", "MATNR", "WERKS", "LGORT", "MENGE"],
    # EQUZ not in seed (case A).
    # Seed MARA — control with ERDAT alias artifact; jaccard ~0.438
    "mara": [
        "MANDT", "MATNR", "ERDAT", "ERNAM", "LAEDA", "AENAM",
        "MTART", "MBRSH", "MEINS", "BRGEW", "NTGEW",
    ],
    # Seed EKKO — clean control; jaccard ~0.476
    "ekko": [
        "MANDT", "EBELN", "BUKRS", "BSTYP", "BSART", "LOEKZ",
        "STATU", "AEDAT", "ERNAM", "LIFNR", "WAERS",
    ],
}


# ---------------------------------------------------------------------------
# (a) Pure-function tests — jaccard
# ---------------------------------------------------------------------------

def test_jaccard_empty_sets() -> None:
    assert jaccard(set(), set()) == 0.0


def test_jaccard_disjoint() -> None:
    assert jaccard({"A", "B"}, {"X", "Y"}) == 0.0


def test_jaccard_identical() -> None:
    assert jaccard({"A", "B", "C"}, {"A", "B", "C"}) == 1.0


def test_jaccard_partial_overlap() -> None:
    # 2 shared / 4 union = 0.5
    assert jaccard({"A", "B", "C"}, {"B", "C", "D"}) == 0.5


# ---------------------------------------------------------------------------
# (a) Pure-function tests — synthesize_grade
# ---------------------------------------------------------------------------

def test_synthesize_grade_case_a_high() -> None:
    assert synthesize_grade("A", "high") == "verified"


def test_synthesize_grade_case_a_medium() -> None:
    assert synthesize_grade("A", "medium") == "verified"


def test_synthesize_grade_case_a_low() -> None:
    assert synthesize_grade("A", "low") == "verified_low_priority"


def test_synthesize_grade_case_b_scope_review() -> None:
    # B = LLM recommended a table already in scope — flag for analyst.
    assert synthesize_grade("B", "high") == "scope_review_needed"


def test_synthesize_grade_case_c() -> None:
    assert synthesize_grade("C", "high") == "divergence_warning"


def test_synthesize_grade_case_d() -> None:
    assert synthesize_grade("D", "high") == "unverified"


# ---------------------------------------------------------------------------
# (b) Classification tests — Q2 cases against mock catalog + raw_sap
# ---------------------------------------------------------------------------

def test_classify_case_a_ser02() -> None:
    """SER02 — catalog YES, raw_sap NO → recommend ingestion."""
    cls, cat_row, seed_cols, cat_keys = classify("SER02", Q2_CATALOG, Q2_RAW_SAP)
    assert cls == "A"
    assert cat_row is not None
    assert seed_cols is None
    assert "VBELN" in cat_keys


def test_classify_case_a_equz() -> None:
    """EQUZ — catalog YES, raw_sap NO → recommend ingestion."""
    cls, cat_row, seed_cols, _ = classify("EQUZ", Q2_CATALOG, Q2_RAW_SAP)
    assert cls == "A"
    assert cat_row["table_name"] == "EQUZ"
    assert seed_cols is None


def test_classify_case_b_mara() -> None:
    """MARA — control table, catalog YES, raw_sap YES, columns ~match → B.

    Empirical jaccard from Q2 = 0.438 (above 0.30 threshold).
    """
    cls, cat_row, seed_cols, cat_keys = classify("MARA", Q2_CATALOG, Q2_RAW_SAP)
    assert cls == "B"
    assert cat_row is not None
    assert seed_cols is not None
    assert cat_keys is not None


def test_classify_case_b_ekko() -> None:
    """EKKO — cleanest control. Empirical jaccard ~0.476 → B."""
    cls, _, _, _ = classify("EKKO", Q2_CATALOG, Q2_RAW_SAP)
    assert cls == "B"


def test_classify_case_c_ser01() -> None:
    """SER01 — known divergent. Seed PO-flavored vs catalog SD-flavored.

    Empirical jaccard from Q2 = 0.043 (well below 0.30) → C.
    """
    cls, cat_row, seed_cols, _ = classify("SER01", Q2_CATALOG, Q2_RAW_SAP)
    assert cls == "C"
    assert cat_row is not None
    assert seed_cols is not None


def test_classify_case_c_ser03() -> None:
    """SER03 — thin slice of right table.

    Empirical jaccard from Q2 = 0.130 (below 0.30) → C.
    """
    cls, _, _, _ = classify("SER03", Q2_CATALOG, Q2_RAW_SAP)
    assert cls == "C"


def test_classify_case_d_itob() -> None:
    """ITOB — sapdatasheet.org page exists but is a VIEW; the scraper
    filters it out so it does not appear in the catalog. Q2 finding #1.
    Catalog NO → D (likely hallucinated)."""
    cls, cat_row, seed_cols, cat_keys = classify("ITOB", Q2_CATALOG, Q2_RAW_SAP)
    assert cls == "D"
    assert cat_row is None
    assert seed_cols is None
    assert cat_keys is None


def test_classify_case_insensitive() -> None:
    """Catalog/raw_sap lookup is case-insensitive (LLM may emit lowercase)."""
    cls, _, _, _ = classify("ekko", Q2_CATALOG, Q2_RAW_SAP)
    assert cls == "B"


# ---------------------------------------------------------------------------
# Threshold sanity — boundary behavior at JACCARD_THRESHOLD
# ---------------------------------------------------------------------------

def test_threshold_constant_matches_q2_calibration() -> None:
    """Brief commits to 0.30 per Q2 stable plateau [0.15, 0.40]."""
    assert JACCARD_THRESHOLD == 0.30


# ---------------------------------------------------------------------------
# (c) End-to-end validate_recommendations
# ---------------------------------------------------------------------------

def test_validate_full_flow_mixed_recommendations() -> None:
    """Mixed A/B/C/D LLM output → validated structure with correct counts.

    Uses the Q2 viability case mix: 2 case A (SER02, EQUZ),
    1 case B (MARA), 1 case C (SER01), 1 case D (ITOB).
    """
    llm_output = {
        "recommendations": [
            {
                "table_name": "SER02",
                "tier": "primary",
                "join_keys": ["MANDT", "OBKNR"],
                "rationale": "Maint.Contract serial doc header",
                "validation_source": "SER02",
                "confidence_grade": "high",
            },
            {
                "table_name": "EQUZ",
                "tier": "hypothesis",
                "join_keys": ["EQUNR"],
                "rationale": "Equipment time segment for lifecycle",
                "validation_source": "EQUZ",
                "confidence_grade": "low",
            },
            {
                "table_name": "MARA",
                "tier": "hypothesis",
                "join_keys": ["MATNR"],
                "rationale": "Material master",
                "validation_source": "MARA",
                "confidence_grade": "high",
            },
            {
                "table_name": "SER01",
                "tier": "hypothesis",
                "join_keys": ["OBKNR"],
                "rationale": "Delivery serial doc header",
                "validation_source": "SER01",
                "confidence_grade": "medium",
            },
            {
                "table_name": "ITOB",
                "tier": "hypothesis",
                "join_keys": ["OBJNR"],
                "rationale": "PM/CS object info",
                "validation_source": "ITOB",
                "confidence_grade": "high",
            },
        ],
        "catalog_gaps": ["IFLOTX or similar functional-location text table"],
    }

    result = validate_recommendations(
        llm_output,
        catalog=Q2_CATALOG,
        raw_sap=Q2_RAW_SAP,
    )

    summary = result["summary"]
    assert summary["total_recommendations"] == 5
    assert summary["case_a_count"] == 2  # SER02, EQUZ
    assert summary["case_b_count"] == 1  # MARA
    assert summary["case_c_count"] == 1  # SER01
    assert summary["case_d_count"] == 1  # ITOB

    by_table = {v.table_name: v for v in result["validated_recommendations"]}

    assert by_table["SER02"].classification == "A"
    assert by_table["SER02"].recommendation_grade == "verified"
    assert by_table["SER02"].catalog_brief_description.startswith("Document Header")

    assert by_table["EQUZ"].classification == "A"
    assert by_table["EQUZ"].recommendation_grade == "verified_low_priority"

    assert by_table["MARA"].classification == "B"
    assert by_table["MARA"].recommendation_grade == "scope_review_needed"

    assert by_table["SER01"].classification == "C"
    assert by_table["SER01"].recommendation_grade == "divergence_warning"
    assert by_table["SER01"].seed_columns is not None
    assert by_table["SER01"].catalog_key_fields is not None

    assert by_table["ITOB"].classification == "D"
    assert by_table["ITOB"].recommendation_grade == "unverified"
    assert by_table["ITOB"].catalog_brief_description is None

    # catalog_gaps passed through untouched
    assert result["catalog_gaps"] == ["IFLOTX or similar functional-location text table"]


def test_validate_preserves_llm_confidence_as_secondary() -> None:
    """System grade overrides confidence, but LLM confidence is preserved."""
    llm_output = {
        "recommendations": [
            {
                "table_name": "ITOB",
                "tier": "primary",
                "join_keys": [],
                "rationale": "...",
                "confidence_grade": "high",  # LLM is confident — system disagrees
            }
        ],
        "catalog_gaps": [],
    }
    result = validate_recommendations(llm_output, catalog=Q2_CATALOG, raw_sap=Q2_RAW_SAP)
    rec = result["validated_recommendations"][0]
    assert rec.llm_confidence_grade == "high"
    assert rec.recommendation_grade == "unverified"  # system override


def test_validate_empty_recommendations() -> None:
    """Empty input → empty output with zero counts."""
    result = validate_recommendations(
        {"recommendations": [], "catalog_gaps": []},
        catalog=Q2_CATALOG,
        raw_sap=Q2_RAW_SAP,
    )
    assert result["validated_recommendations"] == []
    assert result["summary"]["total_recommendations"] == 0
    for k in ("case_a_count", "case_b_count", "case_c_count", "case_d_count"):
        assert result["summary"][k] == 0


def test_validated_recommendation_to_dict_serializable() -> None:
    """Dataclass round-trips to dict for BAR JSON storage (Phase 2b)."""
    rec = ValidatedRecommendation(
        table_name="SER02",
        tier="primary",
        join_keys=["MANDT", "OBKNR"],
        rationale="test",
        catalog_brief_description="desc",
        classification="A",
        recommendation_grade="verified",
        llm_confidence_grade="high",
        seed_columns=None,
        catalog_key_fields=["MANDT", "OBKNR", "VBELN"],
    )
    d = rec.to_dict()
    assert d["table_name"] == "SER02"
    assert d["classification"] == "A"
    assert d["catalog_key_fields"] == ["MANDT", "OBKNR", "VBELN"]
