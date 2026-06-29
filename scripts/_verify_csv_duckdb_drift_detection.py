"""Live reproduction of known_issue #21 — end_of_task.py's get_changed_files()
returns an empty list when a committed seed CSV outpaces DuckDB.

Safe redesign: reproduction does NOT touch any real seed. A scratch file
under dbt/seeds/_probe_drift_reproduction.csv is created, committed,
asserted against, then fully torn down.

Follows the V1/V2/V3 convention from scripts/dv_verify.py, with one
deliberate deviation: explicit exit codes.

    exit 0 → all 3 stages behaved as #21 predicts (bug present).
    exit 1 → any stage deviated OR cleanup could not run safely.

Safety discipline:
  * Only one path is ever touched: SCRATCH_PATH below. Every file mutation
    asserts this path equality before proceeding.
  * All commits this script creates use the TEMP_COMMIT_PREFIX subject.
    Any reset operation verifies the target commit's subject matches before
    resetting — refuses otherwise.
  * Every git mutation re-checks HEAD against the pre-flight-captured SHA
    before executing. Aborts if HEAD has moved out from under us.
  * Cleanup is nested per-stage via try/finally; a global sweep at the end
    reverses any residual mutation.
  * print() is used liberally during destructive operations so post-mortem
    forensics can trace cleanup failures.
"""

import csv
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SCRATCH_REL = "dbt/seeds/_probe_drift_reproduction.csv"
SCRATCH_PATH = ROOT / SCRATCH_REL
TEMP_COMMIT_PREFIX = "DRIFT-REPRODUCTION-TEMP-COMMIT-DO-NOT-KEEP"

sys.path.insert(0, str(ROOT / "scripts"))


# =====================================================================
# Helpers
# =====================================================================
def run_git(args, check=True):
    print(f"  $ git {' '.join(args)}")
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True, cwd=ROOT, check=check
    )


def head_sha():
    return run_git(["rev-parse", "HEAD"]).stdout.strip()


def head_subject():
    return run_git(["log", "-1", "--format=%s"]).stdout.strip()


def assert_scratch_path(p: Path):
    """Defensive guard: no operation in this script touches any other path."""
    if p != SCRATCH_PATH:
        raise RuntimeError(f"SAFETY VIOLATION: attempted to touch {p}, expected only {SCRATCH_PATH}")


def assert_head_matches(expected_sha: str):
    actual = head_sha()
    if actual != expected_sha:
        raise RuntimeError(
            f"SAFETY VIOLATION: HEAD moved from {expected_sha} to {actual} — "
            "another terminal may have committed. Aborting."
        )


def invoke_get_changed_files():
    """Fresh import each call to bypass any module cache."""
    import importlib
    if "end_of_task" in sys.modules:
        importlib.reload(sys.modules["end_of_task"])
    from end_of_task import get_changed_files
    return get_changed_files()


def write_scratch(content: str):
    assert_scratch_path(SCRATCH_PATH)
    with SCRATCH_PATH.open("w", encoding="utf-8", newline="") as f:
        f.write(content)


def touch_scratch_mtime():
    """Advance mtime without changing content."""
    assert_scratch_path(SCRATCH_PATH)
    os.utime(SCRATCH_PATH, None)


def remove_scratch():
    assert_scratch_path(SCRATCH_PATH)
    if SCRATCH_PATH.exists():
        SCRATCH_PATH.unlink()


