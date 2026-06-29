"""Refresh active `domain_facts` rows against current DuckDB state.

For each active fact:

  1. Execute `evidence_sql` against the live warehouse.
  2. Compare the new result set to the stored `evidence_result_json`.
  3. Classify the drift:

        NO DRIFT       — identical records → update `evidence_refreshed_at` only.
        NOISE DRIFT    — same structure, same set of primary-key values,
                         dominant-row pct within 5 percentage points of prior
                         → regenerate `fact_technical` with a focused
                         LLM prompt (~400 tokens). Keep `fact_plain`,
                         `category`, `priority_score` unchanged.
        MATERIAL DRIFT — a new value appeared in the primary grouping column
                         OR the dominant-row pct shifted more than 5pp
                         → mark the old fact `status='superseded'`,
                         `superseded_by=NEW_ID`, and create a brand-new fact
                         via `interpret_domain_fact()` (~1100 tokens).
        UNPARSEABLE    — cannot parse prior JSON / schema changed → log
                         a warning, don't touch the fact, flag for manual review.

  4. After all facts processed, write the updated CSV back, then
     dbt-seed + parquet-export `domain_facts` so the cached views pick
     the new rows up (Rule 27/30 machinery handles the rest).
  5. Print a summary with counts per drift bucket + total tokens used.

CLI:  python scripts/refresh_domain_facts.py
Also exposed from end_of_task.py via `--refresh-domain-facts`.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "dbt" / "seeds" / "domain_facts.csv"
DB = ROOT / "cpe_analytics.duckdb"

# Load .env so the LLM helper (imported via claude_api) finds the API key.
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(ROOT / "app"))
# Streamlit shim — the refresh runs as a plain subprocess.
import types  # noqa: E402
sys.modules.setdefault("streamlit", types.ModuleType("streamlit"))
from claude_api import interpret_domain_fact, _post_claude  # noqa: E402

FIELDS = [
    "fact_id", "category", "scope_layer", "scope_tables",
    "fact_plain", "fact_technical", "evidence_sql",
    "evidence_result_json", "evidence_result_summary",
    "evidence_refreshed_at", "discovered_at", "discovered_by",
    "confidence", "stale_after_days", "auto_inject",
    "priority_score", "status", "superseded_by",
]

MATERIAL_PCT_SHIFT = 5.0  # percentage-point threshold


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_records(text: str):
    """Parse `evidence_result_json` into a list of dict records.

    Returns None on any parse / shape failure so the caller can flag the
    fact for manual review.
    """
    if not text:
        return []
    try:
        val = json.loads(text)
    except Exception:
        return None
    if isinstance(val, list) and all(isinstance(r, dict) for r in val):
        return val
    return None


def _primary_group_col(records: list) -> "str | None":
    """Heuristic: the first column whose values are all non-numeric strings
    is the grouping column. Fall back to the first column."""
    if not records:
        return None
    cols = list(records[0].keys())
    for c in cols:
        sample = [r.get(c) for r in records[:5]]
        if all(isinstance(v, str) and not v.replace(".", "", 1).replace("-", "", 1).isdigit() for v in sample if v is not None):
            return c
    return cols[0] if cols else None


def _dominant_pct(records: list) -> "float | None":
    """If the records have a column named `pct` / `percentage` / `percent` / `share`,
    return its first-row value. Else return None."""
    if not records:
        return None
    for c in records[0]:
        if str(c).lower() in {"pct", "percentage", "percent", "share"}:
            try:
                return float(records[0][c])
            except Exception:
                return None
    return None


def classify_drift(old_json: str, new_records: list) -> tuple[str, str]:
    """Return (bucket, detail). bucket in {'no_drift','noise','material','unparseable'}."""
    old = _as_records(old_json or "[]")
    if old is None:
        return "unparseable", "prior evidence_result_json did not parse"
    if not new_records and not old:
        return "no_drift", "both empty"

    # Compare rendered JSON first — cheapest no-drift check.
    try:
        new_norm = json.dumps(new_records, sort_keys=True, default=str)
        old_norm = json.dumps(old, sort_keys=True, default=str)
        if new_norm == old_norm:
            return "no_drift", "identical records"
    except Exception:
        pass

    gcol = _primary_group_col(old) or _primary_group_col(new_records) or ""
    if gcol:
        old_set = {r.get(gcol) for r in old}
        new_set = {r.get(gcol) for r in new_records}
        appeared = new_set - old_set
        if appeared:
            return "material", f"new {gcol}={sorted(map(str, appeared))}"

    old_top = _dominant_pct(old)
    new_top = _dominant_pct(new_records)
    if old_top is not None and new_top is not None:
        shift = abs(new_top - old_top)
        if shift > MATERIAL_PCT_SHIFT:
            return "material", f"dominant pct shifted {shift:.2f}pp (from {old_top} to {new_top})"
        return "noise", f"pct shift {shift:.2f}pp within tolerance"

    # Same set of groups, no pct column to threshold — call it noise so
    # fact_technical numbers get refreshed without the full supersede cost.
    return "noise", "structure stable, numeric delta below material bar"


NOISE_SYSTEM_PROMPT = """You are updating a domain_fact's technical description after a small (non-material) data refresh.

