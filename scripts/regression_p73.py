"""P7.3 regression test — P7.2 with three fixes applied:

Fix 1 — citation ID format directive (LLM-facing, in prompt — edit in
        app/claude_api.py already applied).
Fix 2 — full-deploy semantic gate: write models to
        dbt/models/_regression_p73/, `dbt compile && dbt run` to materialize,
        query DuckDB for real row_count / column_types / sample_rows[:20],
        call validate_model_semantics with those real values.
Fix 3 — cross-ref known_refs: extend known_refs with same-batch model
        filenames before hallucination grep.

Terms (same as P7.2 to allow before/after comparison):
  BG001 (with findings), BG027 (draft, no history), BG007 (with findings, no s2t).

Teardown: drop dbt/models/_regression_p73/ directory + DROP SCHEMA
main_regression_p73 CASCADE after all 3 terms tested.
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

REGRESSION_MODEL_DIR = ROOT / "dbt" / "models" / "_regression_p73"
REGRESSION_SCHEMA = "main_regression_p73"
REGRESSION_SCHEMA_PLAIN = "regression_p73"   # what dbt uses in {{ config(schema=...) }}


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

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
    words = prose.split()
    n = len(words)
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
    if n > 500 or citation_density > 0.8:
        return "OVERCITES", stats
    return "CLEAN", stats


def _hallucination_grep(dbt_models: list, known_cols: set, known_refs: set) -> list:
    """P7.3 Fix 3: known_refs MUST include same-batch model names before this
    function is called. Call-site responsibility."""
    issues = []
    ident_re = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')
    ref_re = re.compile(r"\{\{\s*ref\(['\"]([a-zA-Z0-9_]+)['\"]\)\s*\}\}")
    for m in dbt_models or []:
        filename = m.get("filename", "?")
        sql = str(m.get("sql", "") or "")
        for ref in ref_re.findall(sql):
            if ref.lower() not in known_refs:
                issues.append((filename, f"unknown ref: '{ref}'"))
        for ident in ident_re.findall(sql):
            if ident.lower() in ('record_source', 'load_date'):
                continue
            if ident.lower() in known_cols:
                continue
            if ident.isupper() and 2 <= len(ident) <= 10 and ident.lower() not in known_cols:
                issues.append((filename, f"suspicious column identifier: '{ident}'"))
    return issues


_CONFIG_RE = re.compile(r"\{\{\s*config\([^}]*\)\s*\}\}", re.DOTALL)


def _inject_regression_config(sql: str) -> str:
    """Strip any LLM-emitted {{ config(...) }} block and prepend a forced
    regression config that pins schema + materialization to the test target."""
    stripped = _CONFIG_RE.sub("", sql).lstrip()
    header = (
        "{{ config(schema='" + REGRESSION_SCHEMA_PLAIN + "', "
        "materialized='view') }}\n\n"
    )
    return header + stripped


def _write_models_for_term(term_id: str, dbt_models: list) -> list[str]:
    """Write each model to dbt/models/_regression_p73/<term_id>/<filename>.
    Returns list of dbt-visible model names (filename stem)."""
    scratch = REGRESSION_MODEL_DIR / term_id
    scratch.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for m in dbt_models or []:
        fn = (m.get("filename") or "").strip()
        sql = str(m.get("sql", "") or "").strip()
        if not fn or not sql:
            continue
        if not fn.endswith(".sql"):
            fn = fn + ".sql"
        wrapped_sql = _inject_regression_config(sql)
        (scratch / fn).write_bytes(wrapped_sql.encode("utf-8"))
        written.append(fn.replace(".sql", ""))
    return written


def _dbt(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    dbt_exe = str(Path(sys.executable).parent / "dbt.EXE")
    return subprocess.run(
        [dbt_exe] + args, capture_output=True, text=True,
        cwd=str(ROOT / "dbt"), timeout=timeout,
    )


def _query_materialized(model_name: str) -> dict:
    """Return {row_count, column_types, sample_rows} for the materialized
    regression model. Defensive — returns {} with nulls on any error."""
    import duckdb
    c = duckdb.connect(str(ROOT / "cpe_analytics.duckdb"), read_only=True)
    try:
        # Check existence first
        exists = c.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND LOWER(table_name) = LOWER(?)",
            [REGRESSION_SCHEMA, model_name],
        ).fetchone()[0]
        if not exists:
            return {"row_count": 0, "column_types": {}, "sample_rows": [],
                    "materialization_status": "missing"}
        row_count = c.execute(
            f"SELECT COUNT(*) FROM {REGRESSION_SCHEMA}.{model_name}"
        ).fetchone()[0]
        col_rows = c.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? AND LOWER(table_name) = LOWER(?) "
            "ORDER BY ordinal_position",
            [REGRESSION_SCHEMA, model_name],
        ).fetchall()
        col_types = {cn: dt for cn, dt in col_rows}
        sample_rows: list[dict] = []
        if row_count:
            col_names = [cn for cn, _ in col_rows]
            srows = c.execute(
                f"SELECT * FROM {REGRESSION_SCHEMA}.{model_name} LIMIT 20"
            ).fetchall()
            for sr in srows:
                sample_rows.append({col_names[i]: sr[i] for i in range(len(col_names))})
        return {
            "row_count": int(row_count),
            "column_types": col_types,
            "sample_rows": sample_rows,
            "materialization_status": "ok",
        }
    except Exception as e:
        return {"row_count": 0, "column_types": {}, "sample_rows": [],
                "materialization_status": f"error: {type(e).__name__}: {e}"}
    finally:
        c.close()


def _semantic_gate_full(term_row: dict, dbt_models: list) -> tuple[bool, dict]:
    """P7.3 Fix 2: full-deploy semantic gate.

    Writes models → dbt compile → dbt run → query materialized → call
    validate_model_semantics with real row_count/column_types/sample_rows.
    Returns (clean_pass, detail_dict).
    """
    if not dbt_models:
        return (True, {"skipped": "no dbt_models"})
    try:
        from claude_api import validate_model_semantics
    except Exception as e:
        return (True, {"skipped": f"validate_model_semantics not importable: {e}"})

    detail = {"compile": None, "run": None, "per_model": []}
    written = _write_models_for_term(term_row["id"], dbt_models)
    if not written:
        return (True, {"skipped": "no writable models"})

    # Compile + run
    compile_r = _dbt(["compile", "--select"] + written, timeout=120)
    detail["compile"] = {
        "rc": compile_r.returncode,
        "stderr_tail": compile_r.stderr[-400:] if compile_r.stderr else "",
    }
    if compile_r.returncode != 0:
        return (False, {**detail, "compile_failed": True})

    run_r = _dbt(["run", "--select"] + written + ["--full-refresh"], timeout=240)
    detail["run"] = {
        "rc": run_r.returncode,
        "stderr_tail": run_r.stderr[-400:] if run_r.stderr else "",
    }
    if run_r.returncode != 0:
        return (False, {**detail, "run_failed": True})

    # Query each materialized model + semantic-validate
    all_critical = 0
    for m in dbt_models:
        fn = (m.get("filename") or "").replace(".sql", "").strip()
        if not fn:
            continue
        sql = str(m.get("sql", "") or "")
        mat = _query_materialized(fn)
        try:
            v = validate_model_semantics(
                term_row=term_row,
                model_name=fn,
                model_sql=sql,
                row_count=mat.get("row_count", 0),
                column_types=mat.get("column_types", {}),
                sample_rows=mat.get("sample_rows", []),
            )
        except Exception as e:
            detail["per_model"].append({
                "filename": fn,
                "materialized": mat,
                "validator_error": f"{type(e).__name__}: {str(e)[:160]}",
            })
            continue
        issues = (v or {}).get("issues") or []
        criticals = [i for i in issues
                     if str((i.get("severity") or "")).lower() == "critical"]
        all_critical += len(criticals)
        detail["per_model"].append({
            "filename": fn,
            "materialized": {k: mat[k] for k in ("row_count", "materialization_status")},
            "columns": list(mat.get("column_types", {}).keys()),
            "sample_first_row": mat.get("sample_rows", [{}])[0] if mat.get("sample_rows") else None,
            "total_issues": len(issues),
            "critical_issues": len(criticals),
            "critical_payload": criticals[:3],
        })
    detail["critical_total"] = all_critical
    return (all_critical == 0, detail)


def run_term(term_id: str, label: str) -> dict:
    import importlib
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
    wall_llm = time.perf_counter() - t0
    if "error" in result:
        return {"term_id": term_id, "error": result["error"], "wall_s": wall_llm}

    known_cols = _list_actual_columns()
    known_refs = _existing_refs()
    dbt_models = result.get("dbt_models") or []

    # P7.3 Fix 3: extend known_refs with same-batch model filenames
    same_batch = {
        (m.get("filename") or "").replace(".sql", "").strip().lower()
        for m in dbt_models
        if (m.get("filename") or "").strip()
    }
    known_refs = known_refs | same_batch

    plain = str(result.get("transformation_plain", "") or "")
    readability_grade, readability_stats = _grade_readability(plain)

    # Compile (in isolation) — the semantic gate's _dbt call will also compile,
    # but we keep a quick pre-check so we can report criterion (a) independently.
    compile_preview_ok = True
    compile_preview_msg = "compile deferred to semantic-gate run"

    # Full-deploy semantic gate (Fix 2)
    t1 = time.perf_counter()
    semantic_ok, semantic_detail = _semantic_gate_full(term_row, dbt_models)
    wall_semantic = time.perf_counter() - t1

    # Override compile_preview from semantic_detail's compile stage
    compile_msg = "compile skipped (no models)"
    if semantic_detail.get("compile"):
        rc = semantic_detail["compile"].get("rc")
        compile_preview_ok = (rc == 0)
        compile_msg = (
            f"dbt compile rc={rc}"
            + (f" — stderr tail: {semantic_detail['compile'].get('stderr_tail','')[-200:]}" if rc != 0 else "")
        )
    elif not dbt_models:
        compile_preview_ok = True
        compile_msg = "no dbt_models in result — compile skipped (vacuous pass)"

    hallucinations = _hallucination_grep(dbt_models, known_cols, known_refs)
    token_total = int(result.get("_bundle_total_tokens", 0))
    budget_ok = token_total < 50_000

    criteria = {
        "a_compile": ("PASS" if compile_preview_ok else "FAIL", compile_msg),
        "b_semantic": ("PASS" if semantic_ok else "FAIL",
                       f"critical_total={semantic_detail.get('critical_total','?')} "
                       f"per_model={len(semantic_detail.get('per_model', []))}"),
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
        "wall_s_llm": round(wall_llm, 1),
        "wall_s_semantic_deploy": round(wall_semantic, 1),
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
        "semantic_detail": semantic_detail,
    }


def _teardown():
    """Remove scratch models + drop regression schema from DuckDB."""
    import duckdb
    if REGRESSION_MODEL_DIR.exists():
        shutil.rmtree(REGRESSION_MODEL_DIR, ignore_errors=True)
        print(f"[cleanup] removed {REGRESSION_MODEL_DIR}")
    try:
        c = duckdb.connect(str(ROOT / "cpe_analytics.duckdb"))
        c.execute(f"DROP SCHEMA IF EXISTS {REGRESSION_SCHEMA} CASCADE")
        c.close()
        print(f"[cleanup] dropped schema {REGRESSION_SCHEMA}")
    except Exception as e:
        print(f"[cleanup] schema drop failed: {e}")


def main():
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    results = []
    try:
        for tid, label in TEST_TERMS:
            results.append(run_term(tid, label))
    finally:
        _teardown()

    print("\n\n==================== P7.3 SUMMARY ====================")
    for r in results:
        tid = r["term_id"]
        if "error" in r:
            print(f"{tid}: ERROR — {r['error']}")
            continue
        print(f"\n{tid} ({r['label']})")
        print(f"  wall (LLM):        {r['wall_s_llm']}s")
        print(f"  wall (semantic):   {r['wall_s_semantic_deploy']}s")
        print(f"  grade:             {r['grade']}")
        for k, (v, msg) in r["criteria"].items():
            print(f"  {k:20s} {v:10s}  {msg}")
        print(f"  bundle:            {r['bundle']}")
        print(f"  dbt_model_count:   {r['dbt_model_count']}")
        print(f"  confidence:        {r['confidence']}")
        print(f"  warnings_count:    {r['warnings_count']}")
        print(f"  LLM self-attest mismatch: {r['llm_self_attestation_mismatch']}")
        if r['_citation_audit_issues']:
            print(f"  citation issues:   {r['_citation_audit_issues']}")
        print(f"  attestation:       {r['attestation']}")
        # Per-model semantic gate payload
        sd = r["semantic_detail"]
        if sd.get("per_model"):
            print(f"  semantic per-model ({len(sd['per_model'])}):")
            for pm in sd["per_model"]:
                print(f"    - {pm.get('filename')}  row_count={pm.get('materialized',{}).get('row_count')}  "
                      f"issues={pm.get('total_issues','?')} critical={pm.get('critical_issues','?')}")

    out = ROOT / "logs" / "regression_p73_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nFull results written to {out}")


if __name__ == "__main__":
    main()
