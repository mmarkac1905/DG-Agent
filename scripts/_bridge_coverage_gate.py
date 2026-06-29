"""Option B Phase 2 — runtime bridge_coverage gate.

Consumes bridge_coverage_by_filter DARs (Phase 1 commit 70b90a2) to
refuse SQL whose filter values are empirically unreachable through
the chosen joins. Deterministic, data-side check that complements C5's
LLM-judgment-based scope-mismatch trigger.

Closes the LLM-overconfident-yes architectural gap (known_issue #100):
fires before the runner's mechanical execute on SQL that would return
0 rows because the filter value cannot be reached through the join.

Per design doc tasks/option_b_design.md Component 2 with refinements:
  F-2 subset-match: DAR via_keys must be subset of SQL join keys
       (reachability is monotonically permissive in keys; subset is
       conservative).
  F-3 IN-list semantics: '=' refuses iff value in unreachable;
       'IN' refuses iff ALL values unreachable; warns iff mixed.
  F-4 layer mapping: stg_sap__X -> X via substring substitution
       (staging is empirically passthrough per OQ-B).
  F-5 CTE handling: sqlglot's find_all walks into CTEs natively;
       _s2t_cardinality_validator's CTE-flatten reused via its
       extract_joins_from_sql.
  OQ-C hybrid policy: soft fall-through when no DARs in scope;
       engages gate only when DARs are present.
"""
from __future__ import annotations

import json
import sys
from typing import Optional

import sqlglot
from sqlglot import expressions as exp

from _s2t_cardinality_validator import (  # noqa: E402
    _column_info,
    _flatten_ctes,
    _staging_to_raw,
    _strip_jinja_refs,
    extract_joins_from_sql,
)

# Mirror Phase 1 analyzer's allowlist (commit 70b90a2). Filters on
# columns outside this set fall through (no DAR could exist for them).
_ALLOWLIST_FILTER_COLUMNS: tuple[str, ...] = (
    "BWART", "BSTYP", "BSART", "BLART", "MTART",
    "KTOPL", "LOEKZ", "STATU", "KOART", "SHKZG",
)


# --- helpers exposed for tests ---------------------------------------

def _normalize_layer(table_name: str) -> str:
    """F-4: stg_sap__X -> X. Tables not following the staging convention
    pass through lowercased. Mirrors _staging_to_raw from the s2t
    validator (kept as separate symbol for tests + readability)."""
    return _staging_to_raw(table_name or "")


def _extract_join_chain(sql: str) -> list[dict]:
    """F-5 CTE-aware join extraction. Thin wrapper over the s2t
    validator's tested extractor; returns the same shape:
    {left_table, right_table, left_alias, right_alias, kind, join_keys}."""
    return extract_joins_from_sql(sql or "")


def _walk_predicates(node) -> list:
    """Collect EQ/IN nodes from an AND-tree (mirrors _walk_eqs but
    accepts both predicate kinds)."""
    out: list = []
    if isinstance(node, exp.And):
        out.extend(_walk_predicates(node.this))
        out.extend(_walk_predicates(node.expression))
    elif isinstance(node, (exp.EQ, exp.In)):
        out.append(node)
    return out


def _build_alias_to_table(flat_ast) -> dict[str, str]:
    """Map every alias in the (CTE-flattened) AST to its raw_sap base
    table name. Subquery aliases (introduced by CTE inlining) map to
    their leftmost base table — same convention as the s2t validator.
    For CTE-projected columns, prefer _resolve_filter_table which
    traces through SELECT projections."""
    m: dict[str, str] = {}
    for tbl in flat_ast.find_all(exp.Table):
        alias = (tbl.alias or tbl.name or "").lower()
        if alias:
            m[alias] = _staging_to_raw(tbl.name)
    for sub in flat_ast.find_all(exp.Subquery):
        if not sub.alias:
            continue
        base = next(iter(sub.find_all(exp.Table)), None)
        if base is None:
            continue
        m[sub.alias.lower()] = _staging_to_raw(base.name)
    return m


def _resolve_subquery_column(sub, column: str) -> Optional[str]:
    """Trace `column` through the subquery's SELECT projections to its
    raw_sap base table. None if the projection isn't a simple Column
    or doesn't resolve. Handles `SELECT m.BWART ...` and
    `SELECT m.BWART AS x ...` forms."""
    select = sub.this if isinstance(sub.this, exp.Select) else None
    if select is None:
        return None
    sub_aliases: dict[str, str] = {}
    for tbl in sub.find_all(exp.Table):
        sub_aliases[(tbl.alias or tbl.name).lower()] = (
            _staging_to_raw(tbl.name)
        )
    target = column.upper()
    for proj in select.expressions or []:
        if isinstance(proj, exp.Alias):
            out_name = proj.alias
            inner = proj.this
        else:
            out_name = getattr(proj, "alias_or_name", None) or (
                proj.name if isinstance(proj, exp.Column) else None
            )
            inner = proj
        if not out_name or out_name.upper() != target:
            continue
        if isinstance(inner, exp.Column):
            src_alias = (inner.table or "").lower()
            if src_alias:
                return sub_aliases.get(src_alias)
    return None