You receive:
  - the fact_plain (keep as-is; do not return it)
  - the old fact_technical (keep the shape; update the exact figures)
  - the old evidence_result_json
  - the new evidence_result_json

Rewrite ONLY fact_technical so the numbers match the new evidence. Keep every column-name reference, stay within one short paragraph, preserve the project's technical register (include SAP tables/fields, exact counts, exact percentages).

RESPOND ONLY IN JSON: {"fact_technical": "..."}"""


def regenerate_fact_technical(api_key_required: bool, old_plain, old_tech, old_json, new_json) -> "str | None":
    """Focused LLM call for NOISE drift. Returns new fact_technical text
    or None on LLM error."""
    user = (
        f"fact_plain (unchanged, for context):\n{old_plain}\n\n"
        f"old fact_technical:\n{old_tech}\n\n"
        f"old evidence_result_json (head):\n{(old_json or '')[:800]}\n\n"
        f"new evidence_result_json (head):\n{(new_json or '')[:800]}\n"
    )
    resp = _post_claude(NOISE_SYSTEM_PROMPT, user, max_tokens=400)
    if isinstance(resp, dict) and "error" not in resp:
        return str(resp.get("fact_technical") or "").strip() or None
    return None


def _next_fact_id(existing_ids: list) -> str:
    nums = [int(x.split("-")[-1]) for x in existing_ids if x and x.startswith("DF-")]
    n = max(nums) if nums else 0
    return f"DF-{n + 1:04d}"


def main() -> int:
    if not SEED.exists():
        print("domain_facts.csv missing — nothing to refresh.")
        return 0

    df = pd.read_csv(SEED)
    if df.empty:
        print("No domain facts on disk. Nothing to refresh.")
        return 0

    active_mask = df["status"].astype(str).str.lower().eq("active")
    active_ids = df.loc[active_mask, "fact_id"].tolist()
    if not active_ids:
        print("No active facts to refresh.")
        return 0

    print(f"Refreshing {len(active_ids)} active domain fact(s) against {DB.name}")
    counts = {"no_drift": 0, "noise": 0, "material": 0, "unparseable": 0, "error": 0}
    tokens_spent_rough = 0  # loose estimate; LLM responses themselves include usage info

    # Convert to list-of-dicts so we can append new rows and mutate the
    # original ones cleanly.
    rows = df.to_dict(orient="records")
    existing_ids = [str(r.get("fact_id") or "") for r in rows]

    conn = duckdb.connect(str(DB), read_only=True)
    try:
        for row in list(rows):
            fid = str(row.get("fact_id") or "")
            if str(row.get("status", "")).lower() != "active":
                continue
            sql = str(row.get("evidence_sql") or "").strip()
            if not sql:
                print(f"  {fid}: evidence_sql empty — skipping")
                continue
            try:
                new_df = conn.execute(sql).fetchdf()
            except Exception as e:
                print(f"  {fid}: ERROR running evidence_sql: {e}")
                counts["error"] += 1
                continue

            new_records = json.loads(new_df.head(50).to_json(orient="records"))
            new_json = json.dumps(new_records, default=str)
            bucket, detail = classify_drift(str(row.get("evidence_result_json") or ""), new_records)
            print(f"  {fid}: {bucket.upper()} — {detail}")
            counts[bucket] += 1

            if bucket == "no_drift":
                row["evidence_refreshed_at"] = _now_utc()
                row["evidence_result_json"] = new_json
                row["evidence_result_summary"] = f"{len(new_records)} rows (no drift)"
                continue

            if bucket == "unparseable":
                # Don't touch anything — operator needs to fix by hand.
                continue

            if bucket == "noise":
                new_tech = regenerate_fact_technical(
                    api_key_required=True,
                    old_plain=row.get("fact_plain"),
                    old_tech=row.get("fact_technical"),
                    old_json=row.get("evidence_result_json"),
                    new_json=new_json,
                )
                tokens_spent_rough += 500
                if new_tech:
                    row["fact_technical"] = new_tech
                row["evidence_result_json"] = new_json
                row["evidence_result_summary"] = f"{len(new_records)} rows (noise-drift refresh)"
                row["evidence_refreshed_at"] = _now_utc()
                continue

            # MATERIAL drift — supersede + mint new fact.
            interp = interpret_domain_fact(
                sql=sql,
                result_preview=new_json,
                focus_area=str(row.get("fact_plain") or "")[:200],
                domains="",
                scope_layer=str(row.get("scope_layer") or "staging"),
            )
            tokens_spent_rough += 1200

            if isinstance(interp, dict) and "error" in interp:
                print(f"    [warn] interpret LLM error: {interp.get('error')} — marking superseded anyway, new fact synthesised as low-confidence fallback")
                interp = {
                    "fact_plain": "[material drift detected — manual review needed]",
                    "fact_technical": new_json[:400],
                    "category": str(row.get("category") or "business_rule_observed"),
                    "scope_layer": str(row.get("scope_layer") or "staging"),
                    "scope_tables": str(row.get("scope_tables") or ""),
                    "confidence": "low",
                    "priority_score": int(row.get("priority_score") or 50),
                    "stale_after_days": row.get("stale_after_days") or "",
                }

            def _si(k, default=""):
                v = interp.get(k) if isinstance(interp, dict) else None
                return str(v).strip() if v is not None else default

            stale = _si("stale_after_days", "")
            if stale.lower() in ("null", "none"):
                stale = ""

            new_id = _next_fact_id(existing_ids)
            existing_ids.append(new_id)
            new_row = {
                "fact_id": new_id,
                "category": _si("category", str(row.get("category") or "business_rule_observed")),
                "scope_layer": _si("scope_layer", str(row.get("scope_layer") or "staging")),
                "scope_tables": _si("scope_tables", str(row.get("scope_tables") or "")),
                "fact_plain": _si("fact_plain"),
                "fact_technical": _si("fact_technical"),
                "evidence_sql": sql,
                "evidence_result_json": new_json,
                "evidence_result_summary": f"{len(new_records)} rows (material drift)",
                "evidence_refreshed_at": _now_utc(),
                "discovered_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "discovered_by": "drift_refresh",
                "confidence": _si("confidence", "medium"),
                "stale_after_days": stale,
                "auto_inject": str(row.get("auto_inject", "true")),
                "priority_score": _si("priority_score", str(row.get("priority_score") or "50")),
                "status": "active",
                "superseded_by": "",
            }
            # Old fact → superseded, linked.
            row["status"] = "superseded"
            row["superseded_by"] = new_id
            row["evidence_refreshed_at"] = _now_utc()
            rows.append(new_row)

    finally:
        conn.close()

    # Write the merged rows back. Drop any pandas NaN into "" so the CSV
    # doesn't leak a literal "nan" string that dbt would then refuse to
    # cast to the inferred column type.
    def _clean(v):
        if v is None:
            return ""
        try:
            if pd.isna(v):  # handles float NaN and pd.NA
                return ""
        except Exception:
            pass
        return v

    with SEED.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in rows:
            w.writerow({k: _clean(r.get(k, "")) for k in FIELDS})

    # Re-seed into DuckDB + refresh parquet so load_domain_context sees
    # the new state immediately (Rule 27 machinery handles the view refresh).
    print("\n--- Re-seeding + Parquet export ---")
    dbt_exe = str(Path(sys.executable).parent / ("dbt.EXE" if os.name == "nt" else "dbt"))
    result = subprocess.run(
        [dbt_exe, "seed", "--full-refresh", "--select", "domain_facts"],
        capture_output=True, text=True, cwd=str(ROOT / "dbt"), timeout=180,
    )
    if result.returncode != 0:
        print(f"  [warn] dbt seed rc={result.returncode}")
        print(f"  stdout tail: {result.stdout[-2000:]}")
        print(f"  stderr tail: {result.stderr[-1000:]}")
    sys.path.insert(0, str(ROOT / "scripts"))
    from export_parquet import export_table  # noqa: E402
    export_table("main_seeds", "domain_facts")

    print("\n=== Refresh summary ===")
    for k in ("no_drift", "noise", "material", "unparseable", "error"):
        print(f"  {k:>12}: {counts[k]}")
    print(f"  Rough LLM tokens spent (estimate): ~{tokens_spent_rough:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