def safe_reset_temp_commit(expected_parent_sha: str):
    """Reset HEAD only if HEAD's subject matches TEMP_COMMIT_PREFIX AND
    HEAD^ == expected_parent_sha. Refuses otherwise.

    Uses `--mixed` (default), NOT `--hard`. `--hard` would reset the entire
    working tree to HEAD^, destroying any other uncommitted changes the
    user had on unrelated paths (e.g. a dirty real seed they were editing).
    `--mixed` only moves HEAD and un-stages the index; working-tree files
    are left alone. The scratch file gets cleaned up separately by the
    caller via remove_scratch().
    """
    subj = head_subject()
    if not subj.startswith(TEMP_COMMIT_PREFIX):
        raise RuntimeError(
            f"SAFETY VIOLATION: refusing to reset — HEAD subject '{subj}' "
            f"does not start with '{TEMP_COMMIT_PREFIX}'"
        )
    parent = run_git(["rev-parse", "HEAD^"]).stdout.strip()
    if parent != expected_parent_sha:
        raise RuntimeError(
            f"SAFETY VIOLATION: refusing to reset — HEAD^ ({parent}) "
            f"!= expected parent ({expected_parent_sha})"
        )
    run_git(["reset", "--mixed", "HEAD^"])


# =====================================================================
# Pre-flight
# =====================================================================
print("=" * 72)
print("Pre-flight")
print("=" * 72)

BASELINE_SHA = head_sha()
print(f"  HEAD SHA                : {BASELINE_SHA}")

pre_status_scratch = run_git(
    ["status", "--porcelain", "--", SCRATCH_REL]
).stdout.strip()
if pre_status_scratch:
    print(f"  ABORT: git sees scratch path '{SCRATCH_REL}' already in index/tree:")
    print(f"    {pre_status_scratch}")
    print("  Prior run did not clean up. Inspect manually — do not re-run blindly.")
    sys.exit(1)

if SCRATCH_PATH.exists():
    print(f"  ABORT: scratch file {SCRATCH_PATH} exists on disk but git doesn't track it.")
    print("  Prior run left a file behind. Delete manually after inspection.")
    sys.exit(1)

ls_scratch = run_git(["ls-files", SCRATCH_REL]).stdout.strip()
if ls_scratch:
    print(f"  ABORT: scratch path {SCRATCH_REL} is tracked by git. Unexpected.")
    sys.exit(1)

print(f"  scratch path clean      : {SCRATCH_REL} does not exist, not tracked")
print(f"  pre-flight OK")

results = {"V1": "SKIP", "V2": "SKIP", "V3": "SKIP"}
stage_mutations = {"V2_commit_added": False, "V3_file_present": False}


# =====================================================================
# V1: baseline — scratch file does not exist, get_changed_files doesn't mention it
# =====================================================================
print()
print("=" * 72)
print("V1: baseline — scratch file absent, get_changed_files() ignores it")
print("=" * 72)

v1_gcf = invoke_get_changed_files()
v1_scratch_in_gcf = SCRATCH_REL in v1_gcf
print(f"  get_changed_files()                 : {v1_gcf!r}")
print(f"  scratch path present in result      : {v1_scratch_in_gcf}")

if not v1_scratch_in_gcf:
    results["V1"] = "PASS"
    print("  -> V1 PASS (baseline holds, scratch file is invisible to gate)")
else:
    results["V1"] = "FAIL"
    print("  -> V1 FAIL (unexpected — scratch mentioned before we created it)")


# =====================================================================
# V2: the bug — create + commit scratch file, DO NOT seed DuckDB, expect gcf=[]
# =====================================================================
print()
print("=" * 72)
print("V2: the bug — commit scratch CSV; expect get_changed_files() misses it")
print("=" * 72)

if results["V1"] != "PASS":
    print("  -> V2 SKIP (V1 did not pass)")
