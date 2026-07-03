"""End-of-task commit gate for CPE Procurement Analytics.

Checks that knowledge graph was updated before allowing commit.
Run after every task: python scripts/end_of_task.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _append_run_log(start_time: float, status: dict, warns: int,
                    blocking: int, verdict: str) -> None:
    """Append one pipe-separated line to logs/end_of_task.log for each run."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _sidecar import current_git_head_sha, now_iso_utc
    duration = time.time() - start_time
    line = (
        f"{now_iso_utc()} | {current_git_head_sha()} | "
        f"wiki={status['wiki']} | context={status['context']} | "
        f"parquet={status['parquet']} | seeds={status['seeds']} | "
        f"warns={warns} | blocking={blocking} | verdict={verdict} | "
        f"duration={duration:.1f}s\n"
    )
    log_path = ROOT / "logs" / "end_of_task.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="") as f:
        f.write(line)


def get_stale_seeds() -> list[str]:
    """Detect seed CSVs whose mtime is newer than their last DuckDB load.

    DuckDB does not expose per-table load timestamps (neither
    duckdb_tables() nor information_schema.tables has one). This helper
    uses the parquet export mtime as a proxy: export_parquet.py runs on
    every end_of_task invocation and writes
    data/parquet/main_seeds/<seed>.parquet, so the parquet mtime
    approximates "last time this seed was coherent with DuckDB".

    Returns: list of dbt/seeds/*.csv paths (repo-relative, forward
    slashes) that appear stale. Empty list when all seeds are in sync.

    Edge cases handled:
    - cpe_analytics.duckdb does not exist → [] (first-run scenario).
    - CSV exists but parquet does not → CSV treated as stale (seed added
      to repo but never exported).
    - Dotfile caches (e.g. .s2t_plain_cache.json) → skipped.
    - Known false-positive mode: a CSV re-written with identical content
      has a fresh mtime even though downstream is already current. The
      cost is a harmless re-seed; correctness is preserved.

    Fixes known_issue #21 by making post-commit-pre-seed drift visible to
    get_changed_files().
    """
    db_path = ROOT / "cpe_analytics.duckdb"
    if not db_path.exists():
        return []

    seeds_dir = ROOT / "dbt" / "seeds"
    parquet_dir = ROOT / "data" / "parquet" / "main_seeds"

    stale: list[str] = []
    for csv_path in sorted(seeds_dir.glob("*.csv")):
        if csv_path.name.startswith("."):
            continue  # dotfile caches (.s2t_plain_cache.json etc.)
        parquet_path = parquet_dir / f"{csv_path.stem}.parquet"
        rel = f"dbt/seeds/{csv_path.name}"
        if not parquet_path.exists():
            stale.append(rel)
            continue
        if csv_path.stat().st_mtime > parquet_path.stat().st_mtime:
            stale.append(rel)
    return stale


def get_changed_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True, text=True, cwd=ROOT,
    )
    staged = result.stdout.strip().split("\n") if result.stdout.strip() else []
    result2 = subprocess.run(
        ["git", "diff", "--name-only"],
        capture_output=True, text=True, cwd=ROOT,
    )
    unstaged = result2.stdout.strip().split("\n") if result2.stdout.strip() else []
    stale_seeds = get_stale_seeds()
    return sorted(set(staged + unstaged + stale_seeds))


