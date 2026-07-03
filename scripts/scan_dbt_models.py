"""Scan all dbt models and extract column-level lineage into seed CSVs.

Reads every .sql file in dbt/models/ and extracts:
  - Model name, layer, materialization, unique_key
  - Source refs (`{{ source() }}` and `{{ ref() }}`)
  - Column names from the final SELECT
  - Transformation type per column (direct / cast / case_when / hash_key / aggregation / ...)
  - Joins and WHERE-clause filters
  - Description from the nearest schema.yml model entry or a leading SQL comment

Writes three seeds with LF line endings so dbt-duckdb's CSV sniffer is happy:
  - dbt/seeds/dbt_model_catalog.csv    (one row per model)
  - dbt/seeds/dbt_column_lineage.csv   (one row per column per model)
  - dbt/seeds/data_vault_design.csv    (reconciled against actual vault models)

The vault-design reconciliation APPENDS a row for every hub/link/satellite
in dbt/models/vault/ that the seed does not already describe, with
inferred business_key / source_tables / notes. Existing rows are never
overwritten (only blank fields are back-filled). Orphan rows — seed
entries whose model no longer exists on disk — are reported as warnings
but preserved, to protect against a typo wiping documentation.

Usage: python scripts/scan_dbt_models.py
"""
import csv
import re
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
DBT_DIR = ROOT / "dbt"
MODELS_DIR = DBT_DIR / "models"
SEEDS_DIR = DBT_DIR / "seeds"


# ----------------------- small parsers -----------------------

def extract_refs(sql: str):
    return re.findall(r"\{\{\s*ref\s*\(\s*['\"](\w+)['\"]\s*\)\s*\}\}", sql)


def extract_sources(sql: str):
    return re.findall(
        r"\{\{\s*source\s*\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]\s*\)\s*\}\}",
        sql,
    )


def extract_materialization(sql: str) -> str:
    m = re.search(r"materialized\s*=\s*['\"](\w+)['\"]", sql)
    return m.group(1) if m else "view"


def extract_unique_key(sql: str) -> str:
    m = re.search(r"unique_key\s*=\s*['\"]([^'\"]+)['\"]", sql)
    if m:
        return m.group(1)
    m = re.search(r"unique_key\s*=\s*\[([^\]]+)\]", sql)
    if m:
        return "; ".join(re.findall(r"['\"](\w+)['\"]", m.group(1)))
    return ""


def extract_grain_from_comments(sql: str) -> str:
    m = re.search(r"[Gg]rain:\s*(.+?)(?:\n|\*/)", sql)
    return m.group(1).strip()[:200] if m else ""


def extract_description(sql: str) -> str:
    m = re.search(r"/\*\s*\n?\s*(.*?)(?:\n\s*\n|\*/)", sql, re.DOTALL)
    if not m:
        return ""
    for line in m.group(1).strip().split("\n"):
        line = line.strip().lstrip("*").strip()
        if line and not line.startswith("{") and not line.startswith("config"):
            return line[:300]
    return ""


_SQL_KEYWORDS = {
    "ON", "WHERE", "USING", "LEFT", "RIGHT", "INNER", "FULL", "CROSS",
    "JOIN", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION", "AND", "OR",
    "AS", "FROM",
}

# Tokens that appear inside expressions but are never column names. Used by
# `_extract_bare_identifier` to find the actual source column hiding inside a
# transformation like CASE WHEN BEDAT ... END or MIN(posting_date).
_NON_COLUMN_TOKENS = {
    "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "NULL", "IS",
    "TRUE", "FALSE", "MIN", "MAX", "SUM", "AVG", "COUNT", "COALESCE", "NULLIF",
    "IFNULL", "CAST", "AS", "DISTINCT", "SUBSTR", "SUBSTRING", "TRIM", "LENGTH",
    "LEAD", "LAG", "ROUND", "OVER", "PARTITION", "BY", "ROW_NUMBER", "DATEDIFF",
    "DATE_TRUNC", "EXTRACT", "ON", "WHERE", "FROM", "JOIN", "LEFT", "RIGHT",
    "INNER", "FULL", "CROSS", "IF", "SELECT", "GROUP", "ORDER", "HAVING",
    "LIMIT", "UNION", "INTEGER", "INT", "BIGINT", "SMALLINT", "DECIMAL", "DATE",
    "DATETIME", "TIMESTAMP", "VARCHAR", "CHAR", "BOOLEAN", "TEXT", "NUMERIC",
    "CURRENT_TIMESTAMP", "CURRENT_DATE", "INTERVAL", "DAY", "MONTH", "YEAR",
    "QUARTER", "WEEK", "HOUR", "MINUTE", "SECOND", "LIKE", "IN", "BETWEEN",
    "EXISTS", "NOT_NULL",
}


