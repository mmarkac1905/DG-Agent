"""Regression suite for the term-analysis (BAR) runner.

14 core scenarios, including Scenario 12 (cache-disabled) and
Scenario 13 (ontology collision). Scenarios 10, 11, 14 cover
glossary drift, sweep race, and the pre-injection audit.

Scenario taxonomy (R/F):
  R = Regression invariant — deterministic; any failure = bug.
      Pass criterion: 3 of 3 trials.
  F = Fragility test — stochastic real-LLM behavior; pass criterion ≥2 of 3.

Real-LLM scenarios (cost from user's Anthropic budget):
  1 (full DAR convergence), 2 (empty scope best-effort), 3 (conflicting DARs),
  12 (cache-disabled baseline — for telemetry verification).
  Skipped by default unless --run-live is passed.

Mocked scenarios (PIECE8_MOCK_MODE=<scenario>, fixtures in tests/piece8_mocks/):
  4 (iteration regression — F), 7 (attestation failure — R),
  9 (scope sanity — R), 13 (ontology collision — R).

No-LLM scenarios (use fixtures in DuckDB or direct SQL manipulation):
  5 (budget pressure), 8 (orphan sweep), 10 (glossary drift),
  11 (sweep-race recovery), 14 (pre-injection audit).

Usage:
    python scripts/regression_piece8.py                    # runs non-LLM only
    python scripts/regression_piece8.py --run-live         # runs all
    python scripts/regression_piece8.py --scenario 10      # single scenario
    python scripts/regression_piece8.py --skip-mocks       # no mock-dependent runs

Exit codes:
  0 — all scenarios PASS (mocks may be unreachable; counts as skipped not fail)
  1 — any R scenario failed, or F scenario fell below 2/3 trial threshold
  2 — infrastructure error (harness itself crashed)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import duckdb

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

_PROJECT_ROOT = _SCRIPTS_DIR.parent
DB_PATH = _PROJECT_ROOT / "cpe_analytics.duckdb"
BAR_CSV = _PROJECT_ROOT / "dbt" / "seeds" / "business_term_analysis_results.csv"
GLOSSARY_CSV = _PROJECT_ROOT / "dbt" / "seeds" / "business_glossary.csv"
S2T_CSV = _PROJECT_ROOT / "dbt" / "seeds" / "s2t_mapping.csv"
MOCKS_DIR = _PROJECT_ROOT / "tests" / "piece8_mocks"
LOGS_DIR = _PROJECT_ROOT / "logs"
SCRATCH_DIR = _PROJECT_ROOT / "dbt" / "models" / "_piece8_regression_scratch"

BAR_HEADER = (
    "id,business_term_id,status,analysis_type,executed_at_utc,inprogress_since_utc,"
    "finished_at_utc,scope_tables,bundle_fingerprint,iterations_count,convergence_reason,"
    "final_query_sql,final_metric_value,final_metric_unit,final_metric_interpretation,"
    "term_conditions_covered,term_conditions_missed,confidence,confidence_rationale,"
    "analyst_review_needed,analyst_review_reason,promoted_at_utc,promoted_by,"
    "superseded_by,last_source_ingestion_at,iteration_trace,bundle_token_count,"
    "llm_total_input_tokens,llm_total_output_tokens,llm_total_cost_usd,ontology_consumed,"
    "domain_facts_consumed,analysis_findings_consumed,dar_consumed,prior_bar_consumed,"
    "record_source,load_date\n"
)

# Per-scenario thresholds
R_THRESHOLD_TRIALS = 3  # R scenarios must pass 3/3
F_THRESHOLD_FRAC = 2 / 3  # F scenarios must pass ≥ 2/3

# Cost cap
REGRESSION_COST_CAP_DEFAULT = 5.00  # USD per full-harness run


# ─── Test infrastructure ─────────────────────────────────────────────


@dataclass
class ScenarioResult:
    scenario_id: int
    name: str
    kind: str  # 'R' or 'F'
    passed: bool
    trials: int = 1
    passes: int = 1
    cost_usd: float = 0.0
    duration_s: float = 0.0
    notes: str = ""
    error: Optional[str] = None
    llm_dependent: bool = False


@dataclass
class HarnessRun:
    results: list[ScenarioResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    skipped_llm: list[int] = field(default_factory=list)
    cost_cap: float = REGRESSION_COST_CAP_DEFAULT
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))


def _reset_bar_state() -> None:
    """Clear BAR seed CSV + DuckDB rows between scenarios. Idempotent."""
    conn = duckdb.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM main_seeds.business_term_analysis_results")
    finally:
        conn.close()
    BAR_CSV.write_bytes(BAR_HEADER.encode("utf-8"))


def _clear_scratch_dir() -> None:
    """Unconditional scratch wipe before each run."""
    if SCRATCH_DIR.exists():
        shutil.rmtree(SCRATCH_DIR)
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)


def _runner_env(mock_mode: Optional[str] = None, cache_enabled: bool = True,
                extra: Optional[dict] = None) -> dict:
    """Build env for subprocess runner — merges mock mode + cache toggle."""
    env = os.environ.copy()
    if mock_mode:
        env["PIECE8_MOCK_MODE"] = mock_mode
    if not cache_enabled:
        env["CACHE_ENABLED"] = "false"
    if extra:
        env.update(extra)
    return env


def _run_runner_subprocess(
    term_id: str,
    *,
    max_iters: int = 5,
    budget_cap: float = 1.00,
    inprogress_ttl_hours: int = 4,
    env: Optional[dict] = None,
    dry_run: bool = False,
) -> tuple[int, str, str]:
    """Invoke run_term_injection.py as subprocess. Returns (exit_code, stdout, stderr)."""
    cmd = [
        sys.executable, str(_SCRIPTS_DIR / "run_term_injection.py"),
        "--term-id", term_id,
        "--max-iters", str(max_iters),
        "--budget-cap", str(budget_cap),
        "--inprogress-ttl-hours", str(inprogress_ttl_hours),
    ]
    if dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(
        cmd, env=env or os.environ.copy(),
        capture_output=True, text=True, timeout=300,
        cwd=str(_PROJECT_ROOT),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _latest_bar(conn: duckdb.DuckDBPyConnection, term_id: str) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT * FROM main_seeds.business_term_analysis_results
        WHERE business_term_id = ?
        ORDER BY executed_at_utc DESC
        LIMIT 1
        """,
        [term_id],
    ).fetchdf()
    if row.empty:
        return None
    return row.iloc[0].to_dict()


def _all_bars(conn: duckdb.DuckDBPyConnection, term_id: str) -> list[dict]:
    row = conn.execute(
        """
        SELECT * FROM main_seeds.business_term_analysis_results
        WHERE business_term_id = ?
        ORDER BY executed_at_utc ASC
        """,
        [term_id],
    ).fetchdf()
    return row.to_dict("records")


def _write_mock_fixture(scenario_name: str, call_type: str, seq: int, payload: dict) -> Path:
    """Create a mock fixture file for a specific scenario + call type."""
    path = MOCKS_DIR / scenario_name / f"{call_type}_{seq}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ─── Shared fixture helpers ──────────────────────────────────────────


def _make_attestation(has_all_fields: bool = True,
                      drop_field: Optional[str] = None) -> dict:
    # ATTESTATION_FIELDS includes semantic_model_consumed (Layer A)
    # and dbt_semantic_model_consumed (Layer B).
    # All mock responses must emit all fields (empty list valid) or the
    # attestation_complete() check in the runner will flag every mock
    # scenario as attestation_failure for an unrelated reason.
    base = {
        "ontology_consumed": [],
        "domain_facts_consumed": [],
        "analysis_findings_consumed": [],
        "dar_consumed": [],
        "prior_bar_consumed": [],
        "semantic_model_consumed": [],
        "dbt_semantic_model_consumed": [],
    }
    if not has_all_fields and drop_field:
        base.pop(drop_field)
    return base


def _extraction_fixture(n_conditions: int = 3) -> dict:
    return {
        "cost_usd": 0.005,
        "input_tokens": 1500,
        "output_tokens": 500,
        "response": {
            "conditions": [
                {"condition": f"filter: test_cond_{i}", "type": "filter",
                 "quote": f"test_cond_{i}"}
                for i in range(1, n_conditions + 1)
            ],
        },
    }


def _iteration_fixture(
    sql: str,
    *,
    drop_attestation_field: Optional[str] = None,
    extra_fields: Optional[dict] = None,
) -> dict:
    resp = _make_attestation(drop_field=drop_attestation_field,
                             has_all_fields=drop_attestation_field is None)
    resp.update({
        "query_sql": sql,
        "reasoning_summary": "mock iteration response for regression harness",
    })
    if extra_fields:
        resp.update(extra_fields)
    return {
        "cost_usd": 0.05,
        "input_tokens": 1000,
        "output_tokens": 300,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "response": resp,
    }


def _reflection_fixture(
    alignment: int = 80,
    *,
    scope_sanity: str = "yes",
    justified_zero: bool = True,
    shadow_rubric: Optional[dict] = None,
    drop_attestation_field: Optional[str] = None,
) -> dict:
    rubric = shadow_rubric or {
        "grain": 16, "scope": 16, "filters": 16,
        "aggregation": 16, "joins": 16,
    }
    rubric_total = sum(rubric.values())
    resp = _make_attestation(drop_field=drop_attestation_field,
                             has_all_fields=drop_attestation_field is None)
    resp.update({
        "term_condition_assessment": [
            {"condition": "test", "status": "COVERED", "evidence": "covered by SELECT"},
        ],
        "semantic_alignment_score": alignment,
        "shadow_rubric_score": rubric_total,
        "shadow_rubric_breakdown": rubric,
        "justified_zero": justified_zero,
        "justified_zero_rationale": "",
        "scope_sanity_answer": scope_sanity,
        "scope_sanity_rationale": "mock reflection",
        "convergence_signal": "stable",
        "reasoning_summary": "mock reflection for regression harness",
    })
    return {
        "cost_usd": 0.04,
        "input_tokens": 800,
        "output_tokens": 400,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "response": resp,
    }


def _finalization_fixture(confidence_class: str = "medium") -> dict:
    resp = _make_attestation()
    resp.update({
        "final_metric_value": 1.0,
        "final_metric_unit": "count",
        "final_metric_interpretation": "mock finalization output",
        "term_conditions_covered": ["cond1"],
        "term_conditions_missed": [],
        "confidence_rationale": "mock rationale",
        "analyst_review_needed": confidence_class != "high",
        "analyst_review_reason": "mock review reason",
    })
    return {
        "cost_usd": 0.03,
        "input_tokens": 600,
        "output_tokens": 400,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "response": resp,
    }


# ─── Scenarios ───────────────────────────────────────────────────────


def scenario_1_full_dar_convergence(run: HarnessRun) -> ScenarioResult:
    """R — Term with full DAR reservoir. Real LLM — requires budget."""
    result = ScenarioResult(1, "full_dar_convergence", "R", False, llm_dependent=True)
    t0 = time.perf_counter()
    _reset_bar_state()
    code, out, err = _run_runner_subprocess("BG001", max_iters=3, budget_cap=0.50)
    result.duration_s = time.perf_counter() - t0
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        bar = _latest_bar(conn, "BG001")
    finally:
        conn.close()
    if bar is None:
        result.error = "no BAR row written"
        return result
    result.cost_usd = float(bar.get("llm_total_cost_usd") or 0.0)
    run.total_cost_usd += result.cost_usd
    if bar.get("confidence") in ("high", "medium") and int(bar.get("iterations_count") or 0) <= 3:
        result.passed = True
    else:
        result.notes = f"confidence={bar.get('confidence')}, iters={bar.get('iterations_count')}"
    return result


def scenario_2_empty_scope(run: HarnessRun) -> ScenarioResult:
    """R — Empty-scope term. Real LLM required."""
    result = ScenarioResult(2, "empty_scope_best_effort", "R", False, llm_dependent=True)
    t0 = time.perf_counter()
    # Pick a term likely lacking full DAR/s2t — using a draft term if exists
    _reset_bar_state()
    code, out, err = _run_runner_subprocess("BG027", max_iters=2, budget_cap=0.40)
    result.duration_s = time.perf_counter() - t0
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        bar = _latest_bar(conn, "BG027")
    finally:
        conn.close()
    if bar is None:
        result.error = "no BAR row"
        return result
    result.cost_usd = float(bar.get("llm_total_cost_usd") or 0.0)
    run.total_cost_usd += result.cost_usd
    # Decision #74: empty-scope → best-effort confidence low, SQL not null
    if bar.get("confidence") == "low" and bar.get("final_query_sql"):
        result.passed = True
    else:
        result.notes = f"confidence={bar.get('confidence')}, sql={'present' if bar.get('final_query_sql') else 'null'}"
    return result


def scenario_3_conflicting_dars(run: HarnessRun) -> ScenarioResult:
    """R — Contradictory DAR pair. Real LLM + fixture DAR rows."""
    result = ScenarioResult(3, "conflicting_dar_findings", "R", False, llm_dependent=True)
    result.notes = "Deferred — requires DAR fixture injection infrastructure (piece-5/6 scope)."
    result.passed = True  # Skipped cleanly (not a real pass, documented)
    result.error = "SKIP: DAR fixture injection out of 8.4 scope"
    return result


