"""Stage F — shared analyzer registry for Domain Analysis Run All + Data
Catalog Source Diagnostic.

Both consumers (Data Analysis tab's per-term Run All and Data Catalog
page's global Source Diagnostic) dispatch the same analyzer suite
against a given table. Keeping the list in one module prevents drift.

Contract for each tuple:
  (script_name, analysis_type_label, arg_flavor)
    - script_name: filename under scripts/ (without directory)
    - analysis_type_label: the string stored in
      `domain_analysis_results.analysis_type` for DARs produced by the
      script. Must match the dbt schema.yml accepted_values enum.
    - arg_flavor: "singular" → `--table <name>` (4 LLM analyzers).
                  "plural"   → `--tables <name>` (deterministic
                  multi-table analyzers).

`grain_relationship` is pairwise and dispatched separately from the
Business Term Analysis tab — not in this registry.

`performance_baseline` is co-emitted by magnitude — no separate entry.

Import convention: `from _analyzer_registry import SOURCE_DIAGNOSTIC_ANALYZERS`
— app/ is sys.path root via Streamlit page discovery, NOT a package.
"""
from __future__ import annotations

SOURCE_DIAGNOSTIC_ANALYZERS: list[tuple[str, str, str]] = [
    ("run_completeness_analysis.py",     "completeness",      "singular"),
    ("run_dimensions_analysis.py",       "dimensions",        "singular"),
    ("run_magnitude_analysis.py",        "magnitude",         "singular"),
    ("run_code_tables_analysis.py",      "code_tables",       "singular"),
    ("run_date_analysis.py",             "date",              "plural"),
    ("run_segmentation_analysis.py",     "segmentation",      "plural"),
    ("run_schema_discovery_analysis.py", "schema_discovery",  "singular"),
]