def _extract_bare_identifier(expression: str) -> str:
    """Return the first identifier in `expression` that isn't a SQL keyword,
    type name, or literal. Used to recover the underlying source column from
    transformations like CASE WHEN BEDAT IS NOT NULL ... END or MIN(posting_date)
    where no qualified `alias.column` reference exists.

    For CASE WHEN expressions we prefer identifiers that appear after the first
    `THEN` keyword — the pattern `CASE WHEN movement_type = '101' THEN quantity
    END` really means "yield quantity", so `quantity` is the semantic origin.
    """
    clean = re.sub(r"'[^']*'", "", expression)

    def _first_non_keyword(text: str) -> str:
        for m in re.finditer(r"(?<![.'])\b([a-zA-Z_][a-zA-Z0-9_]*)\b", text):
            tok = m.group(1)
            if tok.upper() in _NON_COLUMN_TOKENS:
                continue
            return tok
        return ""

    then_match = re.search(r"\bTHEN\b\s+", clean, re.IGNORECASE)
    if then_match:
        preferred = _first_non_keyword(clean[then_match.end():])
        if preferred:
            return preferred
    return _first_non_keyword(clean)


def extract_join_aliases(sql: str) -> dict:
    """Parse table aliases from FROM/JOIN clauses — both `{{ ref('x') }} alias`
    and `<cte_or_name> alias` forms. Keyword-like aliases are filtered out."""
    aliases: dict = {}

    # Form 1: FROM/JOIN {{ ref('model') }} alias
    ref_pattern = (
        r"(?:FROM|JOIN)\s+\{\{\s*ref\s*\(\s*['\"](\w+)['\"]\s*\)\s*\}\}"
        r"(?:\s+AS)?\s+(\w+)(?=\s|,|$|\n)"
    )
    for m in re.finditer(ref_pattern, sql, re.IGNORECASE):
        model_name, alias = m.group(1), m.group(2)
        if alias.upper() in _SQL_KEYWORDS:
            continue
        aliases[alias] = model_name

    # Form 2: FROM/JOIN <identifier> <alias> ON ...  (identifier is usually a CTE name)
    clean = _strip_comments_and_jinja(sql)
    bare_pattern = r"(?:FROM|JOIN)\s+(\w+)(?:\s+AS)?\s+(\w+)(?=\s+ON\b|\s*$|\s*\n)"
    for m in re.finditer(bare_pattern, clean, re.IGNORECASE):
        target, alias = m.group(1), m.group(2)
        if alias.upper() in _SQL_KEYWORDS or target.upper() in _SQL_KEYWORDS:
            continue
        # Don't overwrite a ref-form alias with a bare one
        if alias not in aliases:
            aliases[alias] = target

    return aliases


def extract_ctes(sql: str):
    """Return `(alias_to_ref, alias_to_body)` where:

    - alias_to_ref maps each CTE alias to its underlying `{{ ref('model') }}`
      target (resolving chained aliases)
    - alias_to_body stores each CTE's raw body text, so we can recurse into it
      if the final SELECT is `SELECT * FROM <alias>`
    """
    # Mask single-line comments with spaces so `-- foo` between CTEs doesn't
    # break the `,\s+<alias>\s+AS (` header-detection regex. Preserving string
    # length keeps slice offsets valid for body extraction from the original.
    masked = re.sub(r"--[^\n]*", lambda m: " " * len(m.group(0)), sql)
    aliases: dict = {}
    bodies: dict = {}
    i = 0
    while True:
        m = re.search(r"(?:WITH|,)\s+(\w+)\s+AS\s*\(", masked[i:], re.IGNORECASE)
        if not m:
            break
        alias = m.group(1)
        body_start = i + m.end()
        depth = 1
        j = body_start
        while j < len(masked) and depth > 0:
            c = masked[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            j += 1
        body = sql[body_start: j - 1]
        bodies[alias] = body

        ref_match = re.search(r"\{\{\s*ref\s*\(\s*['\"](\w+)['\"]\s*\)\s*\}\}", body)
        if ref_match:
            aliases[alias] = ref_match.group(1)
        else:
            from_match = re.search(r"\bFROM\s+(\w+)\b", body, re.IGNORECASE)
            if from_match and from_match.group(1) in aliases:
                aliases[alias] = aliases[from_match.group(1)]
        i = j

    # Resolve alias chains (a → b → c → model)
    for _ in range(5):
        changed = False
        for k, v in list(aliases.items()):
            if v in aliases and aliases[v] != v:
                aliases[k] = aliases[v]
                changed = True
        if not changed:
            break

    return aliases, bodies


def extract_cte_aliases(sql: str) -> dict:
    """Backward-compatible wrapper — returns only alias → ref map."""
    return extract_ctes(sql)[0]


def _strip_comments_and_jinja(sql: str) -> str:
    """Drop inline/block comments and neutralise jinja before regex parsing."""
    clean = re.sub(r"\{\{.*?\}\}", "JINJA_EXPR", sql, flags=re.DOTALL)
    clean = re.sub(r"\{%.*?%\}", "", clean, flags=re.DOTALL)
    clean = re.sub(r"--[^\n]*", "", clean)
    clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL)
    return clean