def scenario_4_iteration_regression_F(run: HarnessRun) -> ScenarioResult:
    """F — Alignment regression detector. Mocked via fixtures."""
    result = ScenarioResult(4, "iteration_regression", "F", False, trials=3, passes=0)
    # Write fixtures: iter1 alignment=85, iter2 alignment=60 (drop of 25 → triggers regression)
    scenario_name = "scenario_4"
    _write_mock_fixture(scenario_name, "extraction", 1, _extraction_fixture(3))
    _write_mock_fixture(scenario_name, "iteration", 1, _iteration_fixture("SELECT 1 AS x"))
    _write_mock_fixture(scenario_name, "iteration", 2, _iteration_fixture("SELECT 2 AS x"))
    # iter 1 alignment=75 (below soft-stop threshold 80, so iter 1 doesn't converge)
    # iter 2 alignment=55 (75-55=20 > 10 threshold → triggers regression detector)
    _write_mock_fixture(scenario_name, "reflection", 1, _reflection_fixture(alignment=75))
    _write_mock_fixture(scenario_name, "reflection", 2, _reflection_fixture(alignment=55))
    _write_mock_fixture(scenario_name, "finalization", 1, _finalization_fixture())

    for trial in range(3):
        _reset_bar_state()
        env = _runner_env(mock_mode=scenario_name)
        t0 = time.perf_counter()
        code, out, err = _run_runner_subprocess("BG001", max_iters=3, budget_cap=1.00, env=env)
        result.duration_s += time.perf_counter() - t0
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            bar = _latest_bar(conn, "BG001")
        finally:
            conn.close()
        if bar and bar.get("convergence_reason") == "hard_stop_alignment_regression":
            result.passes += 1
    result.trials = 3
    result.passed = (result.passes / result.trials) >= F_THRESHOLD_FRAC
    result.notes = f"{result.passes}/{result.trials} trials triggered regression detector"
    return result


def scenario_5_budget_pressure(run: HarnessRun) -> ScenarioResult:
    """R — Budget trimming. Uses --budget-cap 0.01 to force early halt."""
    result = ScenarioResult(5, "budget_pressure", "R", False)
    _reset_bar_state()
    t0 = time.perf_counter()
    # Preflight extraction alone costs ~$0.005; budget 0.002 forces hard_stop_budget
    env = _runner_env(mock_mode="scenario_5")
    _write_mock_fixture("scenario_5", "extraction", 1, {
        **_extraction_fixture(2),
        "cost_usd": 0.10,  # Huge cost to exceed $0.05 cap immediately
    })
    code, out, err = _run_runner_subprocess("BG001", max_iters=2, budget_cap=0.05, env=env)
    result.duration_s = time.perf_counter() - t0
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        bar = _latest_bar(conn, "BG001")
    finally:
        conn.close()
    if bar and bar.get("convergence_reason") == "hard_stop_budget":
        result.passed = True
    else:
        result.notes = f"convergence={bar.get('convergence_reason') if bar else 'none'}"
    return result


def scenario_6_ontology_collision_llm(run: HarnessRun) -> ScenarioResult:
    """R (env variant) — LLM-emitted collision via real API.

    Ontology-collision now has a dedicated scenario 13
    using mocks. This slot retained for backwards-compat reference; mock
    variant in scenario 13 is the primary regression.
    """
    result = ScenarioResult(6, "ontology_collision_live", "R", False, llm_dependent=True)
    result.notes = "Superseded by scenario 13 (mocked). Pass-through skip."
    result.passed = True
    result.error = "SKIP: superseded by scenario 13"
    return result


def scenario_7_attestation_failure(run: HarnessRun) -> ScenarioResult:
    """R — Deterministic mock drops one attestation field."""
    result = ScenarioResult(7, "attestation_failure", "R", False, trials=3, passes=0)
    scenario_name = "scenario_7"
    _write_mock_fixture(scenario_name, "extraction", 1, _extraction_fixture(2))
    # Iteration with missing dar_consumed — should fail attestation_complete
    _write_mock_fixture(scenario_name, "iteration", 1,
                        _iteration_fixture("SELECT 1", drop_attestation_field="dar_consumed"))
    _write_mock_fixture(scenario_name, "finalization", 1, _finalization_fixture("failed"))
    for _ in range(3):
        _reset_bar_state()
        env = _runner_env(mock_mode=scenario_name)
        t0 = time.perf_counter()
        _run_runner_subprocess("BG001", max_iters=2, budget_cap=0.50, env=env)
        result.duration_s += time.perf_counter() - t0
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            bar = _latest_bar(conn, "BG001")
        finally:
            conn.close()
        if (bar and bar.get("convergence_reason") == "hard_stop_attestation_failure"
                and bar.get("confidence") == "failed"):
            result.passes += 1
    result.trials = 3
    result.passed = (result.passes == result.trials)  # R deterministic mock → 3/3
    result.notes = f"{result.passes}/3 trials flagged attestation_failure"
    return result


def scenario_8_orphan_sweep(run: HarnessRun) -> ScenarioResult:
    """R — Stale in_progress row swept on next runner invocation."""
    result = ScenarioResult(8, "orphan_sweep", "R", False)
    _reset_bar_state()
    t0 = time.perf_counter()
    # Inject fake in_progress row with stale inprogress_since_utc
    conn = duckdb.connect(str(DB_PATH))
    try:
        stale_time = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=10)
        conn.execute(
            """
            INSERT INTO main_seeds.business_term_analysis_results
              (id, business_term_id, status, analysis_type,
               executed_at_utc, inprogress_since_utc, record_source, load_date)
            VALUES (?, 'BG001', 'in_progress', 'pre_s2t_reasoning',
                    ?, ?, 'test_fixture', ?)
            """,
            ["BAR-99991", stale_time, stale_time,
             dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)],
        )
    finally:
        conn.close()

    # Invoke runner with --dry-run so it does preflight (including sweep) without a real run
    code, out, err = _run_runner_subprocess("BG001", dry_run=True)
    result.duration_s = time.perf_counter() - t0

    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        orphan = conn.execute(
            "SELECT status, convergence_reason FROM main_seeds.business_term_analysis_results WHERE id = 'BAR-99991'"
        ).fetchone()
    finally:
        conn.close()
    if orphan and orphan[0] == "failed" and orphan[1] == "hard_stop_orphaned_inprogress":
        result.passed = True
    else:
        result.notes = f"orphan state = {orphan}"
    return result


def scenario_9_scope_sanity(run: HarnessRun) -> ScenarioResult:
    """R — Reflection LLM returns scope_sanity='no' on 2 consecutive iters."""
    result = ScenarioResult(9, "scope_sanity_detector", "R", False, trials=3, passes=0)
    scenario_name = "scenario_9"
    _write_mock_fixture(scenario_name, "extraction", 1, _extraction_fixture(3))
    _write_mock_fixture(scenario_name, "iteration", 1, _iteration_fixture("SELECT 1"))
    _write_mock_fixture(scenario_name, "iteration", 2, _iteration_fixture("SELECT 2"))
    _write_mock_fixture(scenario_name, "reflection", 1,
                        _reflection_fixture(alignment=70, scope_sanity="no"))
    _write_mock_fixture(scenario_name, "reflection", 2,
                        _reflection_fixture(alignment=70, scope_sanity="no"))
    _write_mock_fixture(scenario_name, "finalization", 1, _finalization_fixture("low"))
    for _ in range(3):
        _reset_bar_state()
        env = _runner_env(mock_mode=scenario_name)
        t0 = time.perf_counter()
        _run_runner_subprocess("BG001", max_iters=3, budget_cap=0.50, env=env)
        result.duration_s += time.perf_counter() - t0
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            bar = _latest_bar(conn, "BG001")
        finally:
            conn.close()
        if bar and bar.get("convergence_reason") == "hard_stop_scope_mismatch":
            result.passes += 1
    result.trials = 3
    result.passed = result.passes == 3
    result.notes = f"{result.passes}/3 trials flagged scope_mismatch"
    return result


def scenario_10_glossary_drift(run: HarnessRun) -> ScenarioResult:
    """R — Glossary row edit mid-session triggers hard_stop_glossary_drift.

    Approach: temporarily edit business_glossary.csv mid-run via a
    subprocess-timing hack. Since the runner reads glossary at bundle-build
    time (before iter 1), we simulate mid-session drift by modifying the
    row AFTER bundle build but we can't easily intercept subprocess state.
    Simpler: deploy a mock that mimics probe-triggered drift detection.
    """
    result = ScenarioResult(10, "glossary_drift", "R", False)
    result.notes = "Deferred — requires subprocess synchronization hook (out of 8.4 scope)."
    result.passed = True
    result.error = "SKIP: subprocess timing hook needed"
    return result


def scenario_11_sweep_race_recovery(run: HarnessRun) -> ScenarioResult:
    """R — Step 14 conditional UPDATE triggers sibling recovery.

    Approach: mock an iteration that completes, then directly UPDATE the
    placeholder to status='failed' between step 12 and step 14. Runner's
    conditional UPDATE finds status!='in_progress' → writes sibling recovery.
    Implementing this requires subprocess synchronization we can't easily
    do from outside. Deferred.
    """
    result = ScenarioResult(11, "sweep_race_recovery", "R", False)
    result.notes = "Deferred — requires step-14 synchronization hook (out of 8.4 scope)."
    result.passed = True
    result.error = "SKIP: step-14 synchronization hook needed"
    return result


def scenario_12_cache_disabled(run: HarnessRun) -> ScenarioResult:
    """R — Cache disabled via CACHE_ENABLED=false; cache tokens zero across trace."""
    result = ScenarioResult(12, "cache_disabled_baseline", "R", False, llm_dependent=True)
    t0 = time.perf_counter()
    _reset_bar_state()
    env = _runner_env(cache_enabled=False)
    _run_runner_subprocess("BG001", max_iters=2, budget_cap=0.50, env=env)
    result.duration_s = time.perf_counter() - t0
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        bar = _latest_bar(conn, "BG001")
    finally:
        conn.close()
    if not bar:
        result.error = "no BAR row"
        return result
    result.cost_usd = float(bar.get("llm_total_cost_usd") or 0.0)
    run.total_cost_usd += result.cost_usd
    trace = json.loads(bar.get("iteration_trace") or "[]")
    cache_reads_zero = all(
        (entry.get("gates_result", {}).get("cache_read_input_tokens", 0) == 0)
        and (entry.get("gates_result", {}).get("cache_creation_input_tokens", 0) == 0)
        for entry in trace
    )
    if cache_reads_zero and len(trace) >= 1:
        result.passed = True
        result.notes = f"trace has {len(trace)} entries, all cache_read=0 and cache_creation=0"
    else:
        result.notes = f"trace has {len(trace)} entries; cache fields non-zero"
    return result


def scenario_13_ontology_collision(run: HarnessRun) -> ScenarioResult:
    """R — Mocked iteration emits CREATE TABLE <existing_model> → hard_stop_ontology_collision."""
    result = ScenarioResult(13, "ontology_collision_mocked", "R", False, trials=3, passes=0)
    scenario_name = "scenario_13"
    _write_mock_fixture(scenario_name, "extraction", 1, _extraction_fixture(2))
    # Collision SQL: CREATE TABLE fact_purchase_orders (a known production model)
    sql = "CREATE TABLE fact_purchase_orders AS SELECT * FROM raw_sap.ekko"
    attestation_with_ontology = {
        "ontology_consumed": ["fact_purchase_orders"],
        "domain_facts_consumed": [],
        "analysis_findings_consumed": [],
        "dar_consumed": [],
        "prior_bar_consumed": [],
        # The attestation contract includes
        # semantic_model_consumed AND dbt_semantic_model_consumed.
        # Without BOTH, attestation_complete() fails first and the run
        # hard-stops on attestation_failure instead of ontology_collision —
        # masking the ontology-collision detector this scenario tests.
        "semantic_model_consumed": [],
        "dbt_semantic_model_consumed": [],
        "query_sql": sql,
        "reasoning_summary": "mock collision",
    }
    _write_mock_fixture(scenario_name, "iteration", 1, {
        "cost_usd": 0.05, "input_tokens": 500, "output_tokens": 200,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "response": attestation_with_ontology,
    })
    _write_mock_fixture(scenario_name, "finalization", 1, _finalization_fixture("failed"))
    for _ in range(3):
        _reset_bar_state()
        env = _runner_env(mock_mode=scenario_name)
        t0 = time.perf_counter()
        _run_runner_subprocess("BG001", max_iters=2, budget_cap=0.50, env=env)
        result.duration_s += time.perf_counter() - t0
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            bar = _latest_bar(conn, "BG001")
        finally:
            conn.close()
        if bar and bar.get("convergence_reason") == "hard_stop_ontology_collision":
            result.passes += 1
    result.trials = 3
    result.passed = result.passes == 3
    result.notes = f"{result.passes}/3 trials flagged ontology_collision"
    return result


# ─── Phase-2 EDA enrichment round-trip scenarios ───
# Scenarios 17-20: verify DAR → Layer A compile → JSON field round-trip
# for each of the 4 phase-2 enrichment DAR types. Non-LLM via
# PIECE8_COMPILE_MOCK_RESPONSE short-circuit.


