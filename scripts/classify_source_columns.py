"""Classify raw_sap columns by analytical role.

known_issue #53 note: deliberately NOT wired to sync_parquet_and_invalidate.
This is a rare-run utility (analyst invokes maybe once per schema change);
if run while Streamlit is open, manually restart the dashboard OR run
`python scripts/export_parquet.py` afterward to refresh the view catalog.

Five stages:
  1. Schema scan — information_schema.columns WHERE table_schema='raw_sap'.
  2. Mechanical-rule pass — DATE/TIME/QUAN/CURR/DEC/DECIMAL deterministic.
  3. Cache lookup — RULE 21 hash-cache at dbt/seeds/.source_column_roles_cache.json.
  4. LLM batch classification — Claude Sonnet, 50-col batches, RULE 38 retries.
  5. Atomic seed + audit-log write — RULE 37 guard, log-first-primary-second.

Roles: measure / dimension / date / key / text.

Invocation modes:
  python scripts/classify_source_columns.py               # cold start or delta
  python scripts/classify_source_columns.py --catchup     # ignore 50-row cap
  python scripts/classify_source_columns.py --force-all   # bypass cache + override guard
  python scripts/classify_source_columns.py --dry-run     # stages 1-4 only
  python scripts/classify_source_columns.py --table ekpo  # restrict to one table
  python scripts/classify_source_columns.py --verbose     # per-column logging

Requires ANTHROPIC_API_KEY in .env or environment for the LLM stage. Without
it, --dry-run still exercises stages 1-3 and reports what WOULD be called.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "dbt" / "seeds"
DB_PATH = ROOT / "cpe_analytics.duckdb"
CACHE_PATH = SEED_DIR / ".source_column_roles_cache.json"
ENV_PATH = ROOT / ".env"

sys.path.insert(0, str(ROOT / "app"))
from _csv_safeguard import assert_csv_safe, assert_fieldnames_cover_rows  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sidecar import (  # noqa: E402
    compute_file_hash,
    current_git_head_sha,
    now_iso_utc,
    SIDECAR_SCHEMA_VERSION,
    write_sidecar,
)

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"
PROMPT_VERSION = 1
BATCH_SIZE = 50
CAP_PER_RUN = 50
MAX_RETRIES = 3

ROLES = ("measure", "dimension", "date", "key", "text")
CONFIDENCES = ("high", "medium", "low")
SOURCES = ("llm_classified", "user_override", "default")

PRIMARY_FIELDS = [
    "table_name", "column_name", "role", "role_confidence", "role_rationale",
    "role_source", "user_override_reason", "classified_at_utc",
    "schema_version", "previous_role", "previous_role_source", "stale",
    "stale_reason",
]
CHANGES_FIELDS = [
    "changed_at_utc", "table_name", "column_name",
    "from_role", "from_source", "from_confidence", "from_rationale",
    "to_role", "to_source", "changed_by", "reason",
]

if ENV_PATH.exists():
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# --- Stage 1: schema scan ---------------------------------------------------

def scan_raw_sap(conn) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT table_name, column_name, data_type, ordinal_position "
        "FROM information_schema.columns WHERE table_schema='raw_sap' "
        "ORDER BY table_name, ordinal_position"
    ).fetchall()
    by_table: dict[str, list[dict]] = {}
    for t, c, dt, op in rows:
        by_table.setdefault(t, []).append({
            "column_name": c, "data_type": dt, "ordinal_position": op,
        })
    return by_table


def compute_schema_version(cols: list[dict]) -> str:
    payload = ",".join(
        f"{c['column_name']}:{c['data_type']}"
        for c in sorted(cols, key=lambda x: x["ordinal_position"])
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# --- Stage 2: mechanical rules -----------------------------------------------

def mechanical_rule(data_type: str) -> tuple[str, str] | None:
    dt = (data_type or "").upper()
    if dt in ("DATE", "DATS"):
        return ("date", "SAP DATE type — temporal grain marker")
    if dt in ("TIME", "TIMS", "TIMESTAMP"):
        return ("date", "SAP TIME/TIMESTAMP type — temporal grain marker")
    if dt in ("QUAN", "CURR", "DEC"):
        return ("measure", f"SAP {dt} type — aggregatable quantity or amount")
    if dt.startswith("DECIMAL") or dt.startswith("NUMERIC"):
        return ("measure", "Decimal numeric type — aggregatable quantity or amount")
    if dt in ("DOUBLE", "FLOAT", "REAL"):
        return ("measure", "Floating-point numeric type — aggregatable measure")
    return None


# --- Stage 3: cache (RULE 21) ----------------------------------------------

def cache_key(table: str, column: str, dtype: str, desc_en: str,
              meaning: str, siblings: list[tuple[str, str]]) -> str:
    payload = "|".join([
        f"v{PROMPT_VERSION}", MODEL, table, column, dtype or "",
        desc_en or "", meaning or "",
        ",".join(f"{n}:{t}" for n, t in sorted(siblings)),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_cache() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] cache unreadable ({e}); starting fresh")
        return {}


def save_cache(cache: dict[str, dict]) -> None:
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        json.dump(cache, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, CACHE_PATH)


# --- Stage 4: LLM -----------------------------------------------------------

SYSTEM_PROMPT = """You are a senior SAP data architect classifying raw-source columns by analytical role for a procurement analytics warehouse.