else:
    try:
        assert_head_matches(BASELINE_SHA)

        # Create valid CSV content. Tiny — this is not a real seed.
        content = (
            "id,title,description,status,priority,created_date,resolved_date,resolution\n"
            "PROBE-001,scratch-file-for-issue-21-reproduction,ignore,open,low,2026-04-19,,\n"
        )
        write_scratch(content)
        print(f"  wrote {SCRATCH_REL} (2 lines: header + 1 row)")

        # Sanity check — scratch file is now dirty/untracked in git's eyes.
        pre_commit_status = run_git(
            ["status", "--porcelain", "--", SCRATCH_REL]
        ).stdout.strip()
        print(f"  git status (pre-commit) : {pre_commit_status}")

        # Commit.
        run_git(["add", SCRATCH_REL])
        commit_msg = f"{TEMP_COMMIT_PREFIX}: V2 probe for issue #21"
        run_git(["commit", "-m", commit_msg])
        stage_mutations["V2_commit_added"] = True
        v2_post_commit_sha = head_sha()
        print(f"  post-commit HEAD        : {v2_post_commit_sha}")
        print(f"  post-commit subject     : {head_subject()}")

        # Now git is clean for the scratch path. Touch mtime to simulate
        # "hey, this file had a write since the last downstream propagation".
        touch_scratch_mtime()

        post_touch_status = run_git(
            ["status", "--porcelain", "--", SCRATCH_REL]
        ).stdout.strip()
        tree_clean_for_scratch = post_touch_status == ""
        print(f"  git status (post-touch) : '{post_touch_status}' (clean={tree_clean_for_scratch})")

        # Invoke — expect the gate to miss the drift.
        v2_gcf = invoke_get_changed_files()
        gcf_missed_scratch = SCRATCH_REL not in v2_gcf
        print(f"  get_changed_files()     : {v2_gcf!r}")
        print(f"  scratch NOT in result   : {gcf_missed_scratch}")

        if tree_clean_for_scratch and gcf_missed_scratch:
            results["V2"] = "PASS"
            print("  -> V2 PASS (#21 confirmed: scratch committed, gate returned empty for it)")
        else:
            results["V2"] = "FAIL"
            print("  -> V2 FAIL (expected silent miss did not occur)")
    finally:
        print("  --- V2 cleanup ---")
        try:
            if stage_mutations["V2_commit_added"]:
                safe_reset_temp_commit(BASELINE_SHA)
                stage_mutations["V2_commit_added"] = False
                print(f"  cleanup: mixed-reset to baseline {BASELINE_SHA}")
                # --mixed leaves working-tree files alone, so the scratch
                # file is still on disk (now untracked). Remove it.
                if SCRATCH_PATH.exists():
                    remove_scratch()
                    print(f"  cleanup: scratch file removed from disk")
            else:
                # Commit never happened; file may or may not exist.
                if SCRATCH_PATH.exists():
                    remove_scratch()
                    print(f"  cleanup: removed orphan scratch file")
        except Exception as e:
            print(f"  CLEANUP ERROR: {e}")
            print("  Manual recovery may be required. See state dump below.")
            results["V2"] = "FAIL"


# =====================================================================
# V3: inverse — create scratch file, do NOT commit, expect gcf includes it
# =====================================================================
print()
print("=" * 72)
print("V3: inverse — uncommitted scratch file, get_changed_files() catches it")
print("=" * 72)

# Only proceed if V1 passed AND we're back at baseline after V2.
if results["V1"] != "PASS":
    print("  -> V3 SKIP (V1 did not pass)")
elif head_sha() != BASELINE_SHA:
    print(f"  -> V3 SKIP (HEAD {head_sha()} != baseline {BASELINE_SHA})")
elif SCRATCH_PATH.exists():
    print(f"  -> V3 SKIP (scratch file from V2 still on disk — cleanup incomplete)")