def _split_projection(projection_text: str):
    """Split a SELECT projection list on top-level commas."""
    depth = 0
    buf = ""
    out = []
    for ch in projection_text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(buf.strip())
            buf = ""
            continue
        buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def _find_top_level_select(text: str):
    """Walk the text and return the FIRST top-level `SELECT ... FROM` block.

    "Top-level" means any SELECT that is not inside a parenthesised subquery —
    so EXISTS/IN subqueries and CTE bodies are ignored. Returns the projection
    substring (between SELECT and FROM) plus the remainder of the query from
    FROM onwards, or (None, None) if nothing matches.
    """
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            i += 1
            continue
        if depth == 0 and text[i: i + 7].upper() == "SELECT " or (
            depth == 0 and text[i: i + 7].upper().startswith("SELECT") and i + 6 < len(text) and text[i + 6] in " \t\n"
        ):
            # Find the matching FROM at the same top-level depth
            j = i + 6
            inner_depth = 0
            while j < len(text):
                c = text[j]
                if c == "(":
                    inner_depth += 1
                elif c == ")":
                    inner_depth -= 1
                    if inner_depth < 0:
                        break
                elif (
                    inner_depth == 0
                    and text[j: j + 5].upper() == "FROM "
                    and (j == 0 or text[j - 1] in " \t\n")
                ):
                    projection = text[i + 6: j].strip()
                    remainder = text[j:]
                    return projection, remainder
                j += 1
            return None, None
        i += 1
    return None, None


def _resolve_final_projection(sql_clean: str, cte_bodies: dict):
    """Return the projection text that should be treated as the model's output.

    If the top-level SELECT is `SELECT * FROM <cte>`, recurse into that CTE's
    body. Caps recursion at 6 to avoid cycles.
    """
    projection, remainder = _find_top_level_select(sql_clean)
    for _ in range(6):
        if projection is None:
            return None, None
        stripped = projection.strip().lstrip("*").strip()
        is_star = stripped == "" or re.fullmatch(r"(\w+\s*\.\s*)?\*", projection.strip())
        if not is_star:
            return projection, remainder
        # Follow the FROM target
        from_match = re.search(r"\bFROM\s+(\w+)", remainder or "", re.IGNORECASE)
        if not from_match:
            return projection, remainder
        target = from_match.group(1)
        if target not in cte_bodies:
            return projection, remainder
        sub_clean = _strip_comments_and_jinja(cte_bodies[target])
        projection, remainder = _find_top_level_select(sub_clean)
    return projection, remainder


def _resolve_cte_column(col_name: str, cte_name: str, cte_bodies: dict,
                        alias_map: dict, depth: int = 0):
    """Walk a CTE body projection to find the defining expression of `col_name`.

    Returns a 4-tuple ``(origin_table, origin_column, expression, chain)``
    where:
      - *origin_table/column*: deepest source (for lineage tracing)
      - *expression*: outermost non-trivial SQL expression (for
        ``transformation_type`` classification)
      - *chain*: list of ALL non-trivial expressions, deepest first,
        stored as semicolon-delimited string in the lineage CSV
    """
    if depth > 6 or not cte_name or cte_name not in cte_bodies:
        return None

    body = cte_bodies[cte_name]
    body_clean = _strip_comments_and_jinja(body)
    proj, rem = _find_top_level_select(body_clean)
    if not proj:
        return None

    proj = re.sub(r"^\s*DISTINCT\s+", "", proj.strip(), flags=re.IGNORECASE)

    # Local alias maps for this CTE body (join aliases + refs in this body only)
    body_join_aliases = extract_join_aliases(body)
    body_refs = extract_refs(body)
    body_sources = extract_sources(body)
    body_sole_ref = body_refs[0] if len(body_refs) == 1 else None
    body_sole_source = (
        f"{body_sources[0][0]}.{body_sources[0][1]}"
        if len(body_sources) == 1 else None
    )
    body_primary_from = None
    if rem:
        fm = re.search(r"\bFROM\s+(\w+)", rem, re.IGNORECASE)
        if fm:
            body_primary_from = fm.group(1)

    def _resolve_through(src_alias: str, src_col: str, cte_expr=None):
        """Given (alias, col) appearing inside this CTE body, walk to the origin."""
        target = (
            body_join_aliases.get(src_alias)
            or alias_map.get(src_alias)
            or src_alias
        )
        if target in cte_bodies:
            inner = _resolve_cte_column(src_col, target, cte_bodies, alias_map, depth + 1)
            if inner:
                i_table, i_col, i_expr, i_chain = inner
                # Prefer outermost non-trivial expression for classification
                use_expr = cte_expr if cte_expr else i_expr
                # Build chain: inner chain + this level (if non-trivial)
                chain = list(i_chain)
                if cte_expr:
                    chain.append(cte_expr)
                return (i_table, i_col, use_expr, chain)
            return (target, src_col, cte_expr, [cte_expr] if cte_expr else [])
        return (target, src_col, cte_expr, [cte_expr] if cte_expr else [])

    def _resolve_bare(bare_col: str, cte_expr=None):
        """Bare identifier with no alias qualifier — look up its source."""
        if body_primary_from and body_primary_from in cte_bodies:
            inner = _resolve_cte_column(bare_col, body_primary_from, cte_bodies, alias_map, depth + 1)
            if inner:
                i_table, i_col, i_expr, i_chain = inner
                use_expr = cte_expr if cte_expr else i_expr
                chain = list(i_chain)
                if cte_expr:
                    chain.append(cte_expr)
                return (i_table, i_col, use_expr, chain)
        if body_sole_ref:
            return (body_sole_ref, bare_col, cte_expr, [cte_expr] if cte_expr else [])
        if body_sole_source:
            return (body_sole_source, bare_col, cte_expr, [cte_expr] if cte_expr else [])
        return None

    for expr in _split_projection(proj):
        expr_s = expr.strip()
        if not expr_s or expr_s.startswith("JINJA"):
            continue
        am = re.search(r"\bAS\s+(\w+)\s*$", expr_s, re.IGNORECASE)
        if am:
            this_alias = am.group(1)
            inner_expr = expr_s[: am.start()].strip()
        else:
            parts = expr_s.split(".")
            this_alias = parts[-1].strip()
            inner_expr = expr_s
        if this_alias != col_name:
            continue

        # 1. Simple alias.col reference — trivial, no CTE expression
        sm = re.match(r"^(\w+)\.(\w+)$", inner_expr)
        if sm:
            return _resolve_through(sm.group(1), sm.group(2))

        # 2. Bare identifier — trivial
        if re.match(r"^\w+$", inner_expr):
            return _resolve_bare(inner_expr)

        # 3. Transformation — inner_expr is the real CTE expression
        refs_in = re.findall(r"(\w+)\.(\w+)", inner_expr)
        if refs_in:
            return _resolve_through(refs_in[0][0], refs_in[0][1], cte_expr=inner_expr)
        bare = _extract_bare_identifier(inner_expr)
        if bare:
            return _resolve_bare(bare, cte_expr=inner_expr)
        return None

    # Column not found in the CTE projection — could be a SELECT *
    if body_sole_ref:
        return (body_sole_ref, col_name, None, [])
    if body_sole_source:
        return (body_sole_source, col_name, None, [])
    return None