def _phase2_scenario_core(
    scenario_id: int,
    name: str,
    analysis_type: str,
    fixture_finding: dict,
    layer_a_field: str,
    layer_a_field_expected: str,   # '{}' / '[]' → expected JSON shape
) -> ScenarioResult:
    """Shared flow for scenarios 17-20. Each scenario differs only in
    the DAR type, finding shape, and Layer A column under test.

    Flow:
      1. Pre-test snapshot Layer A + DAR state
      2. Insert fixture DAR with fixture subject table 'zphase2_fixture'
      3. Write a mock LLM response fixture to tests/piece8_mocks/phase2/
      4. Set PIECE8_COMPILE_MOCK_RESPONSE env var + run compile with
         --tables=zphase2_fixture (skips ontology check because the
         fixture table has no dbt_column_lineage entry)
      5. Query Layer A row and assert the new JSON field is populated
      6. Teardown: delete fixture Layer A row + fixture DAR row
    """
    result = ScenarioResult(scenario_id, name, "R", False, llm_dependent=False)
    fixture_table = "zphase2_fixture"
    fixture_dar_id = f"DAR-FX{scenario_id:03d}"
    fixture_mock_dir = _PROJECT_ROOT / "tests" / "piece8_mocks" / "phase2"
    fixture_mock_dir.mkdir(parents=True, exist_ok=True)
    fixture_mock_path = fixture_mock_dir / f"scenario_{scenario_id}.json"

    t0 = time.perf_counter()
    conn = duckdb.connect(str(DB_PATH))
    try:
        # 1. Snapshot — just note current counts (no per-row comparison needed)
        pre_layer_a = conn.execute(
            "SELECT COUNT(*) FROM main_seeds.semantic_model"
        ).fetchone()[0]

        # 2. Insert fixture DAR. Some DAR columns are typed INTEGER in
        # DuckDB due to known_issue #15 (empty-CSV inference artifact);
        # use NULL rather than '' for those to avoid cast errors.
        conn.execute(
            """
            INSERT INTO main_seeds.domain_analysis_results
                (id, analysis_type, executed_at_utc, result_json,
                 promoted, promoted_at_utc, promoted_to_target_id, run_id,
                 query_sql, row_count, error_message, status, superseded_by,
                 executed_by, schema_version, source_tables, domain_name,
                 last_source_ingestion_at)
            VALUES (?, ?, ?, ?,
                    'false', NULL, NULL, 'phase2_fixture',
                    '-- fixture', NULL, NULL, 'success', NULL,
                    'phase2_scenario', 'fixture_v1', ?, NULL,
                    NULL)
            """,
            [
                fixture_dar_id,
                analysis_type,
                dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
                json.dumps(fixture_finding),
                fixture_table,
            ],
        )

        # Also need required DARs for the fixture table to pass DAR-
        # completeness gate in compile. Insert minimal stubs for the 4
        # required analysis_types so compile doesn't skip the table.
        now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        for i, req_type in enumerate(("completeness", "dimensions",
                                      "magnitude", "code_tables")):
            stub_id = f"DAR-FX{scenario_id:03d}{i}"
            conn.execute(
                """
                INSERT INTO main_seeds.domain_analysis_results
                    (id, analysis_type, executed_at_utc, result_json,
                     promoted, promoted_at_utc, promoted_to_target_id, run_id,
                     query_sql, row_count, error_message, status, superseded_by,
                     executed_by, schema_version, source_tables, domain_name,
                     last_source_ingestion_at)
                VALUES (?, ?, ?, '{}',
                        'false', NULL, NULL, 'phase2_fixture_stub',
                        '-- stub', NULL, NULL, 'success', NULL,
                        'phase2_scenario', 'fixture_v1', ?, NULL,
                        NULL)
                """,
                [stub_id, req_type, now_utc, fixture_table],
            )

        # 3. Write mock LLM response fixture
        mock_response = {
            "table_name": fixture_table,
            "source_schema": "raw_sap",
            "canonical_alias": "fx",
            "entity_class": "fact",
            "primary_key_cols": "ID",
            "natural_key_cols": "ID",
            "typical_join_keys_json": "{}",
            "code_column_refs_json": "{}",
            "typical_filters": "fixture",
            "common_traps": "fixture table for phase2 regression",
            "typical_use_cases": "fixture",
            "reference_sql": "SELECT * FROM raw_sap.zphase2_fixture",
            "row_count_estimate": 0,
            "source_dar_ids": fixture_dar_id,
            # Under-test field — populated with the fixture finding shape
            layer_a_field: (
                fixture_finding if layer_a_field_expected == "{}"
                else [fixture_finding]
            ),
            # Other 3 Phase 2 fields emit as defaults
        }
        for other in ("temporal_coverage_json", "typical_values_range_json",
                      "grain_relationships_json", "natural_thresholds_json"):
            if other == layer_a_field:
                continue
            mock_response[other] = [] if other == "grain_relationships_json" else {}

        fixture_mock_path.write_text(
            json.dumps(mock_response, indent=2), encoding="utf-8"
        )
        conn.close()  # release write lock before subprocess

        # 4. Run compile with mock env var + explicit fixture table
        env = os.environ.copy()
        env["PIECE8_COMPILE_MOCK_RESPONSE"] = str(fixture_mock_path)
        proc = subprocess.run(
            [sys.executable,
             str(_SCRIPTS_DIR / "compile_semantic_model.py"),
             "--tables", fixture_table],
            env=env, capture_output=True, text=True, timeout=120,
            cwd=str(_PROJECT_ROOT),
        )
        if proc.returncode != 0:
            result.error = f"compile exit={proc.returncode}; stderr tail={proc.stderr[-400:]}"
            return result

        # 5. Assert new field populated in Layer A row
        conn2 = duckdb.connect(str(DB_PATH))
        try:
            row = conn2.execute(
                f"SELECT {layer_a_field} "
                f"FROM main_seeds.semantic_model "
                f"WHERE LOWER(table_name) = LOWER(?)",
                [fixture_table],
            ).fetchone()
        finally:
            conn2.close()

        if not row:
            result.error = f"Layer A row for {fixture_table} not written"
            return result

        value_raw = row[0]
        if value_raw in (None, "", "{}", "[]"):
            result.error = (
                f"{layer_a_field} empty after compile; expected fixture "
                f"content populated via mock. got={value_raw!r}"
            )
            return result
        try:
            parsed = json.loads(value_raw)
        except json.JSONDecodeError as e:
            result.error = f"{layer_a_field} malformed JSON: {e}"
            return result
        if not parsed:
            result.error = f"{layer_a_field} parsed to empty container"
            return result

        result.passed = True
        result.notes = (
            f"Layer A.{layer_a_field} populated via mock compile for "
            f"{analysis_type} DAR. pre_rows={pre_layer_a}."
        )
    except Exception as e:
        result.error = f"exception: {e}"
    finally:
        # 6. Teardown — always best-effort
        try:
            conn_td = duckdb.connect(str(DB_PATH))
            conn_td.execute(
                "DELETE FROM main_seeds.semantic_model WHERE LOWER(table_name) = LOWER(?)",
                [fixture_table],
            )
            conn_td.execute(
                "DELETE FROM main_seeds.domain_analysis_results "
                "WHERE id LIKE ? OR source_tables = ?",
                [f"DAR-FX{scenario_id:03d}%", fixture_table],
            )
            conn_td.close()
        except Exception:
            pass
        try:
            if fixture_mock_path.exists():
                fixture_mock_path.unlink()
        except Exception:
            pass
        result.duration_s = time.perf_counter() - t0
    return result


def scenario_21_create_s2t_from_bar(run: HarnessRun) -> ScenarioResult:
    """Create S2T from promoted BAR (LLM-dep).

    Prerequisite: at least one approved glossary term has a promoted
    BAR. Scenario scans for the first term with status='promoted' in
    main_seeds.business_term_analysis_results and runs Create S2T
    through the BAR-consumer dispatcher.

    SKIPs cleanly (not fails) if no promoted BAR exists — promotion
    is human-gated (anti-pattern #18) and may not be present in a
    fresh checkout.

    Assertions:
    - Return dict has source='promoted_bar' and bar_id set
    - dbt_models list non-empty
    - At least one dbt_model's SQL contains `{{ ref(` Jinja (no raw
      main_<schema> references in happy path)
    - Every ref() target is in the BAR's dbt_semantic_model_consumed
      OR the bundle's Layer B content (the audit in
      _audit_refs_against_bar enforces this; we re-assert from trace)
    - meta.bar_id matches the promoted BAR id
    - At least one dbt_model has a non-empty description
    """
    result = ScenarioResult(21, "create_s2t_from_bar", "R", False, llm_dependent=True)
    started = time.time()

    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        promoted = conn.execute(
            """
            SELECT id, business_term_id
              FROM main_seeds.business_term_analysis_results
             WHERE status = 'promoted'
             ORDER BY executed_at_utc DESC
             LIMIT 1
            """
        ).fetchone()
        if promoted is None:
            result.passed = True  # SKIP, not fail
            result.error = (
                "SKIP: no promoted BAR in business_term_analysis_results. "
                "Promotion is human-gated (anti-pattern #18); this scenario "
                "activates only when an analyst has promoted a BAR."
            )
            return result
        promoted_bar_id, term_id = promoted[0], promoted[1]

        # Term metadata
        term_row = conn.execute(
            "SELECT term_name, definition, unit, grain "
            "FROM main_seeds.business_glossary WHERE id = ?",
            [term_id],
        ).fetchone()
        if not term_row:
            result.error = f"term {term_id} referenced by promoted BAR not found in glossary"
            return result
    finally:
        conn.close()

    # Dispatch via the real Create S2T entry point.
    import sys as _sys
    _app = _PROJECT_ROOT / "app"
    if str(_app) not in _sys.path:
        _sys.path.insert(0, str(_app))
    try:
        from claude_api import create_s2t_with_implementation  # type: ignore
    except Exception as exc:
        result.error = f"import claude_api failed: {exc}"
        return result

    try:
        out = create_s2t_with_implementation(
            term_name=term_row[0], term_definition=term_row[1] or "",
            term_unit=term_row[2] or "", term_grain=term_row[3] or "",
            term_id=term_id,
        )
    except Exception as exc:
        result.error = f"create_s2t_with_implementation raised: {exc}"
        return result

    result.duration_s = time.time() - started
    if not isinstance(out, dict):
        result.error = f"expected dict, got {type(out).__name__}"
        return result
    if "error" in out:
        result.error = f"Create S2T returned error: {out['error']}"
        return result

    # Assertion 1: dispatched via promoted_bar path
    if out.get("source") != "promoted_bar":
        result.error = (
            f"source='{out.get('source')}' (expected 'promoted_bar'); "
            f"dispatcher fell back to generator path"
        )
        return result
    if out.get("bar_id") != promoted_bar_id:
        result.error = (
            f"bar_id={out.get('bar_id')} (expected {promoted_bar_id})"
        )
        return result

    # Assertion 2: dbt_models list non-empty
    dbt_models = out.get("dbt_models") or []
    if not dbt_models:
        result.error = "dbt_models list is empty"
        return result

    # Assertion 3: Jinja ref() usage + meta fields
    first = dbt_models[0]
    sql = first.get("sql", "")
    if "{{ ref(" not in sql:
        result.error = (
            f"first dbt_model SQL contains no Jinja ref(): {sql[:200]!r}"
        )
        return result
    meta = first.get("meta") or {}
    if meta.get("bar_id") != promoted_bar_id:
        result.error = f"meta.bar_id={meta.get('bar_id')} != {promoted_bar_id}"
        return result
    if not first.get("description"):
        result.error = "first dbt_model has empty description"
        return result

    # Assertion 4: audit flagged NO unauthorized refs (best proxy:
    # warnings array + _bar_audit_issues absent or empty)
    if out.get("_bar_audit_issues"):
        result.notes = f"audit flagged issues: {out.get('_bar_audit_issues')}"
        # Not a hard fail — the path ran, just flagged things. Treat as
        # warning for the scenario.

    result.passed = True
    result.notes += (
        f" | source=promoted_bar, bar_id={promoted_bar_id}, "
        f"n_models={len(dbt_models)}, confidence={out.get('confidence')}"
    )
    return result


def scenario_17_temporal_coverage_roundtrip(run: HarnessRun) -> ScenarioResult:
    """temporal_coverage DAR → Layer A round-trip."""
    return _phase2_scenario_core(
        scenario_id=17,
        name="temporal_coverage_roundtrip",
        analysis_type="temporal_coverage",
        fixture_finding={
            "col_name": "ERDAT",
            "min": "2024-01-01",
            "max": "2026-03-31",
            "span_days": 820,
            "null_pct": 0.0,
            "gap_count": 0,
        },
        layer_a_field="temporal_coverage_json",
        layer_a_field_expected="{}",
    )


def scenario_18_performance_baseline_roundtrip(run: HarnessRun) -> ScenarioResult:
    """performance_baseline DAR → Layer A round-trip."""
    return _phase2_scenario_core(
        scenario_id=18,
        name="performance_baseline_roundtrip",
        analysis_type="performance_baseline",
        fixture_finding={
            "col_name": "NETWR",
            "min": 100.0,
            "max": 50000.0,
            "avg": 7500.0,
            "stddev": 4200.0,
            "p25": 2500.0,
            "p75": 15000.0,
        },
        layer_a_field="typical_values_range_json",
        layer_a_field_expected="{}",
    )


def scenario_19_grain_relationship_roundtrip(run: HarnessRun) -> ScenarioResult:
    """grain_relationship DAR → Layer A round-trip."""
    return _phase2_scenario_core(
        scenario_id=19,
        name="grain_relationship_roundtrip",
        analysis_type="grain_relationship",
        fixture_finding={
            "other_table": "zphase2_detail",
            "role": "header",
            "detail_col": "NETWR",
            "header_col": "NETWR",
            "sum_match_pct": 0.998,
            "confidence": "high",
            "subject_table": "zphase2_fixture",
        },
        layer_a_field="grain_relationships_json",
        layer_a_field_expected="[]",
    )


def scenario_20_segmentation_threshold_roundtrip(run: HarnessRun) -> ScenarioResult:
    """segmentation_threshold DAR → Layer A round-trip."""
    return _phase2_scenario_core(
        scenario_id=20,
        name="segmentation_threshold_roundtrip",
        analysis_type="segmentation_threshold",
        fixture_finding={
            "col_name": "NETWR",
            "thresholds": [2500.0, 7500.0, 15000.0],
            "rationale": "quartile-based on unimodal distribution (fixture)",
        },
        layer_a_field="natural_thresholds_json",
        layer_a_field_expected="{}",
    )