def _resolve_unqualified_filter(flat_ast, column: str) -> Optional[str]:
    """C3+C4 validation follow-up — handle unqualified outer column
    references through CTE projections.

    When a WHERE predicate references a column without alias
    qualification (e.g. `WHERE BWART = '201'`), the prior gate
    short-circuited at the empty-alias check and silently dropped the
    filter. LLMs routinely emit unqualified outer columns when there's
    only one source post-CTE-flatten (idiomatic post-CTE selection),
    and BAR-00008 / BAR-00009 demonstrated this evades the gate.

    Strategy: walk every Subquery in the flattened AST, attempt
    `_resolve_subquery_column` for the named column, collect unique
    raw_sap candidates. Bind iff exactly one candidate resolves
    (conservative on ambiguity — a multi-source CTE projecting the
    same column name returns None, leaving the existing fall-through
    semantic).

    The base-table-column-list strategy (resolve unqualified column
    against base tables in scope by querying their column lists) is
    NOT implemented here; it would require either DuckDB schema
    queries or a sap_data_dictionary lookup the gate doesn't currently
    perform. The CTE-projection trace covers the dominant LLM idiom
    that this session uncovered; direct unqualified base-table
    references in non-CTE outer SELECTs remain a known gap.
    """
    candidates: set[str] = set()
    for sub in flat_ast.find_all(exp.Subquery):
        traced = _resolve_subquery_column(sub, column)
        if traced:
            candidates.add(traced)
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _resolve_filter_table(
    flat_ast, alias: str, column: str, alias_map: dict[str, str],
) -> Optional[str]:
    """Resolve (alias.column) to a raw_sap table. For Subquery aliases
    (CTE bodies after flattening), trace the column through SELECT
    projections; for base-table aliases, look up alias_map. For empty
    alias (unqualified outer column reference), attempt unambiguous
    single-source resolution via `_resolve_unqualified_filter`."""
    if not alias:
        return _resolve_unqualified_filter(flat_ast, column)
    alias_lc = alias.lower()
    for sub in flat_ast.find_all(exp.Subquery):
        if (sub.alias or "").lower() == alias_lc:
            traced = _resolve_subquery_column(sub, column)
            if traced:
                return traced
            break
    return alias_map.get(alias_lc)


def _extract_equality_filters(flat_ast, alias_map: dict[str, str]) -> list[dict]:
    """F-3: extract WHERE-side `=` and `IN` predicates with
    column-to-literal pattern. Skips column-to-column EQ (those are
    join conditions). Skips range/LIKE/non-allowlist columns.

    Returns list of {raw_table, column, operator, values}.
    """
    filters: list[dict] = []
    for where in flat_ast.find_all(exp.Where):
        for pred in _walk_predicates(where.this):
            if isinstance(pred, exp.EQ):
                col_name, col_alias = _column_info(pred.this)
                rhs = pred.expression
                # col_alias is allowed to be None — _resolve_filter_table
                # handles unqualified columns via single-source CTE
                # projection trace (C3+C4 validation follow-up).
                if col_name is None:
                    continue
                if not isinstance(rhs, exp.Literal):
                    continue
                col_up = col_name.upper()
                if col_up not in _ALLOWLIST_FILTER_COLUMNS:
                    continue
                raw_table = _resolve_filter_table(
                    flat_ast, col_alias or "", col_name, alias_map,
                )
                if not raw_table:
                    continue
                filters.append({
                    "raw_table": raw_table,
                    "column": col_up,
                    "operator": "=",
                    "values": [str(rhs.this)],
                })
            elif isinstance(pred, exp.In):
                col_name, col_alias = _column_info(pred.this)
                if col_name is None:
                    continue
                col_up = col_name.upper()
                if col_up not in _ALLOWLIST_FILTER_COLUMNS:
                    continue
                raw_table = _resolve_filter_table(
                    flat_ast, col_alias or "", col_name, alias_map,
                )
                if not raw_table:
                    continue
                values: list[str] = []
                for v in pred.expressions or []:
                    if isinstance(v, exp.Literal):
                        values.append(str(v.this))
                if not values:
                    continue
                filters.append({
                    "raw_table": raw_table,
                    "column": col_up,
                    "operator": "IN",
                    "values": values,
                })
    return filters