def extract_columns_from_select(sql: str, cte_aliases: dict = None, cte_bodies: dict = None,
                                 join_aliases: dict = None):
    """Return a list of {column_name, expression, origin_table, origin_column,
    transformation_type} dicts for the model's output columns.

    Walks top-level SELECTs, skipping nested subqueries (EXISTS, IN, scalar).
    When the top-level final SELECT is `SELECT * FROM <cte>`, recurse into the
    CTE body. Resolves CTE aliases to their underlying `ref('model')` target.
    """
    columns = []
    cte_aliases = cte_aliases or {}
    cte_bodies = cte_bodies or {}
    join_aliases = join_aliases or {}

    clean = _strip_comments_and_jinja(sql)
    projection, remainder = _resolve_final_projection(clean, {
        alias: _strip_comments_and_jinja(body) for alias, body in cte_bodies.items()
    })
    if not projection:
        # Fall back to the old "last SELECT in the file" heuristic
        fallbacks = list(re.finditer(r"\bSELECT\b\s+(.*?)\bFROM\b", clean, re.IGNORECASE | re.DOTALL))
        if not fallbacks:
            return columns
        projection = fallbacks[-1].group(1)
        remainder = clean[fallbacks[-1].end():]

    # Strip leading DISTINCT so `SELECT DISTINCT col, ...` parses cleanly —
    # without this the first column ends up named `DISTINCT\n    col`.
    projection = re.sub(r"^\s*DISTINCT\s+", "", projection.strip(), flags=re.IGNORECASE)

    # Capture the FROM target of the final SELECT so we can recurse into the
    # CTE it reads from when a column is just a bare identifier (e.g.
    # `SELECT vendor_id FROM src` inside link_po_vendor).
    final_from = None
    if remainder:
        fm = re.search(r"\bFROM\s+(\w+)", remainder, re.IGNORECASE)
        if fm:
            final_from = fm.group(1)

    expressions = _split_projection(projection)

    for expr in expressions:
        expr = expr.strip()
        if not expr or expr == "*" or expr.startswith("JINJA"):
            continue

        alias_match = re.search(r"\bAS\s+(\w+)\s*$", expr, re.IGNORECASE)
        if alias_match:
            alias = alias_match.group(1)
            expression = expr[: alias_match.start()].strip()
        else:
            parts = expr.split(".")
            alias = parts[-1].strip()
            expression = expr

        if "JINJA" in alias:
            continue

        origin_table = ""
        origin_column = ""

        simple = re.match(r"^(\w+)\.(\w+)$", expression)
        if simple:
            origin_table = simple.group(1)
            origin_column = simple.group(2)
            transformation = "direct"
        elif re.match(r"^\w+$", expression):
            origin_column = expression
            transformation = "direct"
        else:
            refs = re.findall(r"(\w+)\.(\w+)", expression)
            if refs:
                origin_table = refs[0][0]
                origin_column = refs[0][1]
            else:
                # Transformation with no qualified ref (CASE WHEN BEDAT...,
                # MIN(posting_date), CAST(x AS ...)). Extract the first bare
                # identifier that looks like a column name.
                bare = _extract_bare_identifier(expression)
                if bare:
                    origin_column = bare

            if re.search(r"\bCASE\b", expression, re.IGNORECASE):
                transformation = "case_when"
            elif re.search(r"\bMD5\b|\bCONCAT_WS\b|\bhash_key\b", expression, re.IGNORECASE):
                transformation = "hash_key"
            elif re.search(r"\bCAST\b|\bSUBSTR\b|\bSUBSTRING\b|\bTRIM\b", expression, re.IGNORECASE):
                transformation = "type_cast"
            elif re.search(r"\bCOALESCE\b|\bNULLIF\b|\bIFNULL\b", expression, re.IGNORECASE):
                transformation = "coalesce"
            elif re.search(r"\bSUM\b|\bAVG\b|\bCOUNT\b|\bMIN\b|\bMAX\b", expression, re.IGNORECASE):
                transformation = "aggregation"
            elif re.search(r"\bROUND\b|\bLEAD\b|\bLAG\b|\bROW_NUMBER\b|\bOVER\s*\(", expression, re.IGNORECASE):
                transformation = "window_function"
            elif re.search(r"\bDATEDIFF\b|\bDATE_TRUNC\b|\bEXTRACT\b", expression, re.IGNORECASE):
                transformation = "date_calc"
            elif "CURRENT_TIMESTAMP" in expression.upper() or "CURRENT_DATE" in expression.upper():
                transformation = "system_generated"
            elif re.match(r"^'[^']*'$", expression):
                transformation = "constant"
            else:
                transformation = "derived"

        # Deep CTE body resolution FIRST — walk into the nearest CTE body
        # before the flat alias map is applied, so we don't lose the
        # intermediate CTE step that knows the real expression. If the
        # column came via a join alias whose immediate target is a CTE
        # (e.g. `fgr` → `first_gr`), resolve through that CTE body. Falls
        # back to bare-column-from-CTE when the final SELECT's FROM target
        # is a CTE and no alias was captured.
        resolve_target = None
        if origin_table in cte_bodies:
            resolve_target = origin_table
        elif origin_table in join_aliases and join_aliases[origin_table] in cte_bodies:
            resolve_target = join_aliases[origin_table]
        elif not origin_table and final_from and final_from in cte_bodies:
            resolve_target = final_from

        transformation_chain = ""
        if resolve_target and origin_column:
            resolved = _resolve_cte_column(
                origin_column, resolve_target, cte_bodies, cte_aliases or {}
            )
            if resolved:
                r_table, r_col, cte_expr, cte_chain = resolved
                if r_table:
                    origin_table = r_table
                if r_col:
                    origin_column = r_col
                # Store the full chain (deepest → outermost, semicolon-delimited)
                if cte_chain:
                    transformation_chain = ";".join(cte_chain)
                # Re-classify ONLY when the outer SELECT was 'direct' (bare alias
                # hiding a CTE transformation). Non-direct outer expressions
                # (case_when, coalesce, etc.) already have the correct business-
                # level classification and must not be overridden.
                if (cte_expr and transformation == "direct"
                        and not re.match(r"^\w+(\.\w+)?$", cte_expr.strip())):
                    expression = cte_expr
                    if re.search(r"\bCASE\b", cte_expr, re.IGNORECASE):
                        transformation = "case_when"
                    elif re.search(r"\bMD5\b|\bCONCAT_WS\b|\bhash_key\b", cte_expr, re.IGNORECASE):
                        transformation = "hash_key"
                    elif re.search(r"\bSUM\b|\bAVG\b|\bCOUNT\b|\bMIN\b|\bMAX\b", cte_expr, re.IGNORECASE):
                        transformation = "aggregation"
                    elif re.search(r"\bDATEDIFF\b|\bDATE_TRUNC\b|\bEXTRACT\b", cte_expr, re.IGNORECASE):
                        transformation = "date_calc"
                    elif re.search(r"\bCAST\b|\bSUBSTR\b|\bSUBSTRING\b|\bTRIM\b", cte_expr, re.IGNORECASE):
                        transformation = "type_cast"
                    elif re.search(r"\bCOALESCE\b|\bNULLIF\b|\bIFNULL\b", cte_expr, re.IGNORECASE):
                        transformation = "coalesce"
                    elif re.search(r"\bROUND\b|\bLEAD\b|\bLAG\b|\bROW_NUMBER\b|\bOVER\s*\(", cte_expr, re.IGNORECASE):
                        transformation = "window_function"
                    elif "CURRENT_TIMESTAMP" in cte_expr.upper() or "CURRENT_DATE" in cte_expr.upper():
                        transformation = "system_generated"
                    else:
                        transformation = "derived"

        # Append the outer SELECT expression as the final chain step when
        # it's non-trivial (CASE WHEN, COALESCE, arithmetic, etc.) and
        # not already the only step in the chain. This captures the
        # business-level transformation that lives in the outer SELECT
        # rather than inside a CTE.
        if (transformation_chain
                and transformation != "direct"
                and not re.match(r"^\w+(\.\w+)?$", expression.strip())):
            chain_parts = transformation_chain.split(";")
            # Only append if the outer expression isn't already the last step
            if chain_parts[-1].strip() != expression.strip():
                transformation_chain = transformation_chain + ";" + expression

        # Fallback: resolve flat alias map (join aliases chained through
        # CTE aliases to their final ref) for cases the deep resolver
        # didn't touch (e.g. `h.vendor_id` where h is a plain join alias
        # onto a model ref, not a CTE).
        if cte_aliases and origin_table in cte_aliases:
            origin_table = cte_aliases[origin_table]

        columns.append(
            {
                "column_name": alias,
                "expression": expression[:2000],
                "origin_table": origin_table,
                "origin_column": origin_column,
                "transformation_type": transformation,
                "transformation_chain": transformation_chain[:2000],
            }
        )

    return columns


