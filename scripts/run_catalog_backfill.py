"""sap_data_dictionary full-coverage backfill.

Generates the ~269 missing (table, field) rows via LLM-per-table
with prompt caching. Preserves 57 existing rows untouched (tagged
description_source='existing', needs_review=0 — set by Part 1 schema
migration before this script runs).

known_issue #53 note: deliberately NOT wired to sync_parquet_and_invalidate.
This is a one-shot utility (rarely re-run after the initial backfill); if
run while Streamlit is open, manually restart the dashboard OR run
`python scripts/export_parquet.py` afterward to refresh the view catalog.

Design:
- Per-table LLM batch (41 tables = 41 API calls).
- System prompt + directives cached via 1h ephemeral breakpoint;
  per-table user content uncached. Cache hit on calls 2-41 cuts cost.
- Per-table JSON response cached to dbt/seeds/.catalog_backfill_cache/
  so re-runs skip paid API calls.
- Defensive validation: reject LLM rows that (a) regenerate existing
  ground truth, (b) use invalid description_source, (c) are missing
  required fields, (d) don't match an expected field_name, (e) duplicate.
- Merge + atomic CSV replace (LF-only, QUOTE_ALL) preserving all 57
  existing rows.

CLI:
  python scripts/run_catalog_backfill.py                 # full backfill
  python scripts/run_catalog_backfill.py --dry-run       # inspect plan, no LLM
  python scripts/run_catalog_backfill.py --table ekbe    # single table
  python scripts/run_catalog_backfill.py --no-cache      # bypass JSON cache

Exit codes:
  0 — success (all tables processed, CSV rewritten, dbt seed loaded)
  1 — LLM / parse / validation failure on one or more tables
  2 — CSV write or dbt seed failure
  3 — precondition failure (missing env, malformed prompt, schema drift)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import duckdb
import requests

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "cpe_analytics.duckdb"
_ENV = _ROOT / ".env"
_PROMPT = _ROOT / "scripts" / "prompts" / "sap_data_dictionary_backfill_prompt.md"
_CSV = _ROOT / "dbt" / "seeds" / "sap_data_dictionary.csv"
_CACHE_DIR = _ROOT / "dbt" / "seeds" / ".catalog_backfill_cache"

_API_URL = "https://api.anthropic.com/v1/messages"
from _model_config import MODEL as _MODEL  # single source of truth (env: DG_AGENT_MODEL)
_MAX_TOKENS = 4096

_SCHEMA_HEADERS = [
    "table_name", "field_name", "data_type", "length",
    "description_en", "description_hr", "business_meaning",
    "example_value", "domain_area",
    "description_source", "needs_review",
]
_CONTENT_FIELDS = [
    "field_name", "data_type", "length",
    "description_en", "description_hr", "business_meaning",
    "example_value", "domain_area", "description_source",
]
_ALLOWED_SOURCES = {
    "sap_standard", "column_name_convention",
    "source_column_roles", "inferred",
}
_ALLOWED_DOMAINS = {
    "procurement", "inventory", "materials", "finance",
    "org_structure", "vendor", "equipment", "goods_receipt",
    "workflow", "cross_domain",
}


# ─── env loading ───────────────────────────────────────────────────────

def _load_env() -> None:
    if not _ENV.exists():
        return
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# ─── prompt loading ────────────────────────────────────────────────────

def _load_prompt() -> tuple[str, str]:
    raw = _PROMPT.read_text(encoding="utf-8")
    sys_marker = "## SYSTEM PROMPT"
    user_marker = "## USER PROMPT TEMPLATE"
    s_start = raw.index(sys_marker) + len(sys_marker)
    s_end = raw.index(user_marker)
    return raw[s_start:s_end].strip(), raw[s_end:].replace(user_marker, "").strip()


# ─── DB introspection ──────────────────────────────────────────────────

def _raw_sap_columns(conn) -> dict[str, list[tuple[str, str]]]:
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='raw_sap' ORDER BY table_name, ordinal_position"
    ).fetchall()
    out: dict[str, list[tuple[str, str]]] = {}
    for t, c, d in rows:
        out.setdefault(t.lower(), []).append((c, d))
    return out


def _existing_rows(conn) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT table_name, field_name, data_type, length, description_en, "
        "description_hr, business_meaning, example_value, domain_area, "
        "description_source, needs_review "
        "FROM main_seeds.sap_data_dictionary "
        "ORDER BY table_name, field_name"
    ).fetchall()
    cols = [d[0] for d in conn.description]
    out: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(zip(cols, r))
        out.setdefault(d["table_name"].lower(), []).append(d)
    return out


def _source_column_roles(conn) -> dict[tuple[str, str], dict]:
    rows = conn.execute(
        "SELECT table_name, column_name, role, role_confidence, role_rationale "
        "FROM main_seeds.source_column_roles"
    ).fetchall()
    out: dict[tuple[str, str], dict] = {}
    for t, c, role, conf, rat in rows:
        out[(t.lower(), c.upper())] = {
            "role": role, "confidence": conf, "rationale": rat,
        }
    return out


# ─── prompt composition ────────────────────────────────────────────────

def _build_per_table_prompt(
    table: str,
    missing: list[tuple[str, str]],
    existing: list[dict],
    roles: dict[tuple[str, str], dict],
    template: str,
) -> str:
    domain_hint = ""
    if existing:
        da = [e["domain_area"] for e in existing if e.get("domain_area")]
        if da:
            domain_hint = max(set(da), key=da.count)

    missing_lines = []
    for field, duckdb_type in missing:
        key = (table.lower(), field.upper())
        role_info = roles.get(key, {})
        role_tag = role_info.get("role") or "(no role)"
        missing_lines.append(
            f"  - field_name={field}, duckdb_type={duckdb_type}, role={role_tag}"
        )
    missing_block = "\n".join(missing_lines) if missing_lines else "  (none)"

    if existing:
        existing_lines = []
        for e in existing:
            existing_lines.append(
                f"  - {e['field_name']}: {e['description_en']} "
                f"[{e['data_type']}, domain={e['domain_area']}]"
            )
        existing_block = "\n".join(existing_lines)
    else:
        existing_block = "  (none — this table has no prior documentation)"

    role_lines = []
    for field, _ in missing:
        key = (table.lower(), field.upper())
        r = roles.get(key)
        if r:
            role_lines.append(
                f"  - {field}: role={r['role']} ({r['confidence']}). "
                f"{r['rationale'][:120]}"
            )
    roles_block = "\n".join(role_lines) if role_lines else "  (no role metadata)"

    return (
        template
        .replace("{table_name}", table.upper())
        .replace("{domain_hint}", domain_hint or "to_determine")
        .replace("{missing_columns_block}", missing_block)
        .replace("{existing_rows_block}", existing_block)
        .replace("{roles_block}", roles_block)
    )


# ─── LLM call with caching ─────────────────────────────────────────────

def _call_llm(system_prompt: str, user_prompt: str, api_key: str) -> dict:
    r = requests.post(
        _API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": _MODEL,
            "max_tokens": _MAX_TOKENS,
            "system": [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": user_prompt}],
            }],
        },
        timeout=180,
    )
    r.raise_for_status()
    body = r.json()
    text = body["content"][0]["text"].strip()
    # Strip code fences defensively
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) if not lines[-1].startswith("```") else -1
        text = "\n".join(lines[1:end])
    payload = json.loads(text)
    return {"payload": payload, "usage": body.get("usage", {})}


# ─── validation ────────────────────────────────────────────────────────

def _validate(
    table: str,
    payload: list,
    expected_fields: set[str],
    existing_fields: set[str],
) -> tuple[list[dict], list[str]]:
    """Returns (accepted, rejection_reasons).

    Rejections (per spec Parts 5 + 6):
    - row with field_name in existing_fields → ground-truth regeneration
    - row with description_source not in _ALLOWED_SOURCES
    - row with domain_area not in _ALLOWED_DOMAINS
    - row with missing/blank description_en
    - row with field_name not in expected_fields (unexpected column)
    - duplicate field_name within the same payload
    - inconsistent rule: source=sap_standard but data_type blank
    - inconsistent rule: source=inferred but data_type non-blank
    """
    if not isinstance(payload, list):
        return [], [f"[{table}] payload is not a JSON array (got {type(payload).__name__})"]

    accepted: list[dict] = []
    rejected: list[str] = []
    seen: set[str] = set()

    for i, raw in enumerate(payload):
        if not isinstance(raw, dict):
            rejected.append(f"[{table}][{i}] not a dict")
            continue
        fn = str(raw.get("field_name", "")).upper().strip()
        if not fn:
            rejected.append(f"[{table}][{i}] missing field_name")
            continue
        if fn in existing_fields:
            rejected.append(
                f"[{table}][{i}] field_name={fn} is in ground truth — regeneration blocked"
            )
            continue
        if fn in seen:
            rejected.append(f"[{table}][{i}] duplicate field_name={fn}")
            continue
        if fn not in expected_fields:
            rejected.append(
                f"[{table}][{i}] field_name={fn} not in expected missing list"
            )
            continue
        src = str(raw.get("description_source", "")).strip()
        if src not in _ALLOWED_SOURCES:
            rejected.append(
                f"[{table}][{i}] field_name={fn} invalid description_source='{src}'"
            )
            continue
        dom = str(raw.get("domain_area", "")).strip()
        if dom not in _ALLOWED_DOMAINS:
            rejected.append(
                f"[{table}][{i}] field_name={fn} invalid domain_area='{dom}'"
            )
            continue
        desc_en = str(raw.get("description_en", "")).strip()
        if not desc_en:
            rejected.append(f"[{table}][{i}] field_name={fn} blank description_en")
            continue
        dtype = str(raw.get("data_type", "")).strip()
        if src == "sap_standard" and not dtype:
            rejected.append(
                f"[{table}][{i}] field_name={fn} sap_standard without data_type"
            )
            continue
        if src == "inferred" and dtype:
            rejected.append(
                f"[{table}][{i}] field_name={fn} inferred with non-blank data_type "
                f"(policy: inferred must leave type/length/hr blank)"
            )
            continue
        seen.add(fn)
        needs = "1" if src == "inferred" else "0"
        accepted.append({
            "table_name": table.upper(),
            "field_name": fn,
            "data_type": dtype,
            "length": str(raw.get("length", "")).strip(),
            "description_en": desc_en,
            "description_hr": str(raw.get("description_hr", "")).strip(),
            "business_meaning": str(raw.get("business_meaning", "")).strip(),
            "example_value": str(raw.get("example_value", "")).strip(),
            "domain_area": dom,
            "description_source": src,
            "needs_review": needs,
        })

    missing_from_output = expected_fields - seen
    for fn in sorted(missing_from_output):
        rejected.append(f"[{table}] expected field_name={fn} missing from LLM output")

    return accepted, rejected


# ─── cache ─────────────────────────────────────────────────────────────

def _cache_load(table: str) -> Optional[dict]:
    p = _CACHE_DIR / f"{table}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_save(table: str, payload: list, usage: dict, prompt_hash: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _CACHE_DIR / f"{table}.json"
    p.write_text(
        json.dumps({
            "table": table, "payload": payload, "usage": usage,
            "prompt_hash": prompt_hash,
            "generated_at_utc": dt.datetime.now(dt.timezone.utc)
                                 .replace(tzinfo=None).isoformat(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ─── CSV merge + write ─────────────────────────────────────────────────

def _write_csv(all_rows: list[dict]) -> None:
    all_rows.sort(key=lambda r: (r["table_name"], r["field_name"]))
    tmp = _CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=_SCHEMA_HEADERS,
            lineterminator="\n", quoting=csv.QUOTE_ALL,
        )
        w.writeheader()
        for r in all_rows:
            w.writerow({k: r.get(k, "") for k in _SCHEMA_HEADERS})
    os.replace(tmp, _CSV)


# ─── main ──────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Print plan, no LLM calls, no CSV write.")
    p.add_argument("--table", default=None,
                   help="Process only this raw_sap table (lowercase).")
    p.add_argument("--no-cache", action="store_true",
                   help="Bypass JSON cache (forces LLM re-invocation).")
    args = p.parse_args(argv)

    _load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not args.dry_run:
        print("[error] ANTHROPIC_API_KEY not set")
        return 3

    try:
        system_prompt, user_template = _load_prompt()
    except Exception as e:
        print(f"[error] prompt load failed: {e}")
        return 3
    prompt_hash = hashlib.sha256(
        (system_prompt + user_template).encode("utf-8")
    ).hexdigest()[:12]

    conn = duckdb.connect(str(_DB), read_only=True)
    try:
        raw_cols = _raw_sap_columns(conn)
        existing = _existing_rows(conn)
        roles = _source_column_roles(conn)
    finally:
        conn.close()

    total_raw = sum(len(v) for v in raw_cols.values())
    total_existing = sum(len(v) for v in existing.values())
    print(f"raw_sap tables: {len(raw_cols)}  total raw columns: {total_raw}")
    print(f"existing dictionary rows: {total_existing}")

    missing_by_table: dict[str, list[tuple[str, str]]] = {}
    for table, cols in raw_cols.items():
        if args.table and table != args.table.lower():
            continue
        existing_set = {e["field_name"].upper() for e in existing.get(table, [])}
        miss = [(c, d) for c, d in cols if c.upper() not in existing_set]
        if miss:
            missing_by_table[table] = miss

    total_missing = sum(len(v) for v in missing_by_table.values())
    print(f"tables to process: {len(missing_by_table)}")
    print(f"cells to backfill: {total_missing}")

    if args.dry_run:
        for t, miss in sorted(missing_by_table.items()):
            existing_cnt = len(existing.get(t, []))
            print(f"  {t:25s} missing={len(miss):2d}  existing={existing_cnt}")
        print("\n[dry-run] no LLM calls made")
        return 0

    all_generated: list[dict] = []
    all_rejected: list[str] = []
    api_stats = {"calls": 0, "cached": 0, "input": 0, "output": 0,
                 "cache_read": 0, "cache_creation": 0}
    t0 = time.perf_counter()

    for idx, table in enumerate(sorted(missing_by_table.keys()), 1):
        miss = missing_by_table[table]
        expected_fields = {f for f, _ in miss}
        existing_fields = {e["field_name"].upper() for e in existing.get(table, [])}

        cache = _cache_load(table) if not args.no_cache else None
        if cache and cache.get("prompt_hash") == prompt_hash:
            payload = cache["payload"]
            api_stats["cached"] += 1
            print(f"[{idx:2d}/{len(missing_by_table)}] {table:25s} "
                  f"({len(miss)} cols) [CACHE]")
        else:
            user_prompt = _build_per_table_prompt(
                table, miss, existing.get(table, []), roles, user_template,
            )
            try:
                resp = _call_llm(system_prompt, user_prompt, api_key)
            except Exception as e:
                all_rejected.append(f"[{table}] LLM call failed: {e}")
                print(f"[{idx:2d}/{len(missing_by_table)}] {table:25s} "
                      f"[API FAIL] {type(e).__name__}: {str(e)[:80]}")
                continue
            payload = resp["payload"]
            u = resp["usage"]
            api_stats["calls"] += 1
            api_stats["input"] += u.get("input_tokens", 0)
            api_stats["output"] += u.get("output_tokens", 0)
            api_stats["cache_read"] += u.get("cache_read_input_tokens", 0)
            api_stats["cache_creation"] += u.get("cache_creation_input_tokens", 0)
            _cache_save(table, payload, u, prompt_hash)
            print(f"[{idx:2d}/{len(missing_by_table)}] {table:25s} "
                  f"({len(miss)} cols) [LLM] "
                  f"in={u.get('input_tokens',0)} out={u.get('output_tokens',0)} "
                  f"cache_read={u.get('cache_read_input_tokens',0)}")

        accepted, rejected = _validate(table, payload, expected_fields, existing_fields)
        all_generated.extend(accepted)
        all_rejected.extend(rejected)
        if rejected:
            print(f"    [VALIDATION] {len(rejected)} rejections on {table}:")
            for r in rejected[:5]:
                print(f"      {r}")
            if len(rejected) > 5:
                print(f"      ... ({len(rejected) - 5} more)")

    wall = time.perf_counter() - t0
    print(f"\nWall: {wall:.1f}s  API calls: {api_stats['calls']}  "
          f"cached: {api_stats['cached']}")
    print(f"Tokens: in={api_stats['input']}  out={api_stats['output']}  "
          f"cache_read={api_stats['cache_read']}  "
          f"cache_write={api_stats['cache_creation']}")
    # Cost (Sonnet pricing: in=$3/MTok, out=$15/MTok, cache_read=$0.30/MTok,
    # cache_write 5m=$3.75/MTok per Anthropic pricing as of knowledge cutoff)
    cost = (api_stats["input"] * 3e-6
            + api_stats["output"] * 15e-6
            + api_stats["cache_read"] * 0.3e-6
            + api_stats["cache_creation"] * 3.75e-6)
    print(f"Estimated cost: ${cost:.4f}")

    print(f"\ngenerated rows: {len(all_generated)}  rejected: {len(all_rejected)}")

    if all_rejected and not args.table:
        log_path = _ROOT / "logs" / f"catalog_backfill_rejections_{int(time.time())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(all_rejected), encoding="utf-8")
        print(f"rejection log: {log_path}")

    # Assemble full CSV: existing (unchanged) + generated (new)
    full = []
    for table, rows_list in existing.items():
        for row in rows_list:
            full.append({k: ("" if row.get(k) is None else str(row.get(k)))
                         for k in _SCHEMA_HEADERS})
    full.extend(all_generated)

    print(f"\ntotal rows for CSV write: {len(full)}")

    if args.table:
        print("[single-table mode] not running dbt seed automatically; run manually.")

    try:
        _write_csv(full)
        print(f"wrote {_CSV.name}")
    except Exception as e:
        print(f"[error] CSV write failed: {e}")
        return 2

    # dbt seed (full-refresh per schema change on first run; no-op subsequent)
    if not args.table:
        print("\nRunning dbt seed...")
        rc = os.system('cd dbt && dbt seed --full-refresh --select sap_data_dictionary')
        if rc != 0:
            print("[error] dbt seed returned non-zero")
            return 2

    # Verification
    print("\n=== VERIFICATION ===")
    conn = duckdb.connect(str(_DB), read_only=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM main_seeds.sap_data_dictionary").fetchone()[0]
        print(f"total rows: {n}")
        cov = conn.execute(
            "SELECT COUNT(DISTINCT table_name) FROM main_seeds.sap_data_dictionary"
        ).fetchone()[0]
        print(f"distinct tables: {cov}")
        src_dist = conn.execute(
            "SELECT description_source, needs_review, COUNT(*) "
            "FROM main_seeds.sap_data_dictionary "
            "GROUP BY description_source, needs_review "
            "ORDER BY description_source, needs_review"
        ).fetchall()
        print("source × needs_review distribution:")
        for s, nr, c in src_dist:
            print(f"  {s:25s} needs_review={nr}  n={c}")
    finally:
        conn.close()

    if all_rejected:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