ROLES (exactly one per column):
- measure: aggregatable quantities or amounts (counts, sums, averages, prices, weights).
- dimension: groupable categorical values useful for slicing (movement types, plants, countries, flags, codes with low-to-moderate cardinality).
- date: date or datetime fields used as temporal grain.
- key: identifier or join key — high cardinality, near-unique per row, used to join tables rather than to group rows. Master-data PKs (LIFNR, MATNR, EBELN, EQUNR, KUNNR) stay `key` even when grouped-by in practice; analyses override locally.
- text: human-readable labels paired with a code column in the same table (MAKTX, LIFNT, TXZ01).

CONFIDENCE RUBRIC:
- high: well-known SAP field with unambiguous role (MATNR→key, BUDAT→date, NETWR→measure, BWART→dimension, MAKTX→text).
- medium: field where the rules apply cleanly but the name isn't well-known (e.g. custom Z-fields that clearly look like measures).
- low: rules don't fire cleanly; classification is best-guess. Will surface for analyst review.

TIE-BREAKERS:
- Numeric-looking code (NUMC `101` for BWART) → dimension.
- Structural PK of a master table → key.
- Unit/currency companion fields (MEINS, WAERS, BPRME) → dimension.
- MANDT (SAP client) → key, rationale notes constant-in-HT.
- CHAR(1) X/blank flags → dimension.

OUTPUT:
Return ONLY a JSON array. One object per input column in input order:
{"table_name":"...","column_name":"...","role":"measure|dimension|date|key|text","role_confidence":"high|medium|low","role_rationale":"..."}

