"""C5 validation layer.

Filters LLM-emitted sourcing recommendations through:
- Layer A: catalog allowlist match (sap_table_catalog.csv)
- Layer B: empirical raw_sap schema cross-check
- Jaccard divergence detection (threshold 0.30 from Q2 calibration)
- Recommendation grade synthesis (overrides LLM confidence)

Implements Component 4 of tasks/c5_design.md. Pattern B: validation
runs in code, after the LLM emits recommendations grounded in the
catalog block. Catalog rows come from dbt/seeds/sap_table_catalog.csv
(Phase 1, commit f87908e). Threshold 0.30 calibrated empirically in
tasks/c5_q2_validation_accuracy.md (stable plateau [0.15, 0.40]).

Standalone module — no runner dependencies. Phase 2b will wire
validate_recommendations() into run_term_injection.py's BAR
finalization path.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
from pathlib import Path

import duckdb

JACCARD_THRESHOLD = 0.30  # tasks/c5_q2_validation_accuracy.md
_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = _ROOT / "dbt" / "seeds" / "sap_table_catalog.csv"
DUCKDB_PATH = _ROOT / "cpe_analytics.duckdb"


@dataclass
class ValidatedRecommendation:
    table_name: str
    tier: str  # primary | hypothesis | customer_namespace
    join_keys: list[str]
    rationale: str
    catalog_brief_description: str | None  # from catalog if matched
    classification: str  # A | B | C | D (Component 4 matrix)
    recommendation_grade: str  # verified | verified_low_priority | divergence_warning | unverified | scope_review_needed
    llm_confidence_grade: str  # preserved as secondary signal
    seed_columns: list[str] | None  # populated for case B/C
    catalog_key_fields: list[str] | None  # populated for case A/B/C

    def to_dict(self) -> dict:
        return asdict(self)


def jaccard(a: set, b: set) -> float:
    """Jaccard similarity. Returns 0.0 if both sets empty."""
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def load_catalog(path: Path | None = None) -> dict[str, dict]:
    """Load sap_table_catalog.csv into {table_name_lower: row_dict}."""
    src = path if path is not None else CATALOG_PATH
    catalog: dict[str, dict] = {}
    with open(src, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            catalog[row["table_name"].lower()] = row
    return catalog


def load_raw_sap_columns(conn) -> dict[str, list[str]]:
    """Query information_schema for raw_sap.* columns.
    Returns {table_name_lower: [column_names]}."""
    query = """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'raw_sap'
        ORDER BY table_name, ordinal_position
    """
    result: dict[str, list[str]] = {}
    for row in conn.execute(query).fetchall():
        table = row[0].lower()
        result.setdefault(table, []).append(row[1])
    return result


def classify(
    table_name: str,
    catalog: dict[str, dict],
    raw_sap: dict[str, list[str]],
) -> tuple[str, dict | None, list[str] | None, list[str] | None]:
    """Return (classification, catalog_row_or_None, seed_cols, catalog_key_fields).

    Per Component 4 matrix:
    - (A) Catalog YES + raw_sap NO → recommend ingestion
    - (B) Catalog YES + raw_sap YES, columns MATCH → already in scope
    - (C) Catalog YES + raw_sap YES, columns DIVERGE → nomenclature divergence
    - (D) Catalog NO → not in catalog (likely hallucinated)
    """
    table_lower = table_name.lower()
    catalog_row = catalog.get(table_lower)

    if catalog_row is None:
        return "D", None, None, None

    catalog_key_fields = [
        f.strip() for f in catalog_row.get("key_fields", "").split(",") if f.strip()
    ]

    seed_cols = raw_sap.get(table_lower)
    if seed_cols is None:
        return "A", catalog_row, None, catalog_key_fields

    seed_set = {c.upper() for c in seed_cols}
    catalog_set = {f.upper() for f in catalog_key_fields}
    similarity = jaccard(seed_set, catalog_set)

    if similarity >= JACCARD_THRESHOLD:
        return "B", catalog_row, seed_cols, catalog_key_fields
    return "C", catalog_row, seed_cols, catalog_key_fields


def synthesize_grade(classification: str, llm_confidence: str) -> str:
    """Per Component 4 grade synthesis rules.

    The system grade overrides the LLM's self-reported confidence so
    that empirical evidence (catalog/seed cross-check) outranks
    free-recall confidence.
    """
    if classification == "A" and llm_confidence in ("high", "medium"):
        return "verified"
    if classification == "A" and llm_confidence == "low":
        return "verified_low_priority"
    if classification == "C":
        return "divergence_warning"
    if classification == "D":
        return "unverified"
    if classification == "B":
        # Already in scope — recommending it is a scope/iteration gap, not a sourcing gap
        return "scope_review_needed"
    return "unknown"


def validate_recommendations(
    llm_output: dict,
    duckdb_conn=None,
    catalog: dict[str, dict] | None = None,
    raw_sap: dict[str, list[str]] | None = None,
) -> dict:
    """Main entry point. Takes parsed LLM JSON output, returns
    structured validation result with classified recommendations.

    Input shape: { "recommendations": [...], "catalog_gaps": [...] }
    Output shape: {
        "validated_recommendations": [ValidatedRecommendation, ...],
        "catalog_gaps": [...],  # passed through
        "summary": {
            "total_recommendations": int,
            "case_a_count": int,  # ingest these
            "case_b_count": int,  # already in scope
            "case_c_count": int,  # divergence warnings
            "case_d_count": int,  # rejected hallucinations
        }
    }

    Test override: pass `catalog` + `raw_sap` directly to bypass
    file/DuckDB I/O.
    """
    if catalog is None:
        catalog = load_catalog()
    if raw_sap is None:
        if duckdb_conn is None:
            duckdb_conn = duckdb.connect(str(DUCKDB_PATH), read_only=True)
        raw_sap = load_raw_sap_columns(duckdb_conn)

    validated: list[ValidatedRecommendation] = []
    counts = {"A": 0, "B": 0, "C": 0, "D": 0}

    for rec in llm_output.get("recommendations", []):
        table_name = rec["table_name"]
        tier = rec.get("tier", "hypothesis")
        join_keys = rec.get("join_keys", []) or []
        rationale = rec.get("rationale", "")
        llm_confidence = rec.get("confidence_grade", "low")

        classification, catalog_row, seed_cols, cat_keys = classify(
            table_name, catalog, raw_sap
        )
        counts[classification] += 1
        grade = synthesize_grade(classification, llm_confidence)

        catalog_desc = catalog_row.get("brief_description") if catalog_row else None

        validated.append(
            ValidatedRecommendation(
                table_name=table_name,
                tier=tier,
                join_keys=list(join_keys),
                rationale=rationale,
                catalog_brief_description=catalog_desc,
                classification=classification,
                recommendation_grade=grade,
                llm_confidence_grade=llm_confidence,
                seed_columns=seed_cols,
                catalog_key_fields=cat_keys,
            )
        )

    return {
        "validated_recommendations": validated,
        "catalog_gaps": llm_output.get("catalog_gaps", []),
        "summary": {
            "total_recommendations": len(validated),
            "case_a_count": counts["A"],
            "case_b_count": counts["B"],
            "case_c_count": counts["C"],
            "case_d_count": counts["D"],
        },
    }