else:
    try:
        assert_head_matches(BASELINE_SHA)

        content = (
            "id,title\n"
            "PROBE-V3,uncommitted-scratch-for-dirty-tree-detection\n"
        )
        write_scratch(content)
        stage_mutations["V3_file_present"] = True
        print(f"  wrote {SCRATCH_REL} (untracked)")

        v3_status = run_git(
            ["status", "--porcelain", "--", SCRATCH_REL]
        ).stdout.strip()
        print(f"  git status              : '{v3_status}'")

        v3_gcf = invoke_get_changed_files()
        # Note: untracked files don't show up in `git diff` — only modified
        # (tracked) files do. get_changed_files() only uses `git diff`, so
        # an UNTRACKED scratch file won't appear in its output. That's a
        # known limitation of the heuristic, distinct from the #21 bug.
        # For V3's "dirty tree detection works as designed" assertion we
        # need the file to be tracked-and-modified. Let's `git add` without
        # committing so it's staged; `git diff --cached --name-only` will
        # pick it up.
        run_git(["add", SCRATCH_REL])
        v3_staged_status = run_git(
            ["status", "--porcelain", "--", SCRATCH_REL]
        ).stdout.strip()
        v3_gcf_staged = invoke_get_changed_files()
        print(f"  git status (staged)     : '{v3_staged_status}'")
        print(f"  get_changed_files()     : {v3_gcf_staged!r}")
        scratch_in_gcf = SCRATCH_REL in v3_gcf_staged
        print(f"  scratch IN result       : {scratch_in_gcf}")

        if scratch_in_gcf:
            results["V3"] = "PASS"
            print("  -> V3 PASS (staged change detected)")
        else:
            results["V3"] = "FAIL"
            print("  -> V3 FAIL (staged change was not reported)")
    finally:
        print("  --- V3 cleanup ---")
        try:
            # Unstage if staged
            run_git(["reset", "HEAD", "--", SCRATCH_REL], check=False)
            # Remove file from disk
            if SCRATCH_PATH.exists():
                remove_scratch()
                stage_mutations["V3_file_present"] = False
                print(f"  cleanup: scratch file removed and unstaged")
        except Exception as e:
            print(f"  CLEANUP ERROR: {e}")
            results["V3"] = "FAIL"


# =====================================================================
# Global-sweep cleanup (belt-and-suspenders)
# =====================================================================
print()
print("=" * 72)
print("Global-sweep cleanup")
print("=" * 72)

try:
    # If a temp commit is still sitting on HEAD, reset it.
    if head_subject().startswith(TEMP_COMMIT_PREFIX):
        print(f"  WARNING: HEAD still a temp commit — resetting")
        safe_reset_temp_commit(BASELINE_SHA)

    # If scratch file is staged, unstage it.
    staged = run_git(["diff", "--cached", "--name-only"]).stdout.strip().splitlines()
    if SCRATCH_REL in staged:
        print(f"  WARNING: scratch still staged — unstaging")
        run_git(["reset", "HEAD", "--", SCRATCH_REL], check=False)

    # If scratch file still exists on disk, remove it.
    if SCRATCH_PATH.exists():
        print(f"  WARNING: scratch still on disk — removing")
        remove_scratch()

    print("  global-sweep OK")
except Exception as e:
    print(f"  GLOBAL-SWEEP ERROR: {e}")


# =====================================================================
# Final state
# =====================================================================
print()
print("=" * 72)
print("Final state")
print("=" * 72)

final_sha = head_sha()
final_status = run_git(["status", "--short"]).stdout.rstrip()
print(f"  HEAD SHA: {final_sha} (baseline: {BASELINE_SHA})")
print(f"  scratch file exists: {SCRATCH_PATH.exists()}")
print(f"  git status --short:")
for line in final_status.splitlines() or ["    <clean>"]:
    print(f"    {line}")

state_intact = (
    final_sha == BASELINE_SHA
    and not SCRATCH_PATH.exists()
)


# =====================================================================
# Verdict
# =====================================================================
print()
print("=" * 72)
print("Verdict")
print("=" * 72)
for k, v in results.items():
    print(f"  {k}: {v}")
print(f"  state intact post-run: {state_intact}")

all_pass = all(v == "PASS" for v in results.values())
if all_pass and state_intact:
    print()
    print("ALL PASS — issue #21 reproduced as described. Bug still present.")
    sys.exit(0)
else:
    print()
    print("FAILURES PRESENT — review output; bug may be fixed, diagnosis wrong, or cleanup gap.")
    sys.exit(1)