def _load_bridge_coverage_dars(
    conn, scope_tables: list[str],
) -> list[dict]:
    """Query main_seeds.domain_analysis_results for
    bridge_coverage_by_filter rows whose source_tables overlaps
    scope_tables AND status='success'. Per OQ-C hybrid policy: returns
    [] if no DARs in scope (gate falls through)."""
    if not scope_tables:
        return []
    scope_lc = [t.lower() for t in scope_tables]
    try:
        rows = conn.execute(
            "SELECT id, result_json FROM main_seeds.domain_analysis_results "
            "WHERE analysis_type = 'bridge_coverage_by_filter' "
            "  AND status = 'success' "
            "  AND len(list_intersect("
            "       list_transform(string_split(LOWER(source_tables), ','),"
            "                      x -> trim(x)),"
            "       ?::VARCHAR[])) > 0",
            [scope_lc],
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for dar_id, rj in rows:
        try:
            d = json.loads(rj or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        d["_dar_id"] = dar_id
        out.append(d)
    return out


def _find_bridge_for_filter(
    joins: list[dict], filter_raw_table: str,
) -> Optional[dict]:
    """Return the join whose right_table matches the filter's raw
    table — that's the bridge whose to-side carries the filter. Filters
    on the directly-FROM'd table (left side of every join) return None
    (gate falls through; no bridge restricts the row population)."""
    for j in joins:
        if (j.get("right_table") or "").lower() == filter_raw_table.lower():
            return j
    return None


def _match_dar(
    dars: list[dict], bridge: dict, filter_col: str,
) -> list[dict]:
    """F-2 subset-match: DAR's (from-key, to-key) pairs must be a
    subset of the SQL join's (from-col, to-col) pairs. Reachability
    cannot grow with additional join keys; subset-match is conservative.
    """
    sql_pairs = {
        (k[0].upper(), k[1].upper()) for k in bridge.get("join_keys") or []
    }
    if not sql_pairs:
        return []
    matches: list[dict] = []
    for dar in dars:
        b = dar.get("bridge") or {}
        fc = dar.get("filter_column") or {}
        if (b.get("from_table") or "").lower() != (
            bridge.get("left_table") or ""
        ).lower():
            continue
        if (b.get("to_table") or "").lower() != (
            bridge.get("right_table") or ""
        ).lower():
            continue
        if (fc.get("column") or "").upper() != filter_col.upper():
            continue
        from_keys = [k.upper() for k in b.get("via_keys_from_to_mid") or []]
        to_keys = [k.upper() for k in b.get("from_to_mid_to_columns") or []]
        if not from_keys or len(from_keys) != len(to_keys):
            continue
        dar_pairs = set(zip(from_keys, to_keys))
        if dar_pairs.issubset(sql_pairs):
            matches.append(dar)
    return matches


def _check_reachability(
    dar: dict, filter_op: str, filter_values: list[str],
) -> tuple[Optional[str], str]:
    """F-3 IN-list semantics. Returns (action, message):
    - action='refuse': '=' on unreachable value, or 'IN' with ALL
      values unreachable (entire predicate is unsatisfiable).
    - action='warn': 'IN' with mixed reach/unreach (analyst review
      needed; gate doesn't refuse).
    - action=None: no issue.
    """
    unreachable = set(dar.get("unreachable_values") or [])
    if not unreachable:
        return None, ""
    fvals = set(filter_values or [])
    if not fvals:
        return None, ""
    bridge = dar.get("bridge") or {}
    fcol = (dar.get("filter_column") or {}).get("column", "")
    dar_id = dar.get("_dar_id", "?")
    keys = "+".join(bridge.get("via_keys_from_to_mid") or [])
    path = (f"{bridge.get('from_table')}->{bridge.get('to_table')} "
            f"on {keys}")
    reach_disp = [
        r.get("value") for r in (dar.get("reachable_values") or [])
    ]
    if filter_op == "=":
        bad = fvals & unreachable
        if bad:
            v = sorted(bad)[0]
            return "refuse", (
                f"Filter {bridge.get('to_table')}.{fcol}='{v}' is "
                f"unreachable through {path}. Reachable values: "
                f"{reach_disp}. See {dar_id}."
            )
        return None, ""
    if filter_op == "IN":
        if fvals.issubset(unreachable):
            return "refuse", (
                f"Filter {bridge.get('to_table')}.{fcol} IN "
                f"{sorted(fvals)}: ALL values unreachable through "
                f"{path}. Reachable values: {reach_disp}. See {dar_id}."
            )
        if fvals & unreachable:
            return "warn", (
                f"Filter {bridge.get('to_table')}.{fcol} IN includes "
                f"unreachable value(s) {sorted(fvals & unreachable)} "
                f"alongside reachable through {path}. See {dar_id}."
            )
        return None, ""
    return None, ""


# --- public entry point ----------------------------------------------

def bridge_coverage_gate(
    sql: str,
    scope_tables: list[str],
    conn,
    dars_override: Optional[list[dict]] = None,
) -> tuple[bool, list[str], str]:
    """Refuse SQL whose filter values are empirically unreachable
    through chosen joins per bridge_coverage_by_filter DARs.

    Returns (passed, violations, gate_status):
      - passed: True if gate passes (no violations OR no DARs OR
        SQL unparseable OR no bridge match for any filter).
      - violations: human-readable list, attached to iteration_trace
        and analyst_review_reason on hard_stop.
      - gate_status: one of {"pass", "fail", "skipped_no_dars",
        "skipped_parse_error"}.

    `dars_override` bypasses the conn query (testability hook).
    """
    if dars_override is not None:
        dars = list(dars_override)
    else:
        dars = _load_bridge_coverage_dars(conn, scope_tables)
    if not dars:
        sys.stderr.write(
            "[WARN] bridge_coverage gate: no DARs found for scope; "
            "schema_discovery + bridge_coverage_by_filter analyzers "
            "have not run for this scope. Gate skipped.\n"
        )
        return True, [], "skipped_no_dars"

    cleaned = _strip_jinja_refs(sql or "")
    if not cleaned.strip():
        return True, [], "skipped_parse_error"
    try:
        parsed = sqlglot.parse_one(cleaned, dialect="duckdb")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(
            f"[INFO] bridge_coverage gate: SQL parse error: {e}\n"
        )
        return True, [], "skipped_parse_error"
    if parsed is None:
        return True, [], "skipped_parse_error"

    flat = _flatten_ctes(parsed)
    alias_map = _build_alias_to_table(flat)
    joins = _extract_join_chain(sql)
    filters = _extract_equality_filters(flat, alias_map)

    violations: list[str] = []
    for f in filters:
        bridge = _find_bridge_for_filter(joins, f["raw_table"])
        if bridge is None:
            continue
        matched = _match_dar(dars, bridge, f["column"])
        if not matched:
            continue
        for dar in matched:
            action, msg = _check_reachability(
                dar, f["operator"], f["values"],
            )
            if action == "refuse":
                violations.append(msg)
            elif action == "warn":
                sys.stderr.write(f"[WARN] bridge_coverage: {msg}\n")

    if violations:
        return False, violations, "fail"
    return True, [], "pass"


# --- OQ-3a Option β: conditional attestation check -------------------

def _check_bridge_coverage_attestation(
    propose: dict,
    conn,
    scope_tables: list[str],
    dars_override: Optional[list[dict]] = None,
) -> tuple[bool, Optional[str]]:
    """OQ-3a Option β: when bridge_coverage_by_filter DARs exist for
    the term's scope, the LLM MUST cite at least one in
    `bridge_coverage_consulted`. An empty list when DARs are present
    means the LLM ignored available evidence — discipline failure.

    Returns (ok, error_msg):
      - ok=True when no DARs exist (any attestation is fine) OR DARs
        exist AND attestation is non-empty.
      - ok=False, error_msg populated when DARs exist but
        `bridge_coverage_consulted` is empty/missing/non-list.

    Distinct from `bridge_coverage_gate`: that one catches "SQL filter
    is unreachable" (data-side); this one catches "LLM didn't cite the
    evidence it should have" (discipline-side). Both can fire on the
    same iteration; either is sufficient to hard-stop.
    """
    if dars_override is not None:
        dars = list(dars_override)
    else:
        dars = _load_bridge_coverage_dars(conn, scope_tables)
    if not dars:
        return True, None
    if not isinstance(propose, dict):
        return False, (
            "bridge_coverage_consulted attestation expected (DARs exist "
            "for scope) but propose response is not a dict."
        )
    consulted = propose.get("bridge_coverage_consulted")
    if consulted is None or not isinstance(consulted, list):
        return False, (
            "bridge_coverage_consulted is missing or not a list. "
            f"{len(dars)} bridge_coverage_by_filter DAR(s) exist for "
            "scope; LLM must cite at least one (or explicitly emit []  "
            "with reasoning_summary acknowledging non-relevance)."
        )
    if len(consulted) == 0:
        dar_ids = sorted({d.get("_dar_id", "?") for d in dars})[:5]
        return False, (
            f"bridge_coverage_consulted is empty but "
            f"{len(dars)} bridge_coverage_by_filter DAR(s) are in "
            f"scope (e.g. {dar_ids}). LLM must consult these to "
            "verify filter reachability."
        )
    return True, None