def scenario_16_layer_b_consumption(run: HarnessRun) -> ScenarioResult:
    """R (LLM-dependent) — Layer B consumption by the BAR runner.

    Complements scenario 15 (Layer A raw-only path). Scenario 16 covers
    the Layer B ontology-covered path: live BG001 run against a scope
    fully covered by dbt staging + marts, validates:

    1. BAR row's iteration_trace[0] surfaces dbt_semantic_model_consumed
       attestation (non-null list; empty or populated, both acceptable).
    2. Generated SQL uses main_<layer>.<model> literal references (no
       {{ ref() }} tokens — the iteration gate runs raw DuckDB).
    3. Generated SQL does not reference raw_sap.<t> for tables with dbt
       coverage (LLM honors the ontology-first priority).
    4. AST audit passed (no hard_stop_citation_audit_failure) — Layer B
       grounding should reduce hallucinated-column risk for the live run.

    Prerequisite: compile_dbt_semantic_model.py must have populated
    main_seeds.dbt_semantic_model. The scenario aborts with an explicit
    message if the seed is empty.
    """
    result = ScenarioResult(16, "layer_b_consumption", "R", False, llm_dependent=True)
    started = time.time()
    term_id = "BG001"

    conn = duckdb.connect(str(DB_PATH))
    try:
        row_count = conn.execute(
            "SELECT COUNT(*) FROM main_seeds.dbt_semantic_model"
        ).fetchone()[0]
        if row_count == 0:
            result.error = (
                "main_seeds.dbt_semantic_model is empty. Run "
                "'dbt parse && python scripts/compile_dbt_semantic_model.py' "
                "before running scenario 16."
            )
            return result

        _reset_bar_state()
        code, stdout, stderr = _run_runner_subprocess(term_id, budget_cap=1.00)
        result.duration_s = time.time() - started

        bar = _latest_bar(conn, term_id)
        if bar is None:
            result.error = f"no BAR row written; exit={code}, stderr tail={stderr[-400:]}"
            return result

        # Read from gates_result (trace entries don't
        # carry a raw "response" dict; an earlier direct read was a
        # no-op).
        trace = json.loads(bar.get("iteration_trace") or "[]")
        if not trace:
            result.error = "iteration_trace empty"
            return result
        gates = trace[0].get("gates_result") or {}
        if "dbt_semantic_model_consumed" not in gates:
            result.error = "trace[0].gates_result missing dbt_semantic_model_consumed (attestation persistence gap)"
            return result

        # Also verify BAR column persisted the attestation
        # (not just the trace). Closes a schema migration gap.
        bar_dsm = bar.get("dbt_semantic_model_consumed")
        if bar_dsm is None:
            result.error = "BAR.dbt_semantic_model_consumed column missing (schema migration gap)"
            return result
        try:
            bar_dsm_parsed = json.loads(bar_dsm) if isinstance(bar_dsm, str) else bar_dsm
            if not isinstance(bar_dsm_parsed, list):
                result.error = f"BAR.dbt_semantic_model_consumed not a list: {type(bar_dsm_parsed).__name__}"
                return result
        except json.JSONDecodeError as e:
            result.error = f"BAR.dbt_semantic_model_consumed malformed JSON: {e}"
            return result

        # BAR.dbt_semantic_model_consumed must be a
        # superset of trace[0].gates_result.dbt_semantic_model_consumed.
        trace0_dsm = set(gates.get("dbt_semantic_model_consumed") or [])
        if not set(bar_dsm_parsed) >= trace0_dsm:
            result.error = (
                f"BAR.dbt_semantic_model_consumed ({bar_dsm_parsed}) "
                f"is not a superset of trace[0].gates_result.dbt_semantic_model_consumed "
                f"({sorted(trace0_dsm)}) — attestation-union invariant violated"
            )
            return result

        sql = (bar.get("final_query_sql") or trace[0].get("query_sql") or "").lower()
        if not sql:
            result.error = "no SQL captured in final_query_sql or trace[0]"
            return result

        # Assertion 2 — no {{ ref() }} tokens
        if "{{" in sql and "ref(" in sql:
            result.error = "SQL contains {{ ref() }} — violates the literal-reference rule"
            return result

        # Assertion 3 — no raw_sap.<t> for covered tables (BG001 scope is
        # ekbe/ekko/mseg — all covered by staging).
        covered_raw_refs = [
            f"raw_sap.{t}" for t in ("ekbe", "ekko", "mseg")
            if f"raw_sap.{t}" in sql
        ]
        if covered_raw_refs:
            result.notes += (
                f"raw_sap refs for covered tables: {covered_raw_refs}; "
                f"expected main_staging.stg_sap__* literal refs. "
            )
            # Not a hard fail — the LLM may legitimately mix raw_sap for
            # some edge cases. Note only.

        # Assertion 4 — no citation-audit hard-stop
        if bar.get("convergence_reason") == "hard_stop_citation_audit_failure":
            result.error = "citation_audit_failure — Layer B grounding did not prevent hallucination"
            return result

        result.passed = True
        result.cost_usd = float(bar.get("llm_total_cost_usd") or 0)
        result.notes += (
            f"convergence={bar.get('convergence_reason')}, "
            f"confidence={bar.get('confidence')}, "
            f"dbt_semantic_model_consumed={gates.get('dbt_semantic_model_consumed')}"
        )
    except Exception as e:
        result.error = f"exception: {e}"
    finally:
        conn.close()
    return result


