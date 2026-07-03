"""Drift probe for the term-analysis (BAR) runner.

Detects mid-session drift
in the inputs that feed the bundle assembler so the runner can abort
with a precise convergence_reason before the drifted bundle corrupts
the iteration trace.

Composition:
1. Run-level ingestion_log currency: MAX(finished_at_utc) + SUM(row_count_total)
   from main_seeds.ingestion_log. Deliberately NOT MAX(load_date) on
   raw_sap.* tables (that assumed a per-table granularity that
   ingestion_log does not provide; known_issue #25).
2. Seed CSV mtimes for the 17 probe-read seeds.
3. manifest.json mtime (ontology layer state).
4. hash_glossary_row(term_id) — querying WHERE id=? because
   business_glossary PK column is `id` (no separate `term_id` column).

Returns: 16-char sha256 prefix.

Two-class branching in the runner:
- probe mismatch → full bundle rebuild via assemble_context
- if rebuild schema_fingerprint differs AND glossary-row hash differs
  → hard_stop_glossary_drift (term_conditions checklist obsolete)
- if rebuild schema_fingerprint differs AND glossary-row hash same
  → hard_stop_bundle_fingerprint_drift
- if rebuild schema_fingerprint same (probe false positive) → update
  baseline, continue
"""
from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import duckdb

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPTS_DIR.parent
SEED_DIR = _PROJECT_ROOT / "dbt" / "seeds"
MANIFEST_PATH = _PROJECT_ROOT / "dbt" / "target" / "manifest.json"


# Full list of seeds the probe reads by mtime
PROBE_SEEDS = (
    "business_glossary.csv",
    "domain_analysis_results.csv",
    "business_term_analysis_results.csv",
    "domain_facts.csv",
    "analysis_findings.csv",
    "sap_data_dictionary.csv",
    "source_column_roles.csv",
    # movement_type_mapping.csv decommissioned 2026-05-05;
    # BWART decode lives in main_marts.dim_movement_type (vault-sourced
    # from T156 + T156T).
    "z_tables_catalog.csv",
    "abap_logic_catalog.csv",
    # vendor_catalog.csv decommissioned 2026-05-05 — vendor enrichment
    # now in main_vault.sat_vendor_business; drift caught by
    # static-layer fingerprint covering raw_sap.zmm_vendor_business.
    # cpe_catalog.csv decommissioned 2026-05-05 — same pattern;
    # enrichment now in main_vault.sat_material_business via
    # raw_sap.zmm_material_business.
    "procurement_rules.csv",
    "org_structure.csv",
    "s2t_mapping.csv",
    "archive_log.csv",
    "ingestion_log.csv",
)


def _iso_or_none(value) -> str:
    """RULE 36 / anti-pattern #50 — deterministic timestamp serialization.
    Defends against repr drift between datetime.datetime and pandas.Timestamp."""
    if value is None:
        return "none"
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def hash_glossary_row(conn: duckdb.DuckDBPyConnection, term_id: str) -> str:
    """Content-hash of the glossary row for this term.
    WHERE id=? (not term_id — business_glossary PK column is `id`).

    Separate from the probe's business_glossary.csv mtime entry because
    mtime is coarse: any glossary edit flips it. This row-specific hash
    lets the runner distinguish "someone edited THIS term" (→
    hard_stop_glossary_drift, checklist obsolete) from "someone edited
    some other term" (→ probe false positive, continue after bundle
    rebuild).
    """
    row = conn.execute(
        """
        SELECT term_name, definition, notes, status
        FROM main_seeds.business_glossary
        WHERE id = ?
        """,
        [term_id],
    ).fetchone()
    if row is None:
        return "missing"
    return hashlib.sha256(repr(row).encode("utf-8")).hexdigest()[:16]


def compute_drift_probe(
    conn: duckdb.DuckDBPyConnection,
    scope_tables: list[str],
    term_id: str,
) -> str:
    """Run-level probe composition.

    scope_tables is accepted for signature symmetry / future extension
    but not hashed (run-level ingestion_log already captures
    source-data currency at the granularity available).

    Cost budget: ~30-80 ms (1 SELECT on ingestion_log + 17 stat calls +
    1 stat on manifest + 1 glossary row fetch). ~60× faster than full
    bundle rebuild in the common case (no drift).
    """
    parts: list[str] = []

    # 1. Source-data currency via ingestion_log (run-level)
    latest = conn.execute(
        """
        SELECT MAX(finished_at_utc)  AS latest_finished,
               SUM(row_count_total)  AS total_rows_ingested
        FROM main_seeds.ingestion_log
        """
    ).fetchone()
    latest_finished_str = _iso_or_none(latest[0] if latest else None)
    total_rows = latest[1] if latest and latest[1] is not None else 0
    parts.append(f"ingestion_log:{latest_finished_str}:{total_rows}")

    # 2. Seed CSV mtimes (the 17 probe-read seeds)
    for seed in PROBE_SEEDS:
        path = SEED_DIR / seed
        if path.exists():
            parts.append(f"{seed}:{path.stat().st_mtime_ns}")
        else:
            parts.append(f"{seed}:missing")

    # 3. manifest.json mtime (ontology layer)
    if MANIFEST_PATH.exists():
        parts.append(f"manifest:{MANIFEST_PATH.stat().st_mtime_ns}")
    else:
        parts.append("manifest:missing")

    # 4. Glossary row hash for this term
    parts.append(f"glossary_row:{hash_glossary_row(conn, term_id)}")

    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