def main() -> None:
    _start = time.time()
    # step_status tracks per-artifact success/error for the run log.
    # Simplified to ok/error per step-3 spec; staleness detection (step 4)
    # will later swap "ok" → "stale" when sidecar comparison flags drift.
    step_status = {"wiki": "ok", "context": "ok", "parquet": "ok", "seeds": "ok"}

    # Optional step: re-evaluate every active domain_fact.
    # Runs BEFORE the normal pipeline so any drift-generated CSV changes
    # flow through the subsequent scanner + export steps.
    if "--refresh-domain-facts" in sys.argv:
        print("--- Refreshing domain facts (drift check) ---")
        try:
            from refresh_domain_facts import main as refresh_main
            refresh_main()
        except Exception as e:
            print(f"  [warn] refresh_domain_facts failed: {e}")

    changed = get_changed_files()
    if not changed:
        print("No changed files. Nothing to check.")
        _append_run_log(_start, step_status, 0, 0, "OK")
        return

    warnings = []
    blocking = []

    # Check if dbt model or seed files changed
    model_changes = [f for f in changed if f.startswith("dbt/models/")]
    seed_changes = [f for f in changed if f.startswith("dbt/seeds/")]

    if model_changes and "dbt/seeds/known_decisions.csv" not in changed:
        blocking.append(
            f"dbt models changed ({len(model_changes)} files) but known_decisions.csv not updated. "
            "Record what you changed and why."
        )

    # RULE 3 enforcement — mart/obt/knowledge models must ref() vault
    # only, not staging or each other; no direct raw_sap.* SQL refs.
    # Allowlist in scripts/check_rule3_layer_violations.py keeps known
    # violators (each tied to an open KI) from blocking forever; any
    # NEW violation hard-fails the gate. Only checks when models change.
    if model_changes:
        print("\n--- Checking RULE 3 (layer-skip violations) ---")
        try:
            rule3 = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent
                    / "check_rule3_layer_violations.py")],
                capture_output=True, text=True, timeout=30,
                cwd=str(ROOT),
            )
            print(rule3.stdout, end="")
            if rule3.returncode != 0:
                blocking.append(
                    "RULE 3 layer-skip violations detected. "
                    "Mart/obt/knowledge models must ref() vault only. "
                    "Run `python scripts/check_rule3_layer_violations.py` "
                    "for details."
                )
        except Exception as e:
            warnings.append(f"RULE 3 check failed to run: {e}")

    # Auto-scan dbt models whenever any model file or related seed changed
    if model_changes or seed_changes:
        print("\n--- Scanning dbt models ---")
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from scan_dbt_models import scan_all_models
            scan_all_models()
        except Exception as e:
            warnings.append(f"dbt model scan failed: {e}")

        # Extract model-to-model relationships from ref() calls and
        # column lineage so the Data Model ERD page never misses a
        # star-schema edge again. Runs after scan_dbt_models because
        # it reads the fresh dbt_column_lineage seed.
        print("\n--- Extracting dbt relationships ---")
        try:
            from extract_dbt_relationships import main as extract_rels
            extract_rels()
        except Exception as e:
            warnings.append(f"dbt relationship extraction failed: {e}")

        # Keep s2t_mapping.transformation_logic_sql aligned with the
        # dbt expressions just scanned. dbt code is the single source of
        # truth; s2t_mapping is documentation that should never drift
        # from it — see commit a47e3fa / beb36bc for the incident that
        # triggered this gate.
        print("\n--- Syncing S2T with dbt ---")
        try:
            from sync_s2t_from_dbt import main as sync_s2t
            sync_s2t()
        except Exception as e:
            warnings.append(f"S2T sync failed: {e}")

        # After the SQL is synced, regenerate the plain-English
        # descriptions from the fresh SQL via Claude. Runs second so
        # the LLM always sees the current dbt expression, never stale
        # hand-written SQL. Requires ANTHROPIC_API_KEY; silently skips
        # if it isn't set.
        print("\n--- Syncing S2T plain descriptions ---")
        try:
            from sync_s2t_plain_from_dbt import main as sync_plain
            sync_plain()
        except Exception as e:
            warnings.append(f"S2T plain sync failed: {e}")

    # Seed scanner outputs into DuckDB so the Parquet export picks up
    # the fresh column lineage, model catalog, and relationship data.
    # Without this step, export_parquet reads stale DuckDB tables and
    # Streamlit shows "No column lineage recorded" for newly deployed models.
    if model_changes or seed_changes:
        # RULE 34 layer 3: normalise any CRLF line endings in dbt/seeds/*.csv
        # before invoking dbt seed. DuckDB's CSV sniffer fails with a cryptic
        # "sniffer: 0 columns" error on mixed CRLF/LF — decision #7 + #51.
        # .gitattributes + the 15 correct writers handle 99% of cases; this
        # hook catches the remaining 1% (Path.write_text regressions, manual
        # edits, external tooling).
        print("\n--- Normalising seed CSV line endings (RULE 34) ---")
        _seeds_dir = ROOT / "dbt" / "seeds"
        _normalised = 0
        for _csv in sorted(_seeds_dir.glob("*.csv")):
            try:
                _raw = _csv.read_bytes()
                if b"\r\n" in _raw:
                    _csv.write_bytes(_raw.replace(b"\r\n", b"\n"))
                    _normalised += 1
                    print(f"  normalised: {_csv.name}")
            except Exception as _e:
                warnings.append(f"seed LF normalise failed for {_csv.name}: {_e}")
        if _normalised == 0:
            print("  all seed CSVs already LF-only")
        else:
            print(f"  {_normalised} seed(s) normalised CRLF -> LF")

        print("\n--- Seeding scanner outputs into DuckDB ---")
        scanner_seeds = [
            "dbt_column_lineage",
            "dbt_model_catalog",
            "dbt_model_relationships",
            "s2t_mapping",
            "domain_facts",
            "known_issues",
            "known_decisions",
            "domain_reports",
            "ingestion_log",
            "archive_log",
            "business_glossary",
            "source_column_roles",
            "source_column_role_changes",
        ]
        try:
            dbt_exe = str(Path(sys.executable).parent / "dbt.EXE")
            dbt_dir = ROOT / "dbt"
            result = subprocess.run(
                [dbt_exe, "seed", "--full-refresh", "--select"] + scanner_seeds,
                capture_output=True, text=True, timeout=120,
                cwd=str(dbt_dir),
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "Done." in line:
                        print(f"  {line.strip()}")
            else:
                warnings.append(f"Scanner seed failed (rc={result.returncode}): {result.stderr[:300]}")
                step_status["seeds"] = "error"
        except Exception as e:
            warnings.append(f"Scanner seed failed: {e}")
            step_status["seeds"] = "error"

        # Refresh the dbt compiled-SQL cache so the Business Glossary view
        # always renders the latest SQL for every deployed model.
        # Without this, `dbt run --select <new_model>` from Stage E only
        # writes the new model's compiled SQL; upstream stagings/vaults
        # stay un-cached until someone runs `dbt compile` manually, and
        # the dashboard shows "compiled sql not in dbt cache" (KI-117).
        # ~5-10s on this project; best-effort — DuckDB write lock from
        # DBeaver/etc. surfaces as a warning, not a blocking failure.
        print("\n--- Compiling dbt models (refresh compiled SQL cache) ---")
        try:
            _t0 = time.time()
            result = subprocess.run(
                [dbt_exe, "compile"],
                capture_output=True, text=True, timeout=180,
                cwd=str(dbt_dir),
            )
            _dt = time.time() - _t0
            if result.returncode == 0:
                _found = next(
                    (l.strip() for l in result.stdout.splitlines()
                     if l.strip().startswith("Found")),
                    "compile completed",
                )
                print(f"  {_found} ({_dt:.1f}s)")
            else:
                # Surface the real cause from stdout tail (dbt prints the
                # IOException there, not stderr).
                _tail = "\n".join(
                    l for l in result.stdout.splitlines()[-10:] if l.strip()
                )[-400:] or result.stderr[:300]
                _is_lock = "already open" in result.stdout or "being used by another process" in result.stdout
                _hint = (
                    " (close DBeaver/external DuckDB clients to unlock)"
                    if _is_lock else ""
                )
                warnings.append(
                    f"dbt compile failed (rc={result.returncode}){_hint}; "
                    f"compiled-SQL cache may be stale, deployed models "
                    f"unaffected. tail: {_tail}"
                )
        except Exception as e:
            warnings.append(f"dbt compile failed: {e}")

        # Generate dbt docs so the native column-level lineage viz at
        # dbt/target/index.html is always fresh. Complements the
        # Streamlit Seeds_Catalog page (grep-based Python-side lineage):
        # `dbt docs serve` provides the DAG graph + column descriptions
        # from schema.yml for everything dbt-tracked. Best-effort like
        # compile — DBeaver lock surfaces as a warning, not a block.
        print("\n--- Generating dbt docs ---")
        try:
            _t0 = time.time()
            result = subprocess.run(
                [dbt_exe, "docs", "generate"],
                capture_output=True, text=True, timeout=180,
                cwd=str(dbt_dir),
            )
            _dt = time.time() - _t0
            if result.returncode == 0:
                print(
                    f"  dbt docs generated ({_dt:.1f}s); "
                    f"serve with: dbt docs serve --port 8080"
                )
            else:
                _tail = "\n".join(
                    l for l in result.stdout.splitlines()[-10:] if l.strip()
                )[-400:] or result.stderr[:300]
                _is_lock = (
                    "already open" in result.stdout
                    or "being used by another process" in result.stdout
                )
                _hint = (
                    " (close DBeaver/external DuckDB clients to unlock)"
                    if _is_lock else ""
                )
                warnings.append(
                    f"dbt docs generate failed (rc={result.returncode}){_hint}; "
                    f"docs index.html may be stale. tail: {_tail}"
                )
        except Exception as e:
            warnings.append(f"dbt docs generate failed: {e}")

    # Always refresh the Parquet export so the Streamlit dashboard sees
    # whatever the current DuckDB file contains. Cheap enough (~10s) to
    # run every time end_of_task fires.
    print("\n--- Exporting to Parquet ---")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from export_parquet import export_all
        export_all()
    except Exception as e:
        warnings.append(f"Parquet export failed: {e}")
        step_status["parquet"] = "error"

    # Per-artifact staleness checks (fixes #20). Replaces the former blanket
    # "Seeds changed" / "Remember to regenerate context" warnings which fired
    # on git-dirty state regardless of whether downstream was actually stale.
    # Staleness is decided by comparing sidecar content hashes, not mtimes.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _sidecar import check_artifact_staleness

    _rebuild_cmd = {
        "wiki": "python scripts/build_knowledge_wiki.py",
        "context": "python scripts/export_context.py",
        "parquet": "python scripts/export_parquet.py",  # informational; runs unconditionally above
    }
    staleness_warnings_count = 0
    for _art in ("wiki", "context", "parquet"):
        _res = check_artifact_staleness(_art)
        if _res["is_stale"]:
            # Only overwrite "ok" — preserve "error" from upstream failures.
            if step_status[_art] == "ok":
                step_status[_art] = "stale"
            staleness_warnings_count += 1
            warnings.append(
                f"{_art} stale: {_res['reason']} — run: {_rebuild_cmd[_art]}"
            )

    # Also surface seed CSV / DuckDB mtime drift (from step 2's get_stale_seeds)
    _stale_csvs = get_stale_seeds()
    if _stale_csvs:
        if step_status["seeds"] == "ok":
            step_status["seeds"] = "stale"
        staleness_warnings_count += 1
        _displayed = _stale_csvs[:5]
        _tail = f" (+{len(_stale_csvs) - 5} more)" if len(_stale_csvs) > 5 else ""
        warnings.append(
            f"seeds stale: {len(_stale_csvs)} CSVs have mtime > parquet mtime: "
            f"{', '.join(_displayed)}{_tail}"
        )

    # Print results
    if blocking:
        print("\n[BLOCKING] cannot commit until resolved:")
        for b in blocking:
            print(f"  - {b}")
        print()

    if warnings:
        print("[WARNINGS]:")
        for w in warnings:
            print(f"  - {w}")
        print()

    verdict = "FAIL" if blocking else "OK"
    # warns in run log = staleness count only per step-4 spec;
    # non-staleness warnings surface via step_status=error.
    _append_run_log(_start, step_status, staleness_warnings_count, len(blocking), verdict)

    if blocking:
        sys.exit(1)
    elif warnings:
        print("OK to commit (with warnings above)")
    else:
        print("All checks passed. Ready to commit.")


if __name__ == "__main__":
    main()