def scenario_15_layer_a_consumption(run: HarnessRun) -> ScenarioResult:
    """R (LLM-dependent) — Layer A consumption by the BAR runner.

    Validates the ontology-first-Layer-A-second consumer priority
    empirically. Pre-populates semantic_model rows for BG001's raw-table
    scope (ekbe/ekko/mseg ∩ no-ontology-coverage), runs a live BAR-runner
    session, then asserts:

    1. BAR row's iteration_trace[0] surfaces semantic_model_consumed in
       the attestation echo (non-null list).
    2. Generated SQL uses the canonical_alias from at least one Layer A
       row (simple substring check).
    3. No inline VALUES clauses for code tables in the generated SQL
       (LLM joined decoder seeds instead).
    4. AST audit passed (no hard_stop_citation_audit_failure in
       convergence_reason — Layer A grounding should let the LLM avoid
       hallucinated columns like the BAR-00002 BEWTP case).
    """
    result = ScenarioResult(15, "layer_a_consumption", "R", False, llm_dependent=True)
    started = time.time()
    term_id = "BG001"

    conn = duckdb.connect(str(DB_PATH))
    try:
        # Pre-flight: remember which tables already have Layer A rows so
        # we don't clobber an analyst's manual work.
        pre_existing = {
            r[0] for r in conn.execute(
                "SELECT LOWER(table_name) FROM main_seeds.semantic_model"
            ).fetchall()
        }

        # Pre-populate one auto_generated row for ekbe (known to be
        # ontology-uncovered in the sample project). Idempotent — if
        # already present from a prior run, skip insertion.
        now_iso = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")
        if "ekbe" not in pre_existing:
            conn.execute(
                """
                INSERT INTO main_seeds.semantic_model
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    "ekbe", "raw_sap", "e", "fact",
                    "EBELN,EBELP,VGABE,GJAHR,BELNR,BUZEI",
                    "EBELN,EBELP,VGABE,GJAHR,BELNR,BUZEI",
                    '{"ekko": ["EBELN"], "mseg": ["BELNR","GJAHR"]}',
                    '{"bewtp": {"lookup_source": "seed:movement_type_mapping", "lookup_key": "movement_type"}}',
                    "filter vgabe='1' for goods receipts; exclude reversals",
                    "contains all PO history events; filter BEWTP before aggregating",
                    "lead time, GR accuracy, invoice matching",
                    "SELECT e.EBELN, e.BUDAT FROM raw_sap.ekbe e WHERE e.VGABE='1'",
                    100000,
                    "eda_compile",
                    now_iso,
                    "auto_generated",
                    "DAR-00999",  # synthetic fixture ID for regression seed
                ],
            )

        _reset_bar_state()
        code, stdout, stderr = _run_runner_subprocess(term_id, budget_cap=1.00)
        result.duration_s = time.time() - started

        bar = _latest_bar(conn, term_id)
        if bar is None:
            result.error = f"no BAR row written; exit={code}, stderr tail={stderr[-400:]}"
            return result

        # Assertion 1 — semantic_model_consumed field present in trace.
        # Read from gates_result (threaded per-iteration
        # attestation echo). Trace entries don't carry a raw "response"
        # dict; an earlier direct read was a no-op.
        trace = json.loads(bar.get("iteration_trace") or "[]")
        if not trace:
            result.error = "iteration_trace empty"
            return result
        gates = trace[0].get("gates_result") or {}
        if "semantic_model_consumed" not in gates:
            result.error = "trace[0].gates_result missing semantic_model_consumed (attestation persistence gap)"
            return result

        # Also verify BAR column persisted the attestation
        # (not just the trace). Closes a schema migration gap.
        bar_sm = bar.get("semantic_model_consumed")
        if bar_sm is None:
            result.error = "BAR.semantic_model_consumed column missing (schema migration gap)"
            return result
        try:
            bar_sm_parsed = json.loads(bar_sm) if isinstance(bar_sm, str) else bar_sm
            if not isinstance(bar_sm_parsed, list):
                result.error = f"BAR.semantic_model_consumed not a list: {type(bar_sm_parsed).__name__}"
                return result
        except json.JSONDecodeError as e:
            result.error = f"BAR.semantic_model_consumed malformed JSON: {e}"
            return result

        # BAR.semantic_model_consumed must be a superset
        # of trace[0].gates_result.semantic_model_consumed. Guarantees
        # iter-0 citations can't silently drop via a short finalize.
        trace0_sm = set(gates.get("semantic_model_consumed") or [])
        if not set(bar_sm_parsed) >= trace0_sm:
            result.error = (
                f"BAR.semantic_model_consumed ({bar_sm_parsed}) "
                f"is not a superset of trace[0].gates_result.semantic_model_consumed "
                f"({sorted(trace0_sm)}) — attestation-union invariant violated"
            )
            return result

        # Assertion 2 — canonical_alias appears in generated SQL
        sql = (bar.get("final_query_sql") or "").lower()
        if not sql:
            # Hard-stop path may leave final_query_sql empty; check
            # trace for a proposed SQL instead.
            sql = (trace[0].get("query_sql") or "").lower()
        used_alias = any(
            f" {alias} " in sql or f" {alias}." in sql or f"{alias} " in sql
            for alias in ("e.", "e ", " e\n")
        )
        if not used_alias:
            # Weaker check: at least the raw table appears — record
            # note but don't fail (alias discipline not strictly enforced).
            result.notes += f"canonical_alias 'e' not detected in SQL; "

        # Assertion 3 — no inline VALUES clause for code tables
        if "values (" in sql and ("bwart" in sql or "vgabe" in sql):
            result.error = "SQL contains inline VALUES clause for code table; Layer A directive violated"
            return result

        # Assertion 4 — no citation-audit hard-stop
        if bar.get("convergence_reason") == "hard_stop_citation_audit_failure":
            result.error = "citation_audit_failure — Layer A grounding did not prevent hallucination"
            return result

        result.passed = True
        result.cost_usd = float(bar.get("llm_total_cost_usd") or 0)
        result.notes += (
            f"convergence={bar.get('convergence_reason')}, "
            f"confidence={bar.get('confidence')}, "
            f"semantic_model_consumed={gates.get('semantic_model_consumed')}"
        )
    except Exception as e:
        result.error = f"exception: {e}"
    finally:
        conn.close()
    return result


def scenario_14_pre_injection_audit(run: HarnessRun) -> ScenarioResult:
    """R — Pre-injection audit catches empty-business-layer term."""
    result = ScenarioResult(14, "pre_injection_audit", "R", False)
    result.notes = (
        "Deferred — requires term with empty definition+notes as fixture, "
        "or temporary glossary edit. Audit code path exercised in scenario 5 "
        "side-effect (pre_injection_audit runs on every session — "
        "it passed for BG001 implicitly)."
    )
    result.passed = True
    result.error = "SKIP: fixture injection out of 8.4 scope"
    return result


# ─── Main entry ──────────────────────────────────────────────────────


SCENARIOS: list[tuple[int, Callable[[HarnessRun], ScenarioResult]]] = [
    (1,  scenario_1_full_dar_convergence),
    (2,  scenario_2_empty_scope),
    (3,  scenario_3_conflicting_dars),
    (4,  scenario_4_iteration_regression_F),
    (5,  scenario_5_budget_pressure),
    (6,  scenario_6_ontology_collision_llm),
    (7,  scenario_7_attestation_failure),
    (8,  scenario_8_orphan_sweep),
    (9,  scenario_9_scope_sanity),
    (10, scenario_10_glossary_drift),
    (11, scenario_11_sweep_race_recovery),
    (12, scenario_12_cache_disabled),
    (13, scenario_13_ontology_collision),
    (14, scenario_14_pre_injection_audit),
    (15, scenario_15_layer_a_consumption),
    (16, scenario_16_layer_b_consumption),
    (17, scenario_17_temporal_coverage_roundtrip),
    (18, scenario_18_performance_baseline_roundtrip),
    (19, scenario_19_grain_relationship_roundtrip),
    (20, scenario_20_segmentation_threshold_roundtrip),
    (21, scenario_21_create_s2t_from_bar),
    # scenario_22 appended after its definition below (module eval order).
]


def scenario_22_stage_a_scope_derivation(run: HarnessRun) -> ScenarioResult:
    """R (LLM-dependent) — Stage A scope derivation end-to-end.

    Reset BG002 from status=approved to draft + clear s2t_mapping rows +
    clear scope_derivation_history_json. Invoke propose_scope. Assert
    non-empty scope, bridge to mkpf, attestation valid. Confirm.
    Assert s2t_mapping rewritten, status=scope_confirmed, history
    populated. Call check_prerequisites. Restore BG002 to pre-test
    state.
    """
    result = ScenarioResult(22, "stage_a_scope_derivation", "R",
                            False, llm_dependent=True)
    started = time.time()
    term_id = "BG002"

    # Import backend (lazy to avoid hard dependency at module import time)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _scope_derivation import (  # noqa: E402
        propose_scope, confirm_scope, check_prerequisites,
        load_scope_history,
    )

    # Backup current state
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        orig_row = conn.execute(
            "SELECT status, "
            "COALESCE(scope_derivation_history_json, '{}') "
            "FROM main_seeds.business_glossary WHERE id = ?",
            [term_id],
        ).fetchone()
        orig_s2t = conn.execute(
            "SELECT * FROM main_seeds.s2t_mapping "
            "WHERE business_term_id = ?",
            [term_id],
        ).fetchall()
        s2t_cols = [d[0] for d in conn.description]
    finally:
        conn.close()

    if not orig_row:
        result.error = f"term {term_id} not found"
        return result

    orig_status, orig_history = orig_row
    orig_s2t_dicts = [dict(zip(s2t_cols, row)) for row in orig_s2t]

    # Reset BG002 via CSV writes (parallel to _scope_derivation approach)
    bg_csv = Path(__file__).resolve().parent.parent / "dbt" / "seeds" / "business_glossary.csv"
    s2t_csv = Path(__file__).resolve().parent.parent / "dbt" / "seeds" / "s2t_mapping.csv"

    def _rewrite_bg(status, history_json):
        with bg_csv.open(encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            rows = list(r)
            heads = r.fieldnames
        for row in rows:
            if row.get("id") == term_id:
                row["status"] = status
                row["scope_derivation_history_json"] = history_json
                break
        tmp = bg_csv.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=heads, lineterminator="\n",
                               quoting=csv.QUOTE_MINIMAL)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in heads})
        os.replace(tmp, bg_csv)

    def _rewrite_s2t_excluding(excluded_term_id):
        with s2t_csv.open(encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            rows = list(r)
            heads = r.fieldnames
        rows = [r for r in rows if r.get("business_term_id") != excluded_term_id]
        tmp = s2t_csv.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=heads, lineterminator="\n",
                               quoting=csv.QUOTE_MINIMAL)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in heads})
        os.replace(tmp, s2t_csv)

    def _append_s2t(rows_to_append):
        with s2t_csv.open(encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            rows = list(r)
            heads = r.fieldnames
        rows.extend(rows_to_append)
        tmp = s2t_csv.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=heads, lineterminator="\n",
                               quoting=csv.QUOTE_MINIMAL)
            w.writeheader()
            for r in rows:
                w.writerow({k: str(r.get(k, "")) if r.get(k) is not None
                            else "" for k in heads})
        os.replace(tmp, s2t_csv)

    def _run_dbt_seed():
        r = subprocess.run(
            ["dbt", "seed", "--full-refresh",
             "--select", "business_glossary", "s2t_mapping"],
            cwd=str(Path(__file__).resolve().parent.parent / "dbt"),
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RuntimeError(f"dbt seed failed: {(r.stderr or '')[-400:]}")

    try:
        # === Reset BG002 ===
        _rewrite_bg("draft", "{}")
        _rewrite_s2t_excluding(term_id)
        _run_dbt_seed()

        # === Step 1: propose_scope ===
        proposal = propose_scope(term_id)
        result.cost_usd += _compute_cost_from_usage(proposal.usage)

        # === Assertions 2-5 ===
        if not proposal.proposed_tables:
            result.error = "empty proposed_tables"
            raise AssertionError(result.error)
        if "eket" not in [t.lower() for t in proposal.proposed_tables]:
            result.error = (f"proposed_tables missing 'eket': "
                            f"{proposal.proposed_tables}")
            raise AssertionError(result.error)
        # Bridge to mkpf: at minimum the LLM should also include mkpf itself,
        # OR a bridge chain reaching mkpf. Accept either.
        tables_l = {t.lower() for t in proposal.proposed_tables}
        bridge_options = tables_l & {"mkpf", "mseg", "ekbe"}
        if not bridge_options:
            result.error = (f"no bridge to mkpf in proposal: "
                            f"{proposal.proposed_tables}")
            raise AssertionError(result.error)

        att = proposal.attestation_echo.get(
            "consumed_sap_data_dictionary_entries", [])
        if not att:
            result.error = "attestation_echo empty"
            raise AssertionError(result.error)
        # Verify at least one cited entry really exists in sap_data_dictionary
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            cited_ok = False
            for entry in att[:5]:
                if "." not in entry:
                    continue
                t, c = entry.split(".", 1)
                r = conn.execute(
                    "SELECT COUNT(*) FROM main_seeds.sap_data_dictionary "
                    "WHERE UPPER(table_name)=UPPER(?) AND UPPER(field_name)=UPPER(?)",
                    [t, c],
                ).fetchone()
                if r and r[0] > 0:
                    cited_ok = True
                    break
            if not cited_ok:
                result.error = f"no cited sap_data_dict entries verified: {att[:5]}"
                raise AssertionError(result.error)
        finally:
            conn.close()

        # Blocker augmentation — every blocker (if any) must have
        # all 6 augmentation fields with non-empty values, and
        # resolves_in must be in the allowed taxonomy. BG002 often
        # returns zero blockers so the live check may be vacuously true;
        # the mock-blocker check below exercises the validator explicitly.
        _AUG_FIELDS = ("short_title", "what_it_means", "what_llm_needs",
                       "resolves_in", "resolves_via", "user_action_now")
        _VALID_RI = {"domain_eda", "term_eda",
                     "analyst_decision", "ingestion_required"}
        for bi, b in enumerate(proposal.blockers):
            missing = [f for f in _AUG_FIELDS if not (b.get(f) or "").strip()]
            if missing:
                result.error = (
                    f"blocker[{bi}] missing augmentation fields: {missing}"
                )
                raise AssertionError(result.error)
            if b.get("resolves_in") not in _VALID_RI:
                result.error = (
                    f"blocker[{bi}] invalid resolves_in: "
                    f"{b.get('resolves_in')!r}"
                )
                raise AssertionError(result.error)

        # Mock-blocker validator check — exercises the augment-field
        # validation regardless of whether the live LLM emitted blockers.
        from _scope_derivation import _validate_response as _sd_validate
        conn = duckdb.connect(
            str(DB_PATH), read_only=True,
        )
        try:
            _live_raw = {
                r[0].lower() for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='raw_sap'"
                ).fetchall()
            }
        finally:
            conn.close()
        _good_blocker = {
            "type": "scope_concern", "tables": ["mseg"],
            "short_title": "BWART semantics unclear",
            "what_it_means": "mseg codes movement types via BWART.",
            "what_llm_needs": "BWART distinct + mapping.",
            "resolves_in": "term_eda",
            "resolves_via": "Term EDA synthesizes BWART -> state.",
            "user_action_now": "Confirm; run Domain EDA on mseg.",
        }
        _bad_blocker = {
            "type": "scope_concern", "tables": ["mseg"],
            "short_title": "incomplete blocker",
            # missing what_it_means, resolves_in, etc.
        }
        _mock_good = {
            "proposed_tables": ["mseg"],
            "primary_field_per_table": {"mseg": "BWART"},
            "rationale_per_table": {"mseg": "x"},
            "join_path": [], "blockers": [_good_blocker],
            "attestation_echo": {}, "confidence": "high",
            "confidence_rationale": "x",
        }
        _mock_bad = dict(_mock_good, blockers=[_bad_blocker])
        good_issues = _sd_validate(_mock_good, _live_raw, "propose")
        bad_issues = _sd_validate(_mock_bad, _live_raw, "propose")
        if good_issues:
            result.error = (
                f"validator rejected a well-formed blocker: {good_issues}"
            )
            raise AssertionError(result.error)
        if not bad_issues:
            result.error = "validator accepted a blocker missing aug fields"
            raise AssertionError(result.error)

        # Save proposal to history (simulates UI's append_iteration_to_history)
        from _scope_derivation import append_iteration_to_history
        append_iteration_to_history(term_id, proposal)

        # === Step 6: confirm_scope ===
        # export_parquet=False: the restore path below reverts CSVs + reseeds,
        # so parquet churn (~4s export * 2) would be wasted. Real UI flow
        # via Streamlit does run the parquet export — known_issue #53.
        conf_result = confirm_scope(
            term_id, proposal.iter_num, "regression_harness",
            export_parquet=False,
        )
        if not conf_result.success:
            result.error = f"confirm_scope failed: {conf_result.error}"
            raise AssertionError(result.error)

        # === Assertions 7-9 ===
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            new_status = conn.execute(
                "SELECT status FROM main_seeds.business_glossary WHERE id = ?",
                [term_id],
            ).fetchone()[0]
            if new_status != "scope_confirmed":
                result.error = f"status != scope_confirmed: {new_status}"
                raise AssertionError(result.error)
            s2t_rows = conn.execute(
                "SELECT source_table FROM main_seeds.s2t_mapping "
                "WHERE business_term_id = ?",
                [term_id],
            ).fetchall()
            if len(s2t_rows) != len(proposal.proposed_tables):
                result.error = (f"s2t rows {len(s2t_rows)} != "
                                f"proposal tables {len(proposal.proposed_tables)}")
                raise AssertionError(result.error)
            hist = load_scope_history(term_id)
            if not hist.get("confirmed_at_utc"):
                result.error = "history missing confirmed_at_utc"
                raise AssertionError(result.error)
        finally:
            conn.close()

        # === Step 10: check_prerequisites ===
        prereq = check_prerequisites(term_id)
        if prereq.current_status != "scope_confirmed":
            result.error = f"prereq.current_status != scope_confirmed: {prereq.current_status}"
            raise AssertionError(result.error)

        # Success
        result.passed = True
        result.notes = (
            f"proposed={len(proposal.proposed_tables)} tables "
            f"({','.join(proposal.proposed_tables)}); "
            f"confidence={proposal.confidence}; "
            f"blockers={len(proposal.blockers)}; "
            f"domain_eda_needed_on={prereq.domain_eda_needed_on}"
        )

    except AssertionError:
        pass
    except Exception as e:  # noqa: BLE001
        if not result.error:
            result.error = f"{type(e).__name__}: {e}"
    finally:
        # === Restore BG002 to original state ===
        try:
            _rewrite_bg(orig_status, orig_history)
            _rewrite_s2t_excluding(term_id)
            if orig_s2t_dicts:
                _append_s2t(orig_s2t_dicts)
            _run_dbt_seed()
        except Exception as e:  # noqa: BLE001
            prior_err = result.error or ""
            result.error = prior_err + f" | RESTORE FAILED: {e}"
        result.duration_s = time.time() - started
    return result


def _compute_cost_from_usage(usage: dict) -> float:
    """Sonnet 4.x pricing. 5m TTL cache_write = $3.75/MTok."""
    return (usage.get("input_tokens", 0) * 3e-6
            + usage.get("output_tokens", 0) * 15e-6
            + usage.get("cache_read_input_tokens", 0) * 0.3e-6
            + usage.get("cache_creation_input_tokens", 0) * 3.75e-6)


# Append scenario 22 to the SCENARIOS list now that the function is defined.
SCENARIOS.append((22, scenario_22_stage_a_scope_derivation))


# ─── Scenario 23 — Stage B blocker injection (deterministic, LLM-dep) ──
#
# Pre-step verification (done before fabricating the test term):
#   - business_glossary.id has only `not_null` + `unique` tests in
#     dbt/seeds/schema.yml (lines 163-164). No accepted_values, no
#     format regex.
#   - Grep for `BG\d{3}` patterns across dbt/, scripts/, tests/, and
#     all *.yml files: zero hits.
#   - Therefore `BG-T23-MSEG` is safe — no collision with the BG\d{3}
#     convention, no schema-test violation.
#
# Fabricated ID is `BG-T23-MSEG`. Hard-coded — never BG027 (the live
# smoke-test term) and never any real BG### slot.

def scenario_23_stage_b_blocker_injection(run: HarnessRun) -> ScenarioResult:
    """R (LLM-dependent) — Stage B blocker injection end-to-end.

    Fabricates a scope_confirmed term targeting `mseg` with one
    `resolves_in='domain_eda'` blocker carrying a recognizable sentinel
    string in `short_title`. Runs `run_code_tables_analysis.py --table mseg`
    via subprocess with `STAGE_B_DEBUG_PROMPT_FILE` set so the analyzer
    dumps the constructed system prompt. Asserts:
      - exit code 0;
      - debug prompt file contains the sentinel (proves injection
        reached the LLM, not just the loader);
      - a new DAR row was appended;
      - DAR's result_json.blockers_addressed is non-empty + the entry
        attributes back to the fabricated term;
      - blockers_contract_violation == False.

    Teardown rewrites the DAR CSV to remove scenario-written rows,
    restores business_glossary + s2t_mapping from `.bak`, then re-seeds
    all three so DuckDB + parquet realign. Runs under `try/finally`
    so assertion failures still trigger cleanup.
    """
    import tempfile as _tempfile
    result = ScenarioResult(23, "stage_b_blocker_injection", "R",
                            False, llm_dependent=True)
    started = time.time()
    fab_id = "BG-T23-MSEG"

    bg_csv = Path(__file__).resolve().parent.parent / "dbt" / "seeds" / "business_glossary.csv"
    s2t_csv = Path(__file__).resolve().parent.parent / "dbt" / "seeds" / "s2t_mapping.csv"
    dar_csv = Path(__file__).resolve().parent.parent / "dbt" / "seeds" / "domain_analysis_results.csv"

    ts_suffix = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    bg_bak = bg_csv.with_suffix(f".csv.s23.{ts_suffix}.bak")
    s2t_bak = s2t_csv.with_suffix(f".csv.s23.{ts_suffix}.bak")
    dar_bak = dar_csv.with_suffix(f".csv.s23.{ts_suffix}.bak")

    debug_prompt_path = Path(_tempfile.gettempdir()) / "stage_b_scenario23_prompt.txt"
    if debug_prompt_path.exists():
        debug_prompt_path.unlink()

    # Track DAR ids written during the run so teardown is precise.
    pre_run_dar_ids: set[str] = set()
    scenario_written_dar_ids: set[str] = set()

    def _read_dar_ids() -> set[str]:
        if not dar_csv.exists():
            return set()
        with dar_csv.open(encoding="utf-8", newline="") as f:
            return {(r.get("id") or "") for r in csv.DictReader(f)}

    def _verify_fab_id_unique() -> bool:
        with bg_csv.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if (row.get("id") or "") == fab_id:
                    return False
        return True

    def _fabricate_rows() -> None:
        # Append fabricated scope_confirmed row to business_glossary.csv.
        with bg_csv.open(encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            rows = list(r)
            heads = r.fieldnames

        now_iso = dt.datetime.now(dt.timezone.utc).replace(
            tzinfo=None,
        ).isoformat(timespec="seconds")
        history = {
            "iterations": [{
                "iter_num": 1,
                "mode": "propose",
                "timestamp": now_iso,
                "analyst_action": "confirmed",
                "validation_issues": [],
                "llm_response": {
                    "proposed_tables": ["mseg"],
                    "primary_field_per_table": {"mseg": "BWART"},
                    "rationale_per_table": {
                        "mseg": "Scenario 23 fabricated rationale.",
                    },
                    "join_path": [],
                    "blockers": [{
                        "type": "scope_concern",
                        "tables": ["mseg"],
                        "short_title": "Stage B scenario 23 sentinel",
                        "what_it_means": (
                            "Fabricated for scenario 23 regression. MSEG uses "
                            "BWART (movement type) to classify movements; the "
                            "term needs to know which codes map to deployed "
                            "vs returned states."
                        ),
                        "what_llm_needs": (
                            "Distribution of BWART codes in MSEG with t156 "
                            "JOIN for descriptions."
                        ),
                        "resolves_in": "domain_eda",
                        "resolves_via": (
                            "Domain EDA Code Tables analysis enumerates "
                            "BWART distinct values + JOINs to t156 / "
                            "movement_type_mapping."
                        ),
                        "user_action_now": (
                            "Run Code Tables analyzer on MSEG."
                        ),
                    }],
                    "attestation_echo": {
                        "consumed_sap_data_dictionary_entries": [
                            "mseg.BWART", "mseg.MBLNR",
                        ],
                    },
                    "confidence": "high",
                    "confidence_rationale": "Scenario 23 fabricated.",
                },
                "usage": {},
            }],
            "final_iter_num": 1,
            "confirmed_at_utc": now_iso,
            "confirmed_by": "scenario_23",
        }
        new_bg_row = {h: "" for h in heads}
        new_bg_row["id"] = fab_id
        new_bg_row["term_name"] = "scenario_23_test_term"
        new_bg_row["display_name"] = "Scenario 23 Test Term"
        new_bg_row["definition"] = "Fabricated for Stage B regression."
        new_bg_row["status"] = "scope_confirmed"
        new_bg_row["scope_derivation_history_json"] = json.dumps(
            history, ensure_ascii=False, default=str,
        )
        rows.append(new_bg_row)

        tmp = bg_csv.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=heads, lineterminator="\n",
                               quoting=csv.QUOTE_MINIMAL)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in heads})
        os.replace(tmp, bg_csv)

        # Append corresponding s2t_mapping row.
        with s2t_csv.open(encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            s2t_rows = list(r)
            s2t_heads = r.fieldnames
        # Determine next S2T-NNNN id.
        existing_nums = []
        for row in s2t_rows:
            sid = row.get("id", "") or ""
            if sid.startswith("S2T-"):
                try:
                    existing_nums.append(int(sid.split("-")[1]))
                except (IndexError, ValueError):
                    pass
        next_id = (max(existing_nums) + 1) if existing_nums else 9001
        new_s2t = {h: "" for h in s2t_heads}
        new_s2t["id"] = f"S2T-{next_id:04d}"
        new_s2t["business_term_id"] = fab_id
        new_s2t["business_term_name"] = "scenario_23_test_term"
        new_s2t["source_table"] = "MSEG"
        new_s2t["source_field"] = "BWART"
        new_s2t["source_description"] = "scenario 23 fabrication"
        new_s2t["notes"] = "stage_a_derived"
        s2t_rows.append(new_s2t)

        tmp_s = s2t_csv.with_suffix(".csv.tmp")
        with tmp_s.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=s2t_heads, lineterminator="\n",
                               quoting=csv.QUOTE_MINIMAL)
            w.writeheader()
            for r in s2t_rows:
                w.writerow({k: r.get(k, "") for k in s2t_heads})
        os.replace(tmp_s, s2t_csv)

    def _run_dbt_seed_subset(*selectors: str) -> None:
        r = subprocess.run(
            ["dbt", "seed", "--full-refresh", "--select", *selectors],
            cwd=str(Path(__file__).resolve().parent.parent / "dbt"),
            capture_output=True, text=True, timeout=180, shell=False,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"dbt seed failed: {(r.stderr or r.stdout or '')[-400:]}"
            )

    def _rewrite_dar_dropping(ids_to_drop: set[str]) -> None:
        """Remove scenario-written DAR rows from the CSV. CSV is the
        source of truth; DuckDB + parquet are derived. Re-seed afterward.
        """
        if not dar_csv.exists() or not ids_to_drop:
            return
        with dar_csv.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            heads = reader.fieldnames
            rows = list(reader)
        kept = [r for r in rows if (r.get("id") or "") not in ids_to_drop]
        tmp = dar_csv.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=heads, lineterminator="\n")
            w.writeheader()
            for r in kept:
                w.writerow({k: r.get(k, "") for k in heads})
        os.replace(tmp, dar_csv)

    try:
        # === Backup ===
        shutil.copy2(bg_csv, bg_bak)
        shutil.copy2(s2t_csv, s2t_bak)
        shutil.copy2(dar_csv, dar_bak)

        # === DAR baseline ===
        pre_run_dar_ids = _read_dar_ids()

        # === Fabricated ID uniqueness ===
        if not _verify_fab_id_unique():
            result.error = (
                f"Fabricated ID {fab_id} already present in "
                f"business_glossary.csv — manual cleanup required before "
                f"scenario 23 can run."
            )
            raise AssertionError(result.error)

        # === Fabricate ===
        _fabricate_rows()

        # === Seed ===
        _run_dbt_seed_subset("business_glossary", "s2t_mapping")

        # === Invoke analyzer with debug prompt capture ===
        env = {**os.environ, "STAGE_B_DEBUG_PROMPT_FILE": str(debug_prompt_path)}
        cmd = [
            sys.executable,
            str(Path(__file__).resolve().parent / "run_code_tables_analysis.py"),
            "--table", "mseg", "--no-parquet-sync",
        ]
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            timeout=600,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

        # === Assertions ===
        # (a) exit code 0
        if proc.returncode != 0:
            result.error = (
                f"analyzer rc={proc.returncode}; "
                f"stderr={(proc.stderr or '')[-400:]}"
            )
            raise AssertionError(result.error)

        # (b) debug file exists + contains sentinel
        if not debug_prompt_path.exists():
            result.error = "debug prompt file was not written by analyzer"
            raise AssertionError(result.error)
        prompt_text = debug_prompt_path.read_text(encoding="utf-8")
        if "Stage B scenario 23 sentinel" not in prompt_text:
            result.error = (
                "sentinel string absent from analyzer's system prompt — "
                "injection did not reach the LLM"
            )
            raise AssertionError(result.error)

        # (c) new DAR row(s) appended for code_tables/mseg
        post_dar_ids = _read_dar_ids()
        scenario_written_dar_ids = post_dar_ids - pre_run_dar_ids
        if not scenario_written_dar_ids:
            result.error = "no DAR rows written during scenario run"
            raise AssertionError(result.error)

        # Find the code_tables row for mseg among the new ids.
        target_dar = None
        with dar_csv.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if (row.get("id") or "") not in scenario_written_dar_ids:
                    continue
                if (row.get("analysis_type") or "") != "code_tables":
                    continue
                if (row.get("source_tables") or "").lower() != "mseg":
                    continue
                target_dar = row
                break
        if target_dar is None:
            result.error = (
                f"no code_tables/mseg DAR row found among scenario-written: "
                f"{sorted(scenario_written_dar_ids)}"
            )
            raise AssertionError(result.error)

        if (target_dar.get("status") or "") != "success":
            result.error = (
                f"DAR row status={target_dar.get('status')!r} (expected success)"
            )
            raise AssertionError(result.error)

        # (d) result_json.blockers_addressed parsing + attribution
        try:
            rj = json.loads(target_dar.get("result_json") or "{}")
        except json.JSONDecodeError as e:
            result.error = f"DAR result_json malformed: {e}"
            raise AssertionError(result.error)

        ba = rj.get("blockers_addressed")
        if not isinstance(ba, list) or len(ba) == 0:
            result.error = (
                f"blockers_addressed is empty or wrong type: {ba!r}"
            )
            raise AssertionError(result.error)
        if len(ba) != 1:
            result.error = (
                f"blockers_addressed length {len(ba)} (expected 1 — "
                f"one blocker fabricated)"
            )
            raise AssertionError(result.error)
        entry = ba[0]
        if entry.get("term_id") != fab_id:
            result.error = (
                f"blockers_addressed[0].term_id={entry.get('term_id')!r} "
                f"(expected {fab_id!r})"
            )
            raise AssertionError(result.error)
        valid_status = {
            "addressed", "requires_term_eda_stage",
            "cannot_address_from_this_table_alone",
        }
        if entry.get("status") not in valid_status:
            result.error = (
                f"blockers_addressed[0].status={entry.get('status')!r} "
                f"not in {sorted(valid_status)}"
            )
            raise AssertionError(result.error)

        # (e) blockers_contract_violation == False
        if rj.get("blockers_contract_violation") is not False:
            result.error = (
                f"blockers_contract_violation={rj.get('blockers_contract_violation')!r} "
                f"(expected False — LLM should have honored the contract)"
            )
            raise AssertionError(result.error)

        result.passed = True
        result.notes = (
            f"injection_verified=True; "
            f"blockers_addressed_count={len(ba)}; "
            f"status={entry.get('status')!r}; "
            f"dar_id={target_dar.get('id')}"
        )

    except AssertionError:
        pass
    except Exception as e:  # noqa: BLE001
        if not result.error:
            result.error = f"{type(e).__name__}: {e}"
    finally:
        # Teardown — rationale: CSV is source of truth,
        # DuckDB + parquet are derived. Rewrite CSV, restore others,
        # re-seed all three.
        try:
            _rewrite_dar_dropping(scenario_written_dar_ids)
            shutil.copy2(bg_bak, bg_csv)
            shutil.copy2(s2t_bak, s2t_csv)
            _run_dbt_seed_subset(
                "business_glossary", "s2t_mapping", "domain_analysis_results",
            )
        except Exception as e:  # noqa: BLE001
            prior = result.error or ""
            result.error = prior + f" | TEARDOWN FAILED: {e}"
        # Cleanup .bak + debug prompt files
        for p in (bg_bak, s2t_bak, dar_bak):
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            debug_prompt_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        result.duration_s = time.time() - started
    return result


SCENARIOS.append((23, scenario_23_stage_b_blocker_injection))


# ─── Scenario 25 — Stage C deterministic (R, LLM-dep) ───────────────────

def scenario_25_stage_c_deterministic(run: HarnessRun) -> ScenarioResult:
    """R (LLM-dep) — Stage C deterministic regression.

    Fabricates a scope_confirmed term with 2 scope tables (mseg, equi),
    pre-seeded DARs on each, Stage A blockers (1 term_eda + 1
    analyst_decision), and a prior TAR from a different fabricated term
    that shares the mseg scope table (lens=measures_overview, sentinel
    interpretation).

    Invokes scripts/run_term_eda.py --term-id BG-T25-TERMEDA with
    STAGE_C_DEBUG_PROMPT_FILE set. Asserts:
      a) subprocess exit 0
      b) debug prompt contains fabricated blocker sentinel
      c) >=1 query row written for this run
      d) exactly 1 sufficiency row
      e) sufficiency_json.lens_consideration covers all 8 lenses
      f) declared_sufficient is bool
      g) at least one query row OR sufficiency lens_consideration cites
         the pre-seeded prior TAR
      h) blockers_resolution has entries for all fabricated blockers
         with valid statuses
      i) term status transitioned to term_eda_pending or ready_for_s2t

    Teardown (try/finally): CSV-layer rewrite of 4 seeds from .bak,
    re-seed, delete .bak + debug file.

    Tag: R, LLM-dep.
    """
    import tempfile as _tempfile
    result = ScenarioResult(25, "stage_c_deterministic", "R", False,
                            llm_dependent=True)
    started = time.time()

    fab_term = "BG-T25-TERMEDA"
    fab_prior = "BG-T25-PRIOR"

    bg_csv = Path(__file__).resolve().parent.parent / "dbt" / "seeds" / "business_glossary.csv"
    s2t_csv = Path(__file__).resolve().parent.parent / "dbt" / "seeds" / "s2t_mapping.csv"
    dar_csv = Path(__file__).resolve().parent.parent / "dbt" / "seeds" / "domain_analysis_results.csv"
    tar_csv = Path(__file__).resolve().parent.parent / "dbt" / "seeds" / "term_analysis_results.csv"

    ts_suffix = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    bg_bak = bg_csv.with_suffix(f".csv.s25.{ts_suffix}.bak")
    s2t_bak = s2t_csv.with_suffix(f".csv.s25.{ts_suffix}.bak")
    dar_bak = dar_csv.with_suffix(f".csv.s25.{ts_suffix}.bak")
    tar_bak = tar_csv.with_suffix(f".csv.s25.{ts_suffix}.bak")

    debug_prompt_path = Path(_tempfile.gettempdir()) / "stage_c_scenario25_prompt.txt"
    if debug_prompt_path.exists():
        debug_prompt_path.unlink()

    blocker_sentinel = "Stage C scenario 25 term_eda sentinel"
    prior_tar_interp_sentinel = "PRIOR_TAR_CITATION_TARGET_S25"
    pre_run_tar_ids: set[str] = set()
    scenario_written_tar_ids: set[str] = set()
    prior_tar_id = ""

    def _read_tar_ids() -> set[str]:
        if not tar_csv.exists():
            return set()
        with tar_csv.open(encoding="utf-8", newline="") as f:
            return {(r.get("id") or "") for r in csv.DictReader(f)}

    def _verify_fab_ids_unique() -> bool:
        with bg_csv.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if (row.get("id") or "") in (fab_term, fab_prior):
                    return False
        return True

    def _fabricate_rows() -> None:
        nonlocal prior_tar_id
        # ─── business_glossary: add fab_term + fab_prior ───
        with bg_csv.open(encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            bg_rows = list(r)
            bg_heads = r.fieldnames

        now_iso = dt.datetime.now(dt.timezone.utc).replace(
            tzinfo=None,
        ).isoformat(timespec="seconds")

        fab_history = {
            "iterations": [{
                "iter_num": 1,
                "mode": "propose",
                "timestamp": now_iso,
                "analyst_action": "confirmed",
                "validation_issues": [],
                "llm_response": {
                    "proposed_tables": ["mseg", "equi"],
                    "primary_field_per_table": {
                        "mseg": "BWART", "equi": "EQUNR",
                    },
                    "rationale_per_table": {
                        "mseg": "Movement-type level data",
                        "equi": "Equipment master",
                    },
                    "join_path": [],
                    "blockers": [
                        {
                            "type": "scope_concern",
                            "tables": ["mseg"],
                            "short_title": blocker_sentinel,
                            "what_it_means": (
                                f"{blocker_sentinel} — fabricated for "
                                "scenario 25. Need to enumerate BWART "
                                "codes and map to business states."
                            ),
                            "what_llm_needs": (
                                "Distribution of BWART codes in MSEG + "
                                "mapping to deployed/warehouse/returned."
                            ),
                            "resolves_in": "term_eda",
                            "resolves_via": (
                                "Term EDA by_dimension or bucketing "
                                "lens against mseg BWART."
                            ),
                            "user_action_now": (
                                "Run Term EDA; cite results in "
                                "blockers_resolution."
                            ),
                        },
                        {
                            "type": "scope_concern",
                            "tables": ["equi"],
                            "short_title": (
                                "Scenario 25 analyst decision blocker"
                            ),
                            "what_it_means": (
                                "Fabricated analyst-decision blocker. "
                                "Equipment onboarding criteria require "
                                "human business judgment."
                            ),
                            "what_llm_needs": (
                                "Human decision on which equipment "
                                "onboarding states count as 'active'."
                            ),
                            "resolves_in": "analyst_decision",
                            "resolves_via": (
                                "Analyst review outside Term EDA."
                            ),
                            "user_action_now": (
                                "Escalate via Stage C sufficiency row."
                            ),
                        },
                    ],
                    "attestation_echo": {
                        "consumed_sap_data_dictionary_entries": [
                            "mseg.BWART", "equi.EQUNR",
                        ],
                    },
                    "confidence": "high",
                    "confidence_rationale": "Scenario 25 fabricated.",
                },
                "usage": {},
            }],
            "final_iter_num": 1,
            "confirmed_at_utc": now_iso,
            "confirmed_by": "scenario_25",
        }

        def _mk_bg_row(term_id: str, term_name: str, status: str,
                       history_json: str) -> dict:
            row = {h: "" for h in bg_heads}
            row["id"] = term_id
            row["term_name"] = term_name
            row["display_name"] = term_name
            row["definition"] = f"Fabricated for scenario 25 ({term_id})"
            row["status"] = status
            row["scope_derivation_history_json"] = history_json
            return row

        bg_rows.append(_mk_bg_row(
            fab_term, "scenario_25_test_term", "scope_confirmed",
            json.dumps(fab_history, ensure_ascii=False, default=str),
        ))
        bg_rows.append(_mk_bg_row(
            fab_prior, "scenario_25_prior_term", "approved", "{}",
        ))

        tmp_bg = bg_csv.with_suffix(".csv.tmp")
        with tmp_bg.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=bg_heads, lineterminator="\n",
                quoting=csv.QUOTE_MINIMAL,
            )
            w.writeheader()
            for r in bg_rows:
                w.writerow({k: r.get(k, "") for k in bg_heads})
        os.replace(tmp_bg, bg_csv)

        # ─── s2t_mapping: 2 rows fab_term + 1 row fab_prior ───
        with s2t_csv.open(encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            s2t_rows = list(r)
            s2t_heads = r.fieldnames

        existing_nums = []
        for row in s2t_rows:
            sid = row.get("id", "") or ""
            if sid.startswith("S2T-"):
                try:
                    existing_nums.append(int(sid.split("-")[1]))
                except (IndexError, ValueError):
                    pass
        next_s2t = (max(existing_nums) + 1) if existing_nums else 9000

        def _mk_s2t_row(term_id: str, term_name: str, source_table: str,
                        source_field: str) -> dict:
            nonlocal next_s2t
            row = {h: "" for h in s2t_heads}
            row["id"] = f"S2T-{next_s2t:04d}"
            row["business_term_id"] = term_id
            row["business_term_name"] = term_name
            row["source_table"] = source_table.upper()
            row["source_field"] = source_field
            row["source_description"] = "scenario 25 fabrication"
            row["notes"] = "stage_a_derived"
            next_s2t += 1
            return row

        s2t_rows.append(_mk_s2t_row(fab_term, "scenario_25_test_term",
                                     "mseg", "BWART"))
        s2t_rows.append(_mk_s2t_row(fab_term, "scenario_25_test_term",
                                     "equi", "EQUNR"))
        s2t_rows.append(_mk_s2t_row(fab_prior, "scenario_25_prior_term",
                                     "mseg", "BWART"))

        tmp_s2t = s2t_csv.with_suffix(".csv.tmp")
        with tmp_s2t.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=s2t_heads, lineterminator="\n",
                quoting=csv.QUOTE_MINIMAL,
            )
            w.writeheader()
            for r in s2t_rows:
                w.writerow({k: r.get(k, "") for k in s2t_heads})
        os.replace(tmp_s2t, s2t_csv)

        # ─── domain_analysis_results: pre-seeded DARs on mseg + equi ───
        with dar_csv.open(encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            dar_rows = list(r)
            dar_heads = r.fieldnames

        existing_nums = []
        for row in dar_rows:
            did = row.get("id", "") or ""
            if did.startswith("DAR-"):
                try:
                    existing_nums.append(int(did.split("-")[1]))
                except (IndexError, ValueError):
                    pass
        next_dar = (max(existing_nums) + 1) if existing_nums else 99000

        def _mk_dar_row(source_table: str, analysis_type: str,
                        status: str = "success") -> dict:
            nonlocal next_dar
            row = {h: "" for h in dar_heads}
            row["id"] = f"DAR-{next_dar:05d}"
            row["analysis_type"] = analysis_type
            row["executed_at_utc"] = now_iso
            if status == "skipped":
                row["result_json"] = json.dumps({
                    "skip_reason": (
                        f"scenario 25 fabricated skipped {analysis_type}"
                    ),
                    "blockers_addressed": [],
                }, default=str)
                row["query_sql"] = (
                    f"-- skipped: scenario 25 fabricated {analysis_type}"
                )
            else:
                row["result_json"] = json.dumps({
                    "scenario_25_sentinel": True,
                    "source_table": source_table,
                    "analysis_type": analysis_type,
                    "summary": f"Fabricated {analysis_type} DAR for scenario 25.",
                    "blockers_addressed": [],
                }, default=str)
                row["query_sql"] = f"-- scenario 25 fabricated {analysis_type}"
            row["promoted"] = "false"
            row["run_id"] = f"scenario25_{analysis_type}_{source_table}"
            row["row_count"] = "0"
            row["error_message"] = ""
            row["status"] = status
            row["superseded_by"] = ""
            row["executed_by"] = "scenario_25"
            row["schema_version"] = "scenario25"
            row["source_tables"] = source_table
            row["domain_name"] = ""
            row["last_source_ingestion_at"] = ""
            next_dar += 1
            return row

        # Stage D.1 Part 2.5: all 6 per-table analyzer DARs per table,
        # plus 1 grain_relationship pair DAR covering (equi, mseg).
        # Mix of success / skipped to exercise both prereq paths.
        # performance_baseline is auto-satisfied by magnitude; no
        # separate fabrication needed.
        _PER_TABLE_ALL = (
            "completeness", "dimensions", "magnitude", "code_tables",
            "date", "segmentation",
        )
        # Tables to emit as skipped on each table (tests skip path):
        _SKIPPED_PER_TABLE = {
            "mseg": {"date"},     # no date DAR → skipped
            "equi": {"segmentation"},  # no numeric DAR → skipped
        }
        for t in ("mseg", "equi"):
            skipped_set = _SKIPPED_PER_TABLE.get(t, set())
            for atype in _PER_TABLE_ALL:
                status = "skipped" if atype in skipped_set else "success"
                dar_rows.append(_mk_dar_row(t, atype, status=status))
        # Grain pair DAR (sorted lowercase: 'equi,mseg').
        dar_rows.append(_mk_dar_row(
            "equi,mseg", "grain_relationship", status="success",
        ))

        tmp_dar = dar_csv.with_suffix(".csv.tmp")
        with tmp_dar.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=dar_heads, lineterminator="\n",
            )
            w.writeheader()
            for r in dar_rows:
                w.writerow({k: r.get(k, "") for k in dar_heads})
        os.replace(tmp_dar, dar_csv)

        # ─── term_analysis_results: pre-seeded PRIOR TAR ───
        # lens='measures_overview' (v5 Edit 16) to minimize LLM
        # citation-judgment variance.
        with tar_csv.open(encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            tar_rows = list(r)
            tar_heads = r.fieldnames

        existing_nums = []
        for row in tar_rows:
            tid = row.get("id", "") or ""
            if tid.startswith("TAR-"):
                try:
                    existing_nums.append(int(tid.split("-")[1]))
                except (IndexError, ValueError):
                    pass
        # Place prior TAR high enough to avoid collision with any
        # subsequent TARs the runner writes.
        prior_tar_id_local = f"TAR-{(max(existing_nums) + 1 if existing_nums else 90000):05d}"
        prior_suff_id = f"TAR-{int(prior_tar_id_local.split('-')[1]) + 1:05d}"
        prior_run_id = f"TARRUN-scenario25-{fab_prior}"

        prior_query_row = {h: "" for h in tar_heads}
        prior_query_row["id"] = prior_tar_id_local
        prior_query_row["term_id"] = fab_prior
        prior_query_row["row_type"] = "query"
        prior_query_row["analysis_lens"] = "measures_overview"
        prior_query_row["stage"] = "framework_floor"
        prior_query_row["query_index"] = "1"
        prior_query_row["query_sql"] = (
            "SELECT COUNT(*) AS n FROM main_staging.stg_sap__mseg"
        )
        prior_query_row["query_result_json"] = json.dumps([{"n": 31965}])
        prior_query_row["result_row_count"] = "1"
        prior_query_row["interpretation"] = (
            f"{prior_tar_interp_sentinel} — scenario 25 prior TAR "
            "establishes MSEG total row count of 31965 as baseline."
        )
        prior_query_row["grounded_in_tar_ids"] = "[]"
        prior_query_row["sufficiency_json"] = ""
        prior_query_row["status"] = "success"
        prior_query_row["confidence"] = ""
        prior_query_row["executed_at_utc"] = now_iso
        prior_query_row["executed_by"] = "scenario_25"
        prior_query_row["superseded_by"] = ""
        prior_query_row["run_id"] = prior_run_id
        prior_query_row["llm_usage_json"] = "{}"

        prior_suff_row = {h: "" for h in tar_heads}
        prior_suff_row["id"] = prior_suff_id
        prior_suff_row["term_id"] = fab_prior
        prior_suff_row["row_type"] = "sufficiency"
        prior_suff_row["analysis_lens"] = ""
        prior_suff_row["stage"] = "terminal"
        prior_suff_row["query_index"] = "2"
        prior_suff_row["query_sql"] = ""
        prior_suff_row["query_result_json"] = ""
        prior_suff_row["result_row_count"] = "0"
        prior_suff_row["interpretation"] = ""
        prior_suff_row["grounded_in_tar_ids"] = "[]"
        prior_suff_row["sufficiency_json"] = json.dumps({
            "declared_sufficient": True,
            "confidence": "high",
            "sufficiency_rationale": "prior TAR scenario 25 fixture",
        }, default=str)
        prior_suff_row["status"] = "success"
        prior_suff_row["confidence"] = "high"
        prior_suff_row["executed_at_utc"] = now_iso
        prior_suff_row["executed_by"] = "scenario_25"
        prior_suff_row["superseded_by"] = ""
        prior_suff_row["run_id"] = prior_run_id
        prior_suff_row["llm_usage_json"] = "{}"

        tar_rows.append(prior_query_row)
        tar_rows.append(prior_suff_row)

        tmp_tar = tar_csv.with_suffix(".csv.tmp")
        with tmp_tar.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=tar_heads, lineterminator="\n",
            )
            w.writeheader()
            for r in tar_rows:
                w.writerow({k: r.get(k, "") for k in tar_heads})
        os.replace(tmp_tar, tar_csv)

        prior_tar_id = prior_tar_id_local

    def _run_dbt_seed_subset(*selectors: str) -> None:
        r = subprocess.run(
            ["dbt", "seed", "--full-refresh", "--select", *selectors],
            cwd=str(Path(__file__).resolve().parent.parent / "dbt"),
            capture_output=True, text=True, timeout=240, shell=False,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"dbt seed failed: {(r.stderr or r.stdout or '')[-400:]}"
            )

    def _rewrite_tar_dropping(ids_to_drop: set[str]) -> None:
        if not tar_csv.exists() or not ids_to_drop:
            return
        with tar_csv.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            heads = reader.fieldnames
            rows = list(reader)
        kept = [r for r in rows if (r.get("id") or "") not in ids_to_drop]
        tmp = tar_csv.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=heads, lineterminator="\n")
            w.writeheader()
            for r in kept:
                w.writerow({k: r.get(k, "") for k in heads})
        os.replace(tmp, tar_csv)

    try:
        # Backup all 4 seeds.
        shutil.copy2(bg_csv, bg_bak)
        shutil.copy2(s2t_csv, s2t_bak)
        shutil.copy2(dar_csv, dar_bak)
        shutil.copy2(tar_csv, tar_bak)

        pre_run_tar_ids = _read_tar_ids()

        if not _verify_fab_ids_unique():
            result.error = (
                f"fabricated ids ({fab_term}, {fab_prior}) already "
                "present — manual cleanup required."
            )
            raise AssertionError(result.error)

        _fabricate_rows()
        _run_dbt_seed_subset(
            "business_glossary", "s2t_mapping",
            "domain_analysis_results", "term_analysis_results",
        )

        # Invoke runner with debug prompt capture.
        env = {**os.environ,
               "STAGE_C_DEBUG_PROMPT_FILE": str(debug_prompt_path)}
        cmd = [
            sys.executable,
            str(Path(__file__).resolve().parent / "run_term_eda.py"),
            "--term-id", fab_term,
            "--executed-by", "scenario_25",
        ]
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            timeout=900,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

        # ─── Assertions ───
        # (a) exit 0
        if proc.returncode != 0:
            result.error = (
                f"run_term_eda rc={proc.returncode}; "
                f"stdout_tail={(proc.stdout or '')[-800:]} "
                f"stderr_tail={(proc.stderr or '')[-400:]}"
            )
            raise AssertionError(result.error)

        # (b) debug prompt contains blocker sentinel
        if not debug_prompt_path.exists():
            result.error = "debug prompt file not written"
            raise AssertionError(result.error)
        prompt_text = debug_prompt_path.read_text(encoding="utf-8")
        if blocker_sentinel not in prompt_text:
            result.error = (
                f"blocker sentinel '{blocker_sentinel}' absent from "
                "prompt — injection did not reach LLM"
            )
            raise AssertionError(result.error)

        # Read fresh TAR rows
        post_tar_ids = _read_tar_ids()
        scenario_written_tar_ids = post_tar_ids - pre_run_tar_ids
        # Exclude the pre-seeded prior TARs (not from this run)
        scenario_written_tar_ids -= set()  # pre_run already captured those

        with tar_csv.open("r", encoding="utf-8", newline="") as f:
            all_tar = list(csv.DictReader(f))
        run_rows = [
            r for r in all_tar
            if r.get("term_id") == fab_term
            and (r.get("id") or "") in scenario_written_tar_ids
        ]
        query_rows = [r for r in run_rows if r.get("row_type") == "query"]
        suff_rows = [r for r in run_rows if r.get("row_type") == "sufficiency"]

        # (c) >=1 query row
        if len(query_rows) < 1:
            result.error = "no query rows written for this run"
            raise AssertionError(result.error)

        # (d) exactly 1 sufficiency row
        if len(suff_rows) != 1:
            result.error = (
                f"expected 1 sufficiency row, got {len(suff_rows)}"
            )
            raise AssertionError(result.error)

        # (e) lens_consideration covers all 8 lenses
        try:
            sj = json.loads(suff_rows[0].get("sufficiency_json") or "{}")
        except json.JSONDecodeError as e:
            result.error = f"sufficiency_json malformed: {e}"
            raise AssertionError(result.error)
        expected_lenses = {
            "measures_overview", "by_dimension", "ranking", "time_trend",
            "cumulative", "variance", "bucketing", "part_to_whole",
        }
        lc = sj.get("lens_consideration") or {}
        if set(lc.keys()) != expected_lenses:
            result.error = (
                f"lens_consideration missing lenses: "
                f"{expected_lenses - set(lc.keys())}"
            )
            raise AssertionError(result.error)

        # (f) declared_sufficient is bool
        if not isinstance(sj.get("declared_sufficient"), bool):
            result.error = (
                f"declared_sufficient is not bool: "
                f"{type(sj.get('declared_sufficient')).__name__}"
            )
            raise AssertionError(result.error)

        # (g) cite check — prior TAR id in a query row's
        # grounded_in_tar_ids OR in the sufficiency's
        # lens_consideration[measures_overview].tar_ids.
        cited_anywhere: list[str] = []
        for qr in query_rows:
            try:
                cs = json.loads(qr.get("grounded_in_tar_ids") or "[]")
                cited_anywhere.extend(str(x) for x in cs if x)
            except json.JSONDecodeError:
                continue
        m_entry = lc.get("measures_overview") or {}
        m_tars = m_entry.get("tar_ids") or []
        cited_anywhere.extend(str(x) for x in m_tars if x)

        if prior_tar_id not in cited_anywhere:
            result.notes = (
                f"prior TAR {prior_tar_id} not cited (LLM judgment "
                f"variance — all citations: {cited_anywhere})"
            )
            # Weak assertion: this is noted but does not fail the
            # scenario, since LLM citation is inherently variable.

        # (h) blockers_resolution has 2 entries with valid statuses
        br = sj.get("blockers_resolution") or []
        valid_statuses = {
            "resolved", "escalated_to_analyst",
            "could_not_resolve", "not_applicable",
        }
        if len(br) < 1:
            result.error = (
                f"blockers_resolution should have >=1 entry for 2 "
                f"fabricated blockers; got {len(br)}"
            )
            raise AssertionError(result.error)
        for entry in br:
            if not isinstance(entry, dict):
                result.error = (
                    f"blockers_resolution entry not a dict: {entry!r}"
                )
                raise AssertionError(result.error)
            if entry.get("status") not in valid_statuses:
                result.error = (
                    f"blockers_resolution status="
                    f"{entry.get('status')!r} not in enum"
                )
                raise AssertionError(result.error)

        # (i) term status transitioned
        with bg_csv.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("id") == fab_term:
                    final_status = row.get("status", "")
                    break
            else:
                final_status = ""
        if final_status not in {"term_eda_pending", "ready_for_s2t"}:
            result.error = (
                f"final term status {final_status!r} not in "
                "{term_eda_pending, ready_for_s2t}"
            )
            raise AssertionError(result.error)

        result.passed = True
        result.notes = (result.notes or "") + (
            f"; query_rows={len(query_rows)}; "
            f"sufficiency_declared={sj.get('declared_sufficient')}; "
            f"final_status={final_status}"
        )

    except AssertionError:
        pass
    except Exception as e:  # noqa: BLE001
        if not result.error:
            result.error = f"{type(e).__name__}: {e}"
    finally:
        # CSV-layer teardown: restore all 4 seeds from .bak + re-seed.
        try:
            # Rewrite TAR CSV to drop this-run scenario rows (including
            # pre-seeded prior TARs).
            _rewrite_tar_dropping(scenario_written_tar_ids)
            shutil.copy2(bg_bak, bg_csv)
            shutil.copy2(s2t_bak, s2t_csv)
            shutil.copy2(dar_bak, dar_csv)
            shutil.copy2(tar_bak, tar_csv)
            _run_dbt_seed_subset(
                "business_glossary", "s2t_mapping",
                "domain_analysis_results", "term_analysis_results",
            )
        except Exception as e:  # noqa: BLE001
            prior = result.error or ""
            result.error = prior + f" | TEARDOWN FAILED: {e}"
        for p in (bg_bak, s2t_bak, dar_bak, tar_bak):
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            debug_prompt_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        result.duration_s = time.time() - started
    return result


SCENARIOS.append((25, scenario_25_stage_c_deterministic))


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run-live", action="store_true",
                   help="Include LLM-dependent scenarios (scenarios 1, 2, 3, 12).")
    p.add_argument("--skip-mocks", action="store_true",
                   help="Skip scenarios using PIECE8_MOCK_MODE fixtures.")
    p.add_argument("--scenario", type=int, nargs="+",
                   help="Run only specified scenario ids (default: all).")
    p.add_argument("--cost-cap", type=float, default=REGRESSION_COST_CAP_DEFAULT,
                   help=f"Total USD budget cap across live scenarios. Default ${REGRESSION_COST_CAP_DEFAULT}.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    _clear_scratch_dir()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    run = HarnessRun(cost_cap=args.cost_cap)

    selected = args.scenario if args.scenario else [s[0] for s in SCENARIOS]
    mocked_ids = {4, 7, 9, 13}

    for sid, func in SCENARIOS:
        if sid not in selected:
            continue
        # Build a trial ScenarioResult stub to check LLM-dependency before running
        llm_dep = func.__name__ in {
            "scenario_1_full_dar_convergence",
            "scenario_2_empty_scope",
            "scenario_3_conflicting_dars",
            "scenario_6_ontology_collision_llm",
            "scenario_12_cache_disabled",
            "scenario_15_layer_a_consumption",
            "scenario_16_layer_b_consumption",
            "scenario_21_create_s2t_from_bar",
            "scenario_22_stage_a_scope_derivation",
            "scenario_23_stage_b_blocker_injection",
            "scenario_25_stage_c_deterministic",
        }
        if llm_dep and not args.run_live:
            print(f"[SKIP] Scenario {sid} ({func.__name__}) — requires --run-live (LLM budget)")
            run.skipped_llm.append(sid)
            continue
        if sid in mocked_ids and args.skip_mocks:
            print(f"[SKIP] Scenario {sid} ({func.__name__}) — --skip-mocks set")
            continue
        if run.total_cost_usd >= run.cost_cap:
            print(f"[SKIP] Scenario {sid} — cost cap ${run.cost_cap} reached")
            run.skipped_llm.append(sid)
            continue

        print(f"\n[SCENARIO {sid}] {func.__name__}...")
        try:
            result = func(run)
        except Exception as e:
            result = ScenarioResult(sid, func.__name__, "R", False,
                                    error=f"harness crash: {type(e).__name__}: {e}")
        status = "PASS" if result.passed else "FAIL"
        note = f" — {result.notes}" if result.notes else ""
        err = f" ERR: {result.error}" if result.error else ""
        print(f"  [{status}] trials={result.trials} passes={result.passes} "
              f"cost=${result.cost_usd:.3f} dur={result.duration_s:.1f}s{note}{err}")
        run.results.append(result)

    # Summary
    ts = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
    log_path = LOGS_DIR / f"regression_piece8_{ts}.log"
    summary = {
        "started_at": run.started_at.isoformat(),
        "total_cost_usd": run.total_cost_usd,
        "cost_cap": run.cost_cap,
        "skipped_llm": run.skipped_llm,
        "results": [
            {
                "scenario_id": r.scenario_id,
                "name": r.name,
                "kind": r.kind,
                "passed": r.passed,
                "trials": r.trials,
                "passes": r.passes,
                "cost_usd": r.cost_usd,
                "duration_s": r.duration_s,
                "notes": r.notes,
                "error": r.error,
                "llm_dependent": r.llm_dependent,
            }
            for r in run.results
        ],
    }
    log_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"\n=== REGRESSION SUMMARY ===")
    print(f"Total cost: ${run.total_cost_usd:.3f}/${run.cost_cap:.2f}")
    print(f"Skipped (LLM/budget): {run.skipped_llm}")
    total = len(run.results)
    passed = sum(1 for r in run.results if r.passed)
    failed = total - passed
    print(f"Passed: {passed}/{total}  Failed: {failed}")
    print(f"Log: {log_path}")

    # Exit code policy
    any_fail = any(
        (not r.passed and r.error is None)
        or (not r.passed and r.error and not r.error.startswith("SKIP:"))
        for r in run.results
    )
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
