"""Load structural domain facts into LLM prompts.

Reads `data/parquet/main_seeds/domain_facts.parquet` via an in-memory
DuckDB connection (Rule 18: never open cpe_analytics.duckdb directly
from long-lived processes). Returns a truncated, priority-ordered text
block ready to paste into a system prompt.

Typical use:

    from scripts._domain_context_loader import load_domain_context
    block = load_domain_context(
        scope_tables=['ekko', 'ekpo'],
        max_tokens=800,
        require_auto_inject=True,
    )
    system_prompt = f"{block}\n\n{existing_system_prompt}"

Returns an empty string when no facts match — callers should NOT render
a section header in that case. Never raises on missing file / empty
seed; returns "" instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
PARQUET_PATH = ROOT / "data" / "parquet" / "main_seeds" / "domain_facts.parquet"

# ~4 characters per token is a conservative English approximation; we
# prefer tiktoken when available for accuracy but avoid making it a hard
# dependency — none of the existing scripts require it.
try:  # pragma: no cover - optional dep
    import tiktoken as _tiktoken

    _ENCODER = _tiktoken.get_encoding("cl100k_base")

    def _count_tokens(text: str) -> int:
        return len(_ENCODER.encode(text))
except Exception:  # pragma: no cover - optional dep
    _ENCODER = None

    def _count_tokens(text: str) -> int:
        return max(1, len(text) // 4)


def _as_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def load_domain_context(
    scope_layer: "str | Iterable[str] | None" = None,
    scope_tables: "Iterable[str] | None" = None,
    categories: "Iterable[str] | None" = None,
    max_tokens: int = 800,
    require_auto_inject: bool = True,
) -> str:
    """Return a formatted block of structural domain facts or "".

    Filters:
      - status = 'active'
      - auto_inject = true (only when require_auto_inject=True)
      - scope_layer ∈ the given list (if provided)
      - scope_tables overlaps the given list (if provided; comma-splits
        the stored scope_tables and matches case-insensitively)
      - category ∈ the given list (if provided)

    Sorts by priority_score DESC, then fact_id ASC. Concatenates
    `[category] fact_technical` lines under a header. Truncates when
    `max_tokens` is reached (drops lowest-priority items first).
    """
    if not PARQUET_PATH.exists():
        return ""

    # Lazy import — keeps the module safe to import in environments
    # that haven't pip-installed duckdb yet (e.g. docs-only tooling).
    import duckdb

    layer_list = _as_list(scope_layer)
    # scope_tables semantics: None = no filter; [] = filter with zero
    # allowed tables = nothing matches; non-empty list = set-overlap.
    if scope_tables is None:
        table_list: "list[str] | None" = None
    else:
        table_list = [t.strip().lower() for t in _as_list(scope_tables) or [] if t and t.strip()]
        if not table_list:
            # Explicit empty scope → return "" without touching the seed.
            return ""
    category_list = _as_list(categories)

    # Status semantics:
    #   require_auto_inject=True  → strict: only 'active' + auto_inject=TRUE
    #   require_auto_inject=False → permissive: 'active' or 'draft' (for
    #                               Guided-Domain dedupe). Never includes
    #                               rejected / superseded / stale.
    params: list = []
    if require_auto_inject:
        where_parts = ["status = 'active'", "auto_inject = TRUE"]
    else:
        where_parts = ["status IN ('active', 'draft')"]
    if layer_list:
        placeholders = ",".join(["?"] * len(layer_list))
        where_parts.append(f"scope_layer IN ({placeholders})")
        params.extend(layer_list)
    if category_list:
        placeholders = ",".join(["?"] * len(category_list))
        where_parts.append(f"category IN ({placeholders})")
        params.extend(category_list)

    sql = f"""
        SELECT fact_id, category, scope_layer, scope_tables,
               fact_technical, priority_score
        FROM read_parquet('{PARQUET_PATH.as_posix()}')
        WHERE {' AND '.join(where_parts)}
        ORDER BY priority_score DESC NULLS LAST, fact_id ASC
    """

    conn = duckdb.connect(":memory:")
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        return ""
    finally:
        conn.close()

    if not rows:
        return ""

    # Scope-tables filter is applied in Python so callers can pass
    # e.g. 'ekko' and match a stored 'EKKO, EKPO' string.
    if table_list:
        filtered = []
        for fact_id, category, scope_layer_v, stored_tables, fact_technical, priority in rows:
            stored = [t.strip().lower() for t in (stored_tables or "").split(",") if t.strip()]
            if any(t in stored for t in table_list):
                filtered.append((fact_id, category, scope_layer_v, stored_tables, fact_technical, priority))
        rows = filtered
        if not rows:
            return ""

    header = "## Known domain facts (structural observations)"
    lines: list[str] = [header]
    token_budget = max_tokens - _count_tokens(header)
    if token_budget <= 0:
        return ""

    for fact_id, category, _layer, _tables, fact_technical, _priority in rows:
        text = (fact_technical or "").strip()
        if not text:
            continue
        line = f"- [{category}] {text}"
        cost = _count_tokens(line) + 1  # +1 for the joining newline
        if cost > token_budget:
            break
        lines.append(line)
        token_budget -= cost

    if len(lines) == 1:  # only the header survived
        return ""
    return "\n".join(lines)


if __name__ == "__main__":
    # Sanity check for the scope_tables filter + priority_score ordering.
    # Writes a temporary parquet file, exercises the four documented cases,
    # and prints PASS/FAIL — does NOT touch the real domain_facts parquet.
    import tempfile

    import duckdb as _duck

    cases_passed = 0
    cases_total = 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_parquet = Path(tmp) / "domain_facts.parquet"

        # Build a tiny table matching the real schema enough for the loader.
        _c = _duck.connect(":memory:")
        _c.execute("""
            CREATE TABLE domain_facts AS
            SELECT 'DF-A' AS fact_id, 'currency' AS category,
                   'staging' AS scope_layer, 'ekko,ekpo' AS scope_tables,
                   'Fact A — currency distribution in POs' AS fact_technical,
                   80 AS priority_score, TRUE AS auto_inject,
                   'active' AS status
            UNION ALL SELECT 'DF-B', 'naming_convention', 'staging', 'mara',
                   'Fact B — material naming convention', 90, TRUE, 'active'
        """)
        _c.execute(f"COPY domain_facts TO '{tmp_parquet.as_posix()}' (FORMAT PARQUET)")
        _c.close()

        # Redirect module-level path at this one test to the tmp parquet
        import _domain_context_loader as _m  # type: ignore[import-not-found]
        _orig = _m.PARQUET_PATH
        _m.PARQUET_PATH = tmp_parquet
        try:
            checks = [
                ("scope=['ekko'] → A only",
                 {"scope_tables": ["ekko"]},
                 lambda s: "DF-A" not in s  # fact_technical doesn't embed fact_id
                 or True),  # placeholder; real check below
            ]
            # Case 1: scope=['ekko'] → only A
            block = _m.load_domain_context(scope_tables=["ekko"], max_tokens=800, require_auto_inject=True)
            cases_total += 1
            if "Fact A" in block and "Fact B" not in block:
                cases_passed += 1
                print("PASS  scope=['ekko']: Fact A present, Fact B absent")
            else:
                print(f"FAIL  scope=['ekko']: got {block!r}")

            # Case 2: scope=['mara','ekko'] → B first (priority 90), then A (80)
            block2 = _m.load_domain_context(scope_tables=["mara", "ekko"], max_tokens=800, require_auto_inject=True)
            cases_total += 1
            if block2.find("Fact B") != -1 and block2.find("Fact A") != -1 and block2.find("Fact B") < block2.find("Fact A"):
                cases_passed += 1
                print("PASS  scope=['mara','ekko']: B before A by priority")
            else:
                print(f"FAIL  scope=['mara','ekko']: got {block2!r}")

            # Case 3: scope=None → all, priority-ordered
            block3 = _m.load_domain_context(scope_tables=None, max_tokens=800, require_auto_inject=True)
            cases_total += 1
            if block3.find("Fact B") != -1 and block3.find("Fact A") != -1 and block3.find("Fact B") < block3.find("Fact A"):
                cases_passed += 1
                print("PASS  scope=None: B before A by priority")
            else:
                print(f"FAIL  scope=None: got {block3!r}")

            # Case 4: scope=[] → "" (empty filter list = no matches)
            block4 = _m.load_domain_context(scope_tables=[], max_tokens=800, require_auto_inject=True)
            cases_total += 1
            if block4 == "":
                cases_passed += 1
                print("PASS  scope=[]: empty string")
            else:
                print(f"FAIL  scope=[]: expected empty, got {block4!r}")

        finally:
            _m.PARQUET_PATH = _orig

    print(f"\n{cases_passed}/{cases_total} sanity checks passed")
    import sys as _sys
    _sys.exit(0 if cases_passed == cases_total else 1)
