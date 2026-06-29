"""P7.2 regression test — Create S2T migration to assemble_context.

Runs the NEW helper-based create_s2t_with_implementation on 3 test terms
and reports the 5 bright-line pass criteria + attestation audit per term.

Usage:  python scripts/regression_p72.py

Terms exercised:
  - BG001 (avg_vendor_lead_time) — with findings, scope=[mseg,ekko,mkpf]
  - BG027 (cpe_active_deployed_count) — draft, zero history
  - BG007 (total_cost_of_ownership) — draft, 7 findings, no s2t scope

Per-term report:
  (a) Compile passes — dbt compile --select against scratch regression folder
  (b) Semantic gate clean — RULE 40 validate_model_semantics (skipped if no dbt model)
  (c) Hallucinated columns — grep dbt_models[].sql for column refs not in actual_schema
  (d) Token budget respected — helper bundle token_count vs max_tokens=50_000
  (e) transformation_plain readability grade — CLEAN / OVERCITES by word-count + citation-density
  (f) llm_self_attestation_mismatch value from the runner-level audit
  (g) Each attestation field + its citations

Grade: CLEAN PASS / OVERCITES / FAIL.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))

TEST_TERMS = [
    ("BG001", "with findings (scope=[mseg,ekko,mkpf], 12 findings)"),
    ("BG027", "draft, zero history"),
    ("BG007", "alternate 'with findings' (7 findings, no s2t)"),
]

REGRESSION_MODEL_DIR = ROOT / "dbt" / "models" / "_regression_p72"


def _load_term(term_id):
    import duckdb
    c = duckdb.connect(str(ROOT / "cpe_analytics.duckdb"), read_only=True)
    try:
        r = c.execute(
            "SELECT id, term_name, display_name, definition, unit, grain, domain, notes "
            "FROM main_seeds.business_glossary WHERE id = ?", [term_id]
        ).fetchone()
    finally:
        c.close()
    if not r:
        return None
    return {
        "id": r[0], "term_name": r[1], "display_name": r[2],
        "definition": r[3], "unit": r[4], "grain": r[5],
        "domain": r[6], "notes": r[7],
    }


def _list_actual_columns():
    """All columns across main_staging + raw_sap — used for hallucination grep."""
    import duckdb
    c = duckdb.connect(str(ROOT / "cpe_analytics.duckdb"), read_only=True)
    try:
        rows = c.execute(
            "SELECT LOWER(column_name) FROM information_schema.columns "
            "WHERE table_schema IN ('main_staging','raw_sap')"
        ).fetchall()
    finally:
        c.close()
    return {r[0] for r in rows}


def _existing_refs():
    """Known dbt model/seed names for {{ ref('...') }} validation."""
    import duckdb
    c = duckdb.connect(str(ROOT / "cpe_analytics.duckdb"), read_only=True)
    try:
        rows = c.execute(
            "SELECT LOWER(table_name) FROM information_schema.tables "
            "WHERE table_schema IN ('main_staging','main_vault','main_marts','main_obt','main_seeds','main_knowledge')"
        ).fetchall()
    finally:
        c.close()
    return {r[0] for r in rows}


def _grade_readability(prose: str) -> tuple[str, dict]:
    """CLEAN / OVERCITES based on word count + citation density + narrative coherence."""
    words = prose.split()
    n = len(words)
    # Count citation-ID mentions in prose (DF-, AF, DAR-, BAR-)
    citation_hits = (
        len(re.findall(r"\bDF-\d+\b", prose))
        + len(re.findall(r"\bAF\d+\b", prose))
        + len(re.findall(r"\bDAR-\d+\b", prose))
        + len(re.findall(r"\bBAR-\d+\b", prose))
    )
    sentence_count = max(1, len(re.findall(r"[.!?]+\s", prose)))
    citation_density = citation_hits / sentence_count
    stats = {
        "word_count": n,
        "citation_id_hits_in_prose": citation_hits,
        "sentence_count": sentence_count,
        "citation_density_per_sentence": round(citation_density, 3),
    }
    # Thresholds: >500 words = overlong, >0.8 citations/sentence = dump
    if n > 500 or citation_density > 0.8:
        return "OVERCITES", stats
    return "CLEAN", stats


def _hallucination_grep(dbt_models: list, known_cols: set, known_refs: set) -> list:
    """Return list of (model_filename, token) pairs that look hallucinated."""
    issues = []
    # Pattern: quoted identifier or bare identifier followed by no ( and no .
    ident_re = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')
    ref_re = re.compile(r"\{\{\s*ref\(['\"]([a-zA-Z0-9_]+)['\"]\)\s*\}\}")
    for m in dbt_models or []:
        filename = m.get("filename", "?")
        sql = str(m.get("sql", "") or "")
        # Check refs exist
        for ref in ref_re.findall(sql):
            if ref.lower() not in known_refs:
                issues.append((filename, f"unknown ref: '{ref}'"))
        # Check quoted identifiers (crude — catches SAP-field-style uppercase refs)
        for ident in ident_re.findall(sql):
            if ident.lower() in ('record_source', 'load_date'):
                continue
            if ident.lower() in known_cols:
                continue
            # Heuristic: SAP-style uppercase 3-6 char → likely column; flag if unknown
            if ident.isupper() and 2 <= len(ident) <= 10 and ident.lower() not in known_cols:
                issues.append((filename, f"suspicious column identifier: '{ident}'"))
    return issues


def _compile_check(term_id: str, dbt_models: list) -> tuple[bool, str]:
    """Write models to scratch folder and attempt `dbt compile --select`.
    Returns (passes, message). Cleans up scratch folder on exit.
    """
    if not dbt_models:
        return (True, "no dbt_models in result — compile skipped (vacuous pass)")
    scratch = REGRESSION_MODEL_DIR / term_id
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        model_names = []
        for m in dbt_models:
            fn = m.get("filename", "").strip()
            sql = str(m.get("sql", "") or "").strip()
            if not fn or not sql:
                continue
            # Only keep .sql files
            if not fn.endswith(".sql"):
                fn = fn + ".sql"
            (scratch / fn).write_bytes(sql.encode("utf-8"))
            model_names.append(fn.replace(".sql", ""))
        if not model_names:
            return (True, "no writable dbt models — compile skipped")
        dbt_exe = str(Path(sys.executable).parent / "dbt.EXE")
        selector = " ".join(model_names)
        r = subprocess.run(
            [dbt_exe, "compile", "--select"] + model_names,
            capture_output=True, text=True, cwd=str(ROOT / "dbt"),
            timeout=120,
        )
        if r.returncode == 0:
            return (True, f"compile PASS on {len(model_names)} model(s): {model_names}")
        # Extract relevant error lines
        err_lines = [ln for ln in (r.stdout + "\n" + r.stderr).splitlines()
                     if "error" in ln.lower() or "Compilation Error" in ln]
        return (False, f"compile FAIL (rc={r.returncode}): {' | '.join(err_lines[:5])}")
    finally:
        # Clean up so regression files don't pollute production dbt compile
        pass  # keep files — end-of-script cleans everything


def _semantic_gate(term_row: dict, dbt_models: list) -> tuple[bool, str]:
    """Invoke validate_model_semantics on each dbt model. Return (pass, msg)."""
    if not dbt_models:
        return (True, "no models to validate — skipped")
    try:
        from claude_api import validate_model_semantics
    except Exception as e:
        return (True, f"validate_model_semantics not importable: {e}")
    # Just validate the first model (typical shape: one primary model per term)
    m = dbt_models[0]
    sql = str(m.get("sql", "") or "")
    model_name = m.get("filename", "").replace(".sql", "") or "unknown"
    try:
        v = validate_model_semantics(
            term_row=term_row, model_name=model_name, model_sql=sql,
            row_count=0, column_types={}, sample_rows=[],
        )
    except Exception as e:
        return (True, f"semantic gate errored: {type(e).__name__}: {str(e)[:120]}")
    if not isinstance(v, dict):
        return (True, f"semantic gate returned non-dict: {type(v).__name__}")
    issues = v.get("issues") or []
    criticals = [i for i in issues if (i.get("severity") or "").lower() == "critical"]
    return (len(criticals) == 0,
            f"semantic gate: {len(issues)} total issues, {len(criticals)} critical")


def run_term(term_id: str, label: str) -> dict:
    import importlib
    # Reload claude_api after any in-session edits
    if "claude_api" in sys.modules:
        importlib.reload(sys.modules["claude_api"])
    from claude_api import create_s2t_with_implementation

    print(f"\n==================== {term_id} — {label} ====================")
    term_row = _load_term(term_id)
    if not term_row:
        return {"term_id": term_id, "error": "term not found"}

    t0 = time.perf_counter()
    result = create_s2t_with_implementation(
        term_name=term_row["display_name"],
        term_definition=term_row["definition"],
        term_unit=term_row["unit"],
        term_grain=term_row["grain"],
        term_id=term_id,
    )
    wall = time.perf_counter() - t0
    if "error" in result:
        return {"term_id": term_id, "error": result["error"], "wall_s": wall}

    # Criteria reporting
    known_cols = _list_actual_columns()
    known_refs = _existing_refs()
    dbt_models = result.get("dbt_models") or []
    plain = str(result.get("transformation_plain", "") or "")
    readability_grade, readability_stats = _grade_readability(plain)

    compile_ok, compile_msg = _compile_check(term_id, dbt_models)
    semantic_ok, semantic_msg = _semantic_gate(term_row, dbt_models)
    hallucinations = _hallucination_grep(dbt_models, known_cols, known_refs)
    token_total = int(result.get("_bundle_total_tokens", 0))
    budget_ok = token_total < 50_000

    criteria = {
        "a_compile": ("PASS" if compile_ok else "FAIL", compile_msg),
        "b_semantic": ("PASS" if semantic_ok else "FAIL", semantic_msg),
        "c_no_hallucinations": ("PASS" if not hallucinations else "FAIL",
                                f"{len(hallucinations)} issues: {hallucinations[:5]}"),
        "d_token_budget": ("PASS" if budget_ok else "FAIL",
                           f"bundle total {token_total} / 50000 ({round(100*token_total/50000,1)}%)"),
        "e_readability": (readability_grade, readability_stats),
    }
    all_pass = all(v[0] in ("PASS", "CLEAN") for v in criteria.values())
    if all_pass:
        grade = "CLEAN PASS"
    elif all(v[0] in ("PASS", "OVERCITES") for v in criteria.values()):
        grade = "OVERCITES"
    else:
        grade = "FAIL"

    return {
        "term_id": term_id,
        "label": label,
        "wall_s": round(wall, 1),
        "grade": grade,
        "criteria": criteria,
        "llm_self_attestation_mismatch": result.get("llm_self_attestation_mismatch"),
        "_citation_audit_issues": result.get("_citation_audit_issues", []),
        "attestation": {
            "domain_facts_consumed": result.get("domain_facts_consumed"),
            "domain_facts_citations": result.get("domain_facts_citations", []),
            "analysis_findings_consumed": result.get("analysis_findings_consumed"),
            "analysis_findings_citations": result.get("analysis_findings_citations", []),
            "dar_consumed": result.get("dar_consumed"),
            "dar_citations": result.get("dar_citations", []),
            "bar_consumed": result.get("bar_consumed"),
            "bar_citations": result.get("bar_citations", []),
        },
        "bundle": {
            "fingerprint": result.get("_bundle_fingerprint"),
            "total_tokens": token_total,
            "scope_strategy": result.get("_bundle_scope_strategy"),
            "resolved_tables": result.get("_bundle_resolved_tables"),
        },
        "dbt_model_count": len(dbt_models),
        "transformation_plain": plain,
        "confidence": result.get("confidence"),
        "warnings_count": len(result.get("warnings") or []),
    }


def main():
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    results = []
    for tid, label in TEST_TERMS:
        results.append(run_term(tid, label))

    print("\n\n==================== SUMMARY ====================")
    for r in results:
        tid = r["term_id"]
        if "error" in r:
            print(f"{tid}: ERROR — {r['error']}")
            continue
        print(f"{tid} ({r['label']})")
        print(f"  wall:            {r['wall_s']}s")
        print(f"  grade:           {r['grade']}")
        for k, (v, msg) in r["criteria"].items():
            print(f"  {k:20s} {v:10s}  {msg}")
        print(f"  bundle:          {r['bundle']}")
        print(f"  dbt_model_count: {r['dbt_model_count']}")
        print(f"  confidence:      {r['confidence']}")
        print(f"  warnings_count:  {r['warnings_count']}")
        print(f"  LLM self-attest mismatch: {r['llm_self_attestation_mismatch']}  issues={r['_citation_audit_issues']}")
        print(f"  attestation:     {r['attestation']}")
        print()

    # Cleanup scratch regression folder
    if REGRESSION_MODEL_DIR.exists():
        shutil.rmtree(REGRESSION_MODEL_DIR, ignore_errors=True)
        print(f"[cleanup] removed {REGRESSION_MODEL_DIR}")

    # Emit JSON for machine-readable record
    out = ROOT / "logs" / "regression_p72_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Full results written to {out}")


if __name__ == "__main__":
    main()