Rationale: ONE sentence, ≤250 chars, English. No SQL, no code fences, no preamble, no trailing prose."""


def build_user_prompt(batch: list[dict]) -> str:
    lines = [f"Classify these {len(batch)} raw SAP columns:\n"]
    for i, col in enumerate(batch, 1):
        sib = ", ".join(f"{n}:{t}" for n, t in col["siblings"][:5]) or "(none)"
        lines.append(f"--- {i} ---")
        lines.append(f"table: {col['table_name']}")
        lines.append(f"column: {col['column_name']}")
        lines.append(f"data_type: {col['data_type']}")
        if col.get("description_en"):
            lines.append(f"description: {col['description_en']}")
        if col.get("business_meaning"):
            lines.append(f"meaning: {col['business_meaning']}")
        if col.get("example_value"):
            lines.append(f"example: {col['example_value']}")
        lines.append(f"siblings: {sib}")
        lines.append("")
    return "\n".join(lines)


def call_llm(batch: list[dict], api_key: str) -> list[dict]:
    body = {
        "model": MODEL,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_user_prompt(batch)}],
    }
    r = requests.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=180,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) if not lines[-1].startswith("```") else -1
        text = "\n".join(lines[1:end])
    return json.loads(text)


def validate_cls(obj) -> str | None:
    if not isinstance(obj, dict):
        return "not a dict"
    for f in ("table_name", "column_name", "role", "role_confidence", "role_rationale"):
        if f not in obj:
            return f"missing {f!r}"
    if obj["role"] not in ROLES:
        return f"invalid role {obj['role']!r}"
    if obj["role_confidence"] not in CONFIDENCES:
        return f"invalid confidence {obj['role_confidence']!r}"
    return None


def classify_batch(batch: list[dict], api_key: str, verbose: bool) -> dict:
    """Return {(table, column): classification}. RULE 38 pattern, 3 retries."""
    done: dict = {}
    pending = list(batch)
    for attempt in range(1, MAX_RETRIES + 1):
        if not pending:
            break
        try:
            raw = call_llm(pending, api_key)
        except Exception as e:
            if verbose:
                print(f"    [retry {attempt}/{MAX_RETRIES}] {type(e).__name__}: {e}")
            time.sleep(min(2 ** attempt, 10))
            continue

        still: list[dict] = []
        for col in pending:
            match = next(
                (o for o in raw if isinstance(o, dict)
                 and o.get("table_name") == col["table_name"]
                 and o.get("column_name") == col["column_name"]),
                None,
            )
            if match is None:
                still.append(col)
                continue
            err = validate_cls(match)
            if err:
                if verbose:
                    print(f"    [invalid] {col['column_name']}: {err}")
                still.append(col)
                continue
            rat = str(match["role_rationale"])
            if len(rat) > 300:
                rat = rat[:297] + "..."
            done[(col["table_name"], col["column_name"])] = {
                "role": match["role"],
                "role_confidence": match["role_confidence"],
                "role_rationale": rat,
            }
        pending = still

    # Fallback after 3 attempts
    for col in pending:
        done[(col["table_name"], col["column_name"])] = {
            "role": "key",
            "role_confidence": "low",
            "role_rationale": (
                "LLM classification failed after 3 attempts — fallback to key-low-confidence"
            ),
        }
    return done


# --- Stage 5: atomic write --------------------------------------------------

def load_existing() -> list[dict]:
    path = SEED_DIR / "source_column_roles.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_primary(rows: list[dict]) -> None:
    path = SEED_DIR / "source_column_roles.csv"
    assert_csv_safe(path, pd.DataFrame(rows))
    assert_fieldnames_cover_rows(PRIMARY_FIELDS, rows)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PRIMARY_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def append_changes(events: list[dict]) -> None:
    if not events:
        return
    path = SEED_DIR / "source_column_role_changes.csv"
    assert_fieldnames_cover_rows(CHANGES_FIELDS, events)
    need_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CHANGES_FIELDS, lineterminator="\n")
        if need_header:
            w.writeheader()
        w.writerows(events)


# --- Orchestration ---------------------------------------------------------

def _change_event(now: str, existing: dict, new: dict, reason: str,
                  changed_by: str = "pipeline") -> dict:
    return {
        "changed_at_utc": now,
        "table_name": new["table_name"],
        "column_name": new["column_name"],
        "from_role": existing.get("role", ""),
        "from_source": existing.get("role_source", ""),
        "from_confidence": existing.get("role_confidence", ""),
        "from_rationale": existing.get("role_rationale", ""),
        "to_role": new["role"],
        "to_source": new["role_source"],
        "changed_by": changed_by,
        "reason": reason,
    }


def _build_row(col_meta: dict, role: str, confidence: str, rationale: str,
               source: str, now: str, existing: dict | None) -> dict:
    return {
        "table_name": col_meta["table_name"],
        "column_name": col_meta["column_name"],
        "role": role,
        "role_confidence": confidence,
        "role_rationale": rationale,
        "role_source": source,
        "user_override_reason": "",
        "classified_at_utc": now,
        "schema_version": col_meta["schema_version"],
        "previous_role": (existing or {}).get("role", ""),
        "previous_role_source": (existing or {}).get("role_source", ""),
        "stale": "false",
        "stale_reason": "",
    }


def classify_run(*, force_all: bool = False, catchup: bool = False,
                 dry_run: bool = False, table_filter: str | None = None,
                 verbose: bool = False) -> int:
    # Stage 1: scan
    if not DB_PATH.exists():
        print(f"[error] {DB_PATH} not found")
        return 2
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        by_table = scan_raw_sap(conn)
    finally:
        conn.close()
    if table_filter:
        by_table = {t: cols for t, cols in by_table.items() if t == table_filter}
        if not by_table:
            print(f"[error] table {table_filter!r} not found in raw_sap")
            return 2

    # Dictionary for enrichment
    dd_rows = []
    dd_path = SEED_DIR / "sap_data_dictionary.csv"
    if dd_path.exists():
        with dd_path.open("r", encoding="utf-8", newline="") as f:
            dd_rows = list(csv.DictReader(f))
    dd_by_key = {
        (r.get("table_name", "").upper(), r.get("field_name", "").upper()): r
        for r in dd_rows
    }

    existing = load_existing()
    existing_by_key = {(r["table_name"], r["column_name"]): r for r in existing}

    now = now_iso_utc()
    kept: dict[tuple[str, str], dict] = {}
    events: list[dict] = []
    to_llm: list[dict] = []

    # Stage 2: mechanical + user-override preservation
    for table, cols in by_table.items():
        schema_v = compute_schema_version(cols)
        sibling_pool = [(c["column_name"], c["data_type"]) for c in cols]
        for c in cols:
            key = (table, c["column_name"])
            er = existing_by_key.get(key)

            # User overrides: preserve unless --force-all
            if er and er.get("role_source") == "user_override" and not force_all:
                er["schema_version"] = schema_v
                kept[key] = er
                continue

            dd = dd_by_key.get((table.upper(), c["column_name"].upper()), {})
            siblings = [(n, t) for n, t in sibling_pool if n != c["column_name"]][:5]
            col_meta = {
                "table_name": table, "column_name": c["column_name"],
                "data_type": c["data_type"], "schema_version": schema_v,
            }

            # Try SAP type (from sap_data_dictionary) before DuckDB type —
            # generator stores SAP dates/amounts as VARCHAR/DOUBLE, losing
            # the DATS/QUAN/CURR signal at the DuckDB layer. The dict
            # preserves it. Falls through to DuckDB type when dict is silent.
            mech = mechanical_rule(dd.get("data_type", "")) or mechanical_rule(c["data_type"])
            if mech:
                role, rat = mech
                row = _build_row(col_meta, role, "high", rat, "default", now, er)
                if er and er.get("role") != role:
                    events.append(_change_event(now, er, row, "mechanical_rule"))
                if force_all and er and er.get("role_source") == "user_override":
                    events.append(_change_event(now, er, row, "forced_full_reclassify"))
                kept[key] = row
                continue

            to_llm.append({**col_meta, **{
                "description_en": dd.get("description_en", ""),
                "business_meaning": dd.get("business_meaning", ""),
                "example_value": dd.get("example_value", ""),
                "siblings": siblings,
                "_existing": er,
            }})

    # Stage 3: cache lookup
    cache = {} if force_all else load_cache()
    llm_queue: list[dict] = []
    cache_hits = 0
    for col in to_llm:
        ck = cache_key(col["table_name"], col["column_name"], col["data_type"],
                       col["description_en"], col["business_meaning"], col["siblings"])
        hit = cache.get(ck)
        if hit:
            cache_hits += 1
            er = col["_existing"]
            row = _build_row(col, hit["role"], hit["role_confidence"],
                             hit["role_rationale"], "llm_classified", now, er)
            if er and er.get("role") != hit["role"]:
                events.append(_change_event(now, er, row, "cache_hit_classification_change"))
            if force_all and er and er.get("role_source") == "user_override":
                events.append(_change_event(now, er, row, "forced_full_reclassify"))
            kept[(col["table_name"], col["column_name"])] = row
        else:
            col["_cache_key"] = ck
            llm_queue.append(col)

    # Per-run cap unless --catchup/--force-all
    deferred = 0
    if not catchup and not force_all and len(llm_queue) > CAP_PER_RUN:
        deferred = len(llm_queue) - CAP_PER_RUN
        llm_queue = llm_queue[:CAP_PER_RUN]
        print(f"[cap] {CAP_PER_RUN}-row classification cap reached; "
              f"{deferred} rows deferred to next run")

    total_cols = sum(len(v) for v in by_table.values())
    print(f"Scan: {total_cols} columns across {len(by_table)} tables.")
    print(f"  Mechanical/override/cache settled: {len(kept)}")
    print(f"  LLM queue: {len(llm_queue)}"
          f"{f' (+{deferred} deferred)' if deferred else ''}")
    print(f"  Cache hits: {cache_hits}")

    # Stage 4: LLM
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if llm_queue and dry_run:
        n_batches = (len(llm_queue) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"[dry-run] would call LLM for {len(llm_queue)} columns "
              f"in {n_batches} batch(es). Skipping.")
    elif llm_queue:
        if not api_key:
            print("[error] ANTHROPIC_API_KEY not set; cannot run LLM stage. "
                  "Use --dry-run or set the key.")
            return 2
        for i in range(0, len(llm_queue), BATCH_SIZE):
            batch = llm_queue[i:i + BATCH_SIZE]
            print(f"  batch {i // BATCH_SIZE + 1}: {len(batch)} columns...")
            results = classify_batch(batch, api_key, verbose)
            for col in batch:
                cls = results[(col["table_name"], col["column_name"])]
                # Write to cache even for fallbacks (avoids re-burning tokens next run)
                cache[col["_cache_key"]] = {
                    **cls,
                    "cached_at_utc": now,
                    "llm_model": MODEL,
                    "prompt_version": PROMPT_VERSION,
                }
                er = col["_existing"]
                row = _build_row(col, cls["role"], cls["role_confidence"],
                                 cls["role_rationale"], "llm_classified", now, er)
                if er and er.get("role") != cls["role"]:
                    events.append(_change_event(now, er, row, "llm_classification_change"))
                if force_all and er and er.get("role_source") == "user_override":
                    events.append(_change_event(now, er, row, "forced_full_reclassify"))
                kept[(col["table_name"], col["column_name"])] = row

    # Preserve deferred existing rows (not touched this run)
    classified_keys = set(kept.keys())
    for key, er in existing_by_key.items():
        if key not in classified_keys:
            kept[key] = er

    # Hard-delete rows whose columns no longer exist in raw_sap
    if table_filter is None:
        live = {(t, c["column_name"]) for t, cols in by_table.items() for c in cols}
        dropped = [k for k in kept if k not in live]
        for k in dropped:
            del kept[k]
        if dropped and verbose:
            print(f"  hard-deleted {len(dropped)} row(s) for columns no longer in raw_sap")

    final = sorted(kept.values(), key=lambda r: (r["table_name"], r["column_name"]))

    if dry_run:
        print(f"[dry-run] would write {len(final)} primary rows, "
              f"{len(events)} change events. Skipping stage 5.")
        return 0

    # Stage 5: audit log FIRST, then primary, then cache, then sidecar
    append_changes(events)
    save_primary(final)
    save_cache(cache)

    primary_hash = compute_file_hash(SEED_DIR / "source_column_roles.csv")
    changes_path = SEED_DIR / "source_column_role_changes.csv"
    changes_hash = compute_file_hash(changes_path) if changes_path.exists() else ""
    write_sidecar("source_column_roles", {
        "artifact": "source_column_roles",
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "built_at_utc": now,
        "git_head_sha": current_git_head_sha(),
        "inputs": {
            "source_column_roles.csv": {"sha256": primary_hash},
            "source_column_role_changes.csv": {"sha256": changes_hash},
        },
        "stats": {
            "total_rows": len(final),
            "change_events_appended": len(events),
            "cache_hits": cache_hits,
            "llm_batches": (len(llm_queue) + BATCH_SIZE - 1) // BATCH_SIZE
                           if llm_queue else 0,
            "deferred": deferred,
        },
    })

    partial = any(r.get("role_rationale", "").startswith(
        "LLM classification failed") for r in final)
    print(f"[ok] wrote {len(final)} classifications, {len(events)} change events.")
    return 1 if partial else 0


# --- User-override helper (called from Streamlit UI) ----------------------

def apply_user_override(table_name: str, column_name: str, new_role: str,
                        user_override_reason: str, changed_by: str) -> None:
    if new_role not in ROLES:
        raise ValueError(f"new_role {new_role!r} not in {ROLES}")
    reason = (user_override_reason or "").strip()
    if not reason:
        raise ValueError("user_override_reason is required")
    if len(reason) > 500:
        raise ValueError("user_override_reason must be ≤500 chars")

    existing = load_existing()
    target = next(
        (r for r in existing
         if r["table_name"] == table_name and r["column_name"] == column_name),
        None,
    )
    if target is None:
        raise ValueError(
            f"ColumnNotClassifiedError: {table_name}.{column_name} has no prior "
            f"classification. Run classify_source_columns.py first."
        )

    now = now_iso_utc()
    append_changes([{
        "changed_at_utc": now, "table_name": table_name, "column_name": column_name,
        "from_role": target.get("role", ""),
        "from_source": target.get("role_source", ""),
        "from_confidence": target.get("role_confidence", ""),
        "from_rationale": target.get("role_rationale", ""),
        "to_role": new_role, "to_source": "user_override",
        "changed_by": changed_by or "unknown", "reason": reason,
    }])

    target["previous_role"] = target.get("role", "")
    target["previous_role_source"] = target.get("role_source", "")
    target["role"] = new_role
    target["role_source"] = "user_override"
    target["user_override_reason"] = reason
    target["classified_at_utc"] = now
    target["stale"] = "false"
    target["stale_reason"] = ""
    save_primary(existing)


# --- CLI -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force-all", action="store_true",
                    help="bypass cache + user-override preservation")
    ap.add_argument("--catchup", action="store_true",
                    help="ignore the 50-row cap; classify until backlog is zero")
    ap.add_argument("--dry-run", action="store_true",
                    help="run stages 1-3 only; print what WOULD be written")
    ap.add_argument("--table", metavar="NAME",
                    help="restrict to one raw_sap table")
    ap.add_argument("--verbose", action="store_true",
                    help="per-column decisions (default: per-table summary)")
    args = ap.parse_args()

    if args.force_all:
        existing = load_existing()
        overrides = [r for r in existing if r.get("role_source") == "user_override"]
        if overrides:
            print(f"WARNING: --force-all will overwrite {len(overrides)} user override(s).")
            print("Prior state will be preserved in source_column_role_changes.csv.")
            resp = input("Type YES to proceed: ").strip()
            if resp != "YES":
                print("Aborted.")
                return 3

    return classify_run(
        force_all=args.force_all, catchup=args.catchup, dry_run=args.dry_run,
        table_filter=args.table, verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