def extract_joins(sql: str) -> str:
    clean = _strip_comments_and_jinja(sql)
    joins = []
    pattern = (
        r"(LEFT\s+|RIGHT\s+|INNER\s+|FULL\s+|CROSS\s+)?JOIN\s+(\S+)\s+(\w+)?"
        r"\s*ON\s+(.*?)(?=(?:LEFT|RIGHT|INNER|FULL|CROSS)?\s*JOIN|\bWHERE\b"
        r"|\bGROUP\b|\bORDER\b|\bLIMIT\b|$)"
    )
    for m in re.finditer(pattern, clean, re.IGNORECASE | re.DOTALL):
        joins.append(f"{(m.group(1) or 'INNER').strip()} JOIN {m.group(2)}")
    return "; ".join(joins)[:300]


def extract_filters(sql: str) -> str:
    clean = _strip_comments_and_jinja(sql)
    m = re.search(
        r"\bWHERE\b\s+(.*?)(?=\bGROUP\b|\bORDER\b|\bLIMIT\b|\bHAVING\b|$)",
        clean, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    parts = re.split(r"\bAND\b", m.group(1), flags=re.IGNORECASE)
    return "; ".join(p.strip()[:100] for p in parts if p.strip())[:300]


def determine_layer(filepath: Path) -> str:
    rel = filepath.relative_to(MODELS_DIR).parts
    if rel:
        layer = rel[0]
        if layer in ("staging", "vault", "marts", "obt", "knowledge"):
            return layer
    return "other"


def load_schema_descriptions() -> dict:
    """Collect descriptions from every schema.yml under models/. Optional."""
    if yaml is None:
        return {}
    descriptions: dict = {}
    for schema_file in MODELS_DIR.rglob("schema.yml"):
        try:
            with open(schema_file, "r", encoding="utf-8") as f:
                schema = yaml.safe_load(f)
            if not schema or "models" not in schema:
                continue
            for model in schema["models"]:
                name = model.get("name", "")
                col_descs = {
                    col.get("name", ""): col.get("description", "") or ""
                    for col in model.get("columns", []) or []
                }
                descriptions[name] = {
                    "description": model.get("description", "") or "",
                    "columns": col_descs,
                }
        except Exception as e:
            print(f"  [warn] failed to parse {schema_file.name}: {e}")
    return descriptions


# ----------------------- main entry point -----------------------

def scan_all_models():
    print("Scanning dbt models...")
    schema_descs = load_schema_descriptions()

    model_catalog = []
    column_lineage = []
    model_id = 0
    col_id = 0

    for sql_file in sorted(MODELS_DIR.rglob("*.sql")):
        # Skip the archive folder — archived models must not appear in
        # scan output (otherwise dbt_column_lineage / dbt_model_catalog
        # would list them under "other", breaking the clean-baseline
        # invariant after `run_archive`).
        if "archive" in sql_file.relative_to(MODELS_DIR).parts:
            continue
        model_id += 1
        model_name = sql_file.stem
        layer = determine_layer(sql_file)
        relative_path = str(sql_file.relative_to(DBT_DIR))

        with open(sql_file, "r", encoding="utf-8") as f:
            sql = f.read()

        materialization = extract_materialization(sql)
        unique_key = extract_unique_key(sql)
        refs = extract_refs(sql)
        sources = extract_sources(sql)
        joins = extract_joins(sql)
        filters = extract_filters(sql)
        grain = extract_grain_from_comments(sql)
        description = extract_description(sql)
        cte_aliases, cte_bodies = extract_ctes(sql)
        join_aliases = extract_join_aliases(sql)

        # Build the full alias→model lookup and resolve transitively so that
        # `pm → po_material → link_po_material` collapses into `pm → link_po_material`
        merged = dict(join_aliases)
        for a, v in cte_aliases.items():
            if a not in merged:
                merged[a] = v
        for _ in range(6):
            changed = False
            for k, v in list(merged.items()):
                if v in merged and merged[v] != v and merged[v] != k:
                    merged[k] = merged[v]
                    changed = True
                elif v in cte_aliases and cte_aliases[v] != v:
                    merged[k] = cte_aliases[v]
                    changed = True
            if not changed:
                break

        columns = extract_columns_from_select(
            sql,
            cte_aliases=merged,
            cte_bodies=cte_bodies,
            join_aliases=join_aliases,
        )

        # Back-fill empty origin_table when the model has exactly one ref.
        # Satellites and staging models typically do `SELECT BEDAT AS po_date`
        # — the column extractor captures the origin column name but has
        # nothing to hang the model on because the expression lacks a table
        # prefix. If there's only one upstream `ref()`, every empty
        # origin_table is unambiguously that ref.
        sole_ref = None
        if len(refs) == 1:
            sole_ref = refs[0]
        elif len(refs) > 1:
            # Prefer a stg_sap__* ref when multiple refs exist (common for
            # vault satellites that also ref their parent hub for FK)
            stg_refs = [r for r in refs if r.startswith("stg_sap__")]
            if len(stg_refs) == 1:
                sole_ref = stg_refs[0]
        if sole_ref:
            for col in columns:
                if not col.get("origin_table") and col.get("origin_column"):
                    col["origin_table"] = sole_ref

        # Back-fill staging-layer columns with the raw SAP source they read
        # from. Staging models use `{{ source('raw_sap', 'table') }}` not
        # `{{ ref() }}`, so `sole_ref` above never fires for them. Without
        # this, every staging column has origin_table=NULL and the Business
        # Glossary trace walker cannot extend from staging to SOURCE.
        if not sole_ref and len(sources) == 1:
            src_schema, src_table = sources[0]
            src_origin = f"{src_schema}.{src_table}"
            for col in columns:
                if not col.get("origin_table"):
                    col["origin_table"] = src_origin
                    if not col.get("origin_column"):
                        col["origin_column"] = col["column_name"]

        schema_info = schema_descs.get(model_name, {})
        if not description and schema_info.get("description"):
            description = schema_info["description"]

        model_catalog.append(
            {
                "id": f"M{model_id:03d}",
                "model_name": model_name,
                "layer": layer,
                "file_path": relative_path,
                "materialization": materialization,
                "unique_key": unique_key,
                "description": description[:300],
                "grain": grain,
                "source_tables": "; ".join(f"{s[0]}.{s[1]}" for s in sources),
                "ref_models": "; ".join(refs),
                "joins": joins,
                "filters": filters,
                "column_count": len(columns),
                "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
        )

        for col in columns:
            col_id += 1
            col_schema_desc = schema_info.get("columns", {}).get(col["column_name"], "")
            column_lineage.append(
                {
                    "id": f"CL{col_id:04d}",
                    "model_name": model_name,
                    "layer": layer,
                    "column_name": col["column_name"],
                    "expression": col["expression"],
                    "origin_table": col["origin_table"],
                    "origin_column": col["origin_column"],
                    "transformation_type": col["transformation_type"],
                    "transformation_chain": col.get("transformation_chain", ""),
                    "description": col_schema_desc[:200] if col_schema_desc else "",
                }
            )

        print(f"  {layer}/{model_name}: {len(columns)} cols, {len(refs)} refs, {len(sources)} sources")

    catalog_path = SEEDS_DIR / "dbt_model_catalog.csv"
    with open(catalog_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "id", "model_name", "layer", "file_path", "materialization",
                "unique_key", "description", "grain", "source_tables", "ref_models",
                "joins", "filters", "column_count", "scanned_at",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(model_catalog)
    print(f"\n  Written: {catalog_path.relative_to(ROOT)} ({len(model_catalog)} models)")

    lineage_path = SEEDS_DIR / "dbt_column_lineage.csv"
    with open(lineage_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "id", "model_name", "layer", "column_name", "expression",
                "origin_table", "origin_column", "transformation_type",
                "transformation_chain", "transformation_chain_plain",
                "description",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(column_lineage)
    print(f"  Written: {lineage_path.relative_to(ROOT)} ({len(column_lineage)} columns)")

    # Keep the vault design seed in sync with the models that actually exist
    sync_vault_design_seed(model_catalog, column_lineage)

    print(f"\n{'=' * 60}")
    print(f"Models: {len(model_catalog)} | Columns: {len(column_lineage)}")
    by_layer: dict = {}
    for m in model_catalog:
        by_layer[m["layer"]] = by_layer.get(m["layer"], 0) + 1
    for layer, count in sorted(by_layer.items()):
        print(f"  {layer}: {count}")

    return model_catalog, column_lineage


def sync_vault_design_seed(model_catalog, column_lineage):
    """Append any vault models missing from dbt/seeds/data_vault_design.csv.

    Rules:
      - Existing rows are NEVER overwritten; only blank fields get back-filled.
      - New rows get inferred business_key / source_tables / notes / decided_date.
      - Rows present in the seed but missing from dbt/models/vault/ are
        logged as warnings but preserved (protects against typos).
      - Output is sorted hubs → links → satellites, alphabetical within type.
      - LF line endings (dbt's CSV sniffer trips on CRLF mixed with LF).
    """
    seed_path = SEEDS_DIR / "data_vault_design.csv"
    if not seed_path.exists():
        return

    fieldnames = [
        "id", "entity_type", "entity_name", "business_key",
        "source_tables", "grain", "notes", "decided_date",
    ]

    existing_rows = list(csv.DictReader(open(seed_path, encoding="utf-8")))
    by_name = {r["entity_name"]: r for r in existing_rows}

    # Pick the first non-housekeeping column per vault model — that's the
    # business key for hubs and the parent-hash-key reference for satellites.
    # Only accept clean snake_case identifiers — the extractor sometimes
    # grabs the full `SELECT DISTINCT <col>` expression for 1-column hubs.
    _ident = re.compile(r"^[a-z][a-z0-9_]*$")
    first_meaningful_col = {}
    for cl in column_lineage:
        if cl["layer"] != "vault":
            continue
        col = (cl["column_name"] or "").strip()
        if not _ident.match(col):
            continue
        if col.lower() in ("hashdiff", "load_date", "record_source"):
            continue
        if col.lower().startswith("hashdiff"):
            continue
        first_meaningful_col.setdefault(cl["model_name"], col)

    vault_models = [m for m in model_catalog if m["layer"] == "vault"]
    actual_names = {m["model_name"] for m in vault_models}

    added, updated = [], []
    max_id = max(
        (int(r["id"]) for r in existing_rows if r.get("id", "").isdigit()),
        default=0,
    )
    today = datetime.now().strftime("%Y-%m-%d")

    for m in vault_models:
        name = m["model_name"]
        if name.startswith("hub_"):
            entity_type = "hub"
        elif name.startswith("link_"):
            entity_type = "link"
        elif name.startswith("sat_"):
            entity_type = "satellite"
        else:
            continue

        # Trace refs back to stg_sap__ models and extract the SAP table name
        refs = (m.get("ref_models") or "").split(";")
        sap_sources = []
        for r in refs:
            r = r.strip()
            if r.startswith("stg_sap__"):
                sap_sources.append(r.replace("stg_sap__", "").upper())
        inferred_source_tables = "+".join(sorted(set(sap_sources)))

        inferred_bk = first_meaningful_col.get(name, "")
        if entity_type == "hub":
            business_key_value = inferred_bk
        else:
            # Links and satellites reference hubs via hk_* — use the first
            # such column from the model's column list if we captured one
            hk_cols = [
                cl["column_name"] for cl in column_lineage
                if cl["model_name"] == name and cl["column_name"].lower().startswith("hk_")
            ]
            business_key_value = hk_cols[0] if hk_cols else inferred_bk

        inferred_notes = (m.get("description") or "").strip()
        if not inferred_notes:
            inferred_notes = (
                f"{entity_type.capitalize()} — auto-registered from "
                f"dbt/models/vault/{name}.sql"
            )
        inferred_grain = (m.get("grain") or "").strip()

        def _is_bad(val: str) -> bool:
            """Treat newlines or the extractor's DISTINCT leakage as empty."""
            s = (val or "").strip()
            if not s:
                return True
            if "\n" in s or "\r" in s:
                return True
            if s.upper().startswith("DISTINCT"):
                return True
            return False

        row = by_name.get(name)
        if row is None:
            max_id += 1
            new_row = {
                "id": str(max_id),
                "entity_type": entity_type,
                "entity_name": name,
                "business_key": business_key_value,
                "source_tables": inferred_source_tables,
                "grain": inferred_grain,
                "notes": inferred_notes[:300],
                "decided_date": today,
            }
            existing_rows.append(new_row)
            by_name[name] = new_row
            added.append(name)
        else:
            # Back-fill blank OR malformed fields; never overwrite a clean
            # human-edited value
            changes = {}
            if _is_bad(row.get("business_key")) and business_key_value:
                changes["business_key"] = business_key_value
            if _is_bad(row.get("source_tables")) and inferred_source_tables:
                changes["source_tables"] = inferred_source_tables
            if _is_bad(row.get("grain")) and inferred_grain:
                changes["grain"] = inferred_grain
            if _is_bad(row.get("notes")) and inferred_notes:
                changes["notes"] = inferred_notes[:300]
            if changes:
                row.update(changes)
                updated.append(f"{name} ({', '.join(changes)})")

    # Stable sort: hubs first, then links, then satellites, alpha within type
    type_order = {"hub": 0, "link": 1, "satellite": 2}
    existing_rows.sort(
        key=lambda r: (
            type_order.get((r.get("entity_type") or "").lower(), 9),
            r.get("entity_name") or "",
        )
    )

    # Orphan detection — rows in seed but no matching vault model on disk
    orphans = [
        r["entity_name"] for r in existing_rows
        if r["entity_name"] not in actual_names and r.get("entity_type") in ("hub", "link", "satellite")
    ]

    with open(seed_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        w.writeheader()
        for r in existing_rows:
            w.writerow({k: (r.get(k) or "") for k in fieldnames})

    print(
        f"  Vault design seed: {len(existing_rows)} total "
        f"(+{len(added)} added, {len(updated)} back-filled)"
    )
    if added:
        print(f"    Added:       {', '.join(added)}")
    if updated:
        print(f"    Back-filled: {', '.join(updated[:6])}")
        if len(updated) > 6:
            print(f"                 … and {len(updated) - 6} more")
    if orphans:
        print(
            f"    ⚠️ Orphans (seed has, vault doesn't): {', '.join(orphans)}"
        )


if __name__ == "__main__":
    scan_all_models()
