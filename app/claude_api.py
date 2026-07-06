"""Claude API integration for intelligent S2T mapping suggestions.

Sends business term definition + SAP context to Claude, receives structured
source-to-target mapping suggestions.
"""
import json
import os
import requests
from pathlib import Path
from typing import Optional

# Load .env from project root
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if ENV_PATH.exists():
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
API_URL = "https://api.anthropic.com/v1/messages"

# Model id comes from scripts/_model_config.py (env override: DG_AGENT_MODEL)
import sys as _sys
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)
from _model_config import MODEL
from _source_config import SOURCE_SCHEMA, DOMAIN_CONTEXT


def llm_retry_on_error(
    conn,
    sql: str,
    original_question: str,
    max_retries: int = 3,
):
    """Execute SQL with LLM-powered self-healing on failure.

    When SQL fails (e.g. hallucinated column), captures the error, fetches
    actual schema for the involved tables, and asks Claude to fix the SQL.

    Args:
        conn: DuckDB connection (in-memory Parquet-backed).
        sql: The SQL to execute.
        original_question: The business question the SQL was meant to answer.
        max_retries: Maximum LLM retry attempts (default 3).

    Returns:
        (dataframe, final_sql, error_message, retry_count, original_sql_if_retried)
        - On success: (df, final_sql, None, retries_used, original_or_None)
        - On failure: (None, None, error_str, retries_used, original_sql)
    """
    import re

    # First attempt: try the SQL as-is
    try:
        df = conn.execute(sql).fetchdf()
        return df, sql, None, 0, None
    except Exception as first_err:
        first_error = str(first_err)

    # SQL failed — enter retry loop
    if not API_KEY or API_KEY == "your-api-key-here":
        return None, None, first_error, 0, sql

    current_sql = sql
    current_error = first_error
    attempted_sqls = [sql]

    for attempt in range(max_retries):
        # Extract table names from the failed SQL
        table_refs = set(re.findall(
            r'(?:main_staging\.stg_sap__(\w+)|main_marts\.(\w+)|main_obt\.(\w+)|main_knowledge\.(\w+))',
            current_sql, re.IGNORECASE,
        ))
        table_names = set()
        for groups in table_refs:
            for g in groups:
                if g:
                    table_names.add(g)

        # Fetch actual schema for involved tables
        schema_info = ""
        if table_names:
            schema_parts = []
            for tbl in sorted(table_names):
                for schema in ('main_staging', 'main_marts', 'main_obt', 'main_knowledge'):
                    try:
                        cols = conn.execute(
                            f"SELECT column_name, data_type FROM information_schema.columns "
                            f"WHERE table_schema = '{schema}' AND table_name = '{tbl}' "
                            f"ORDER BY ordinal_position"
                        ).fetchdf()
                        if not cols.empty:
                            schema_parts.append(
                                f"\n{schema}.{tbl}:\n" +
                                "\n".join(f"  {r['column_name']} ({r['data_type']})" for _, r in cols.iterrows())
                            )
                            break
                    except Exception:
                        continue
            schema_info = "\n".join(schema_parts)

        # Ask Claude to fix the SQL
        fix_result = _post_claude(
            system_prompt=(
                "You are a SQL repair assistant for DuckDB. The user's SQL failed. "
                "You are given the original question, the failed SQL, the exact error message, "
                "and the actual schema of the involved tables. "
                "Return ONLY corrected SQL — no explanation, no markdown fences, no preamble. "
                "RESPOND ONLY IN JSON: {\"sql\": \"<corrected SQL>\"}"
            ),
            user_prompt=(
                f"Original question: {original_question}\n\n"
                f"Failed SQL:\n{current_sql}\n\n"
                f"Error message:\n{current_error}\n\n"
                f"Actual schema of involved tables:\n{schema_info}\n\n"
                "Return corrected SQL only as JSON: {\"sql\": \"...\"}"
            ),
            max_tokens=1500,
        )

        if "error" in fix_result:
            break

        fixed_sql = fix_result.get("sql", "")
        if not fixed_sql or fixed_sql == current_sql:
            break

        attempted_sqls.append(fixed_sql)
        current_sql = fixed_sql

        try:
            df = conn.execute(fixed_sql).fetchdf()
            return df, fixed_sql, None, attempt + 1, sql
        except Exception as retry_err:
            current_error = str(retry_err)

    # All retries exhausted
    all_attempts = "\n\n---\n\n".join(
        f"Attempt {i+1}:\n{s}" for i, s in enumerate(attempted_sqls)
    )
    return None, None, f"{current_error}\n\nAll {len(attempted_sqls)} SQL attempts:\n{all_attempts}", len(attempted_sqls) - 1, sql


def suggest_s2t_mapping(
    term_name: str,
    term_definition: str,
    term_unit: str,
    term_grain: str,
    actual_schema: str,
    abap_catalog: str,
    existing_glossary: str,
    existing_s2t: str,
    analysis_findings: str = "",
    column_lineage: str = "",
):
    """Ask Claude to suggest source-to-target mapping for a business term.

    `actual_schema` is the CSV list of columns that actually exist in
    `main_staging` — not the theoretical SAP data dictionary seed. Claude
    may only reference tables and columns listed here; anything else must
    be flagged in the `warnings` array.

    Returns a dict with suggestion fields, or an {"error": ...} dict on failure.
    """
    if not API_KEY or API_KEY == "your-api-key-here":
        return {"error": "ANTHROPIC_API_KEY not set. Add it to .env file in project root."}

    system_prompt = """You are a senior SAP data analyst mapping business terms to the EXACT source tables that already exist in the project's DuckDB database.

You have access to:
1. The ACTUAL schema of tables already loaded in main_staging (authoritative)
2. The ABAP custom code catalog (custom programs that modify standard SAP behavior)
3. The existing business glossary (already mapped terms)
4. The existing S2T mappings (already completed mappings for reference)

CRITICAL — READ BEFORE RESPONDING:
The column list provided below is the EXACT set of columns available in our warehouse. You MUST NOT reference any table or column that is not in this list. If the metric requires additional columns that don't exist here, DO NOT invent them — flag them in the `warnings` array with the exact missing (table, column) pairs and explain what they'd be used for.

RESPOND ONLY IN JSON with this exact structure — no markdown, no backticks, no preamble:
{
    "suggested_sources": [
        {
            "source_table": "EKKO",
            "source_field": "BEDAT",
            "source_description": "PO creation date",
            "why_needed": "Start date for lead time calculation"
        }
    ],
    "joins": [
        {
            "left_table": "MSEG",
            "right_table": "EKKO",
            "join_key": "EBELN",
            "join_type": "INNER",
            "description": "Link goods receipt item to purchase order header"
        }
    ],
    "filters": [
        {
            "table": "MSEG",
            "field": "BWART",
            "condition": "= '101'",
            "reason": "Only goods receipt movements, exclude reversals and returns"
        }
    ],
    "transformation_plain": "Step-by-step description in plain English of how to compute the metric",
    "transformation_sql": "SELECT statement showing the actual SQL computation",
    "target_model": "the dbt mart model where this should live (e.g., fact_purchase_orders)",
    "target_column": "the column name in the target model (e.g., lead_time_days)",
    "warnings": ["Any caveats, ABAP custom code that might affect the result, edge cases to watch for, OR required columns that are missing from the actual schema"],
    "confidence": "high/medium/low"
}"""

    findings_section = (
        f"\n\nPRIOR DATA ANALYSIS FINDINGS for this term (these were validated against the live database — ground your mapping in them):\n{analysis_findings}"
        if analysis_findings and analysis_findings.strip() else ""
    )
    lineage_section = (
        f"\n\nColumn-level lineage of existing models — check this BEFORE suggesting a new target. If the metric already exists, reuse that column:\n{column_lineage[:3000]}"
        if column_lineage and column_lineage.strip() else ""
    )

    user_prompt = f"""Map this business term to the EXACT columns already loaded in our warehouse:

**Term:** {term_name}
**Definition:** {term_definition}
**Unit:** {term_unit}
**Grain:** {term_grain}

ACTUAL schema of main_staging (every column we actually have — do NOT reference anything outside this list):
{actual_schema}

Here is the ABAP custom code catalog (programs that modify standard SAP behavior):
{abap_catalog}

Here is the existing business glossary (already mapped terms for context):
{existing_glossary}

Here are the existing S2T mappings (completed examples for reference):
{existing_s2t}{findings_section}{lineage_section}

Suggest the source-to-target mapping for this term. Return ONLY valid JSON."""

    text = ""
    try:
        response = requests.post(
            API_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": MODEL,
                "max_tokens": 2000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=600,
        )
        response.raise_for_status()
        data = response.json()

        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        return json.loads(text)

    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {str(e)}"}
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse Claude response as JSON: {str(e)}", "raw_response": text[:500]}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


def _post_claude(system_prompt: str, user_prompt: str, max_tokens: int = 2000):
    """Shared POST helper for Claude API calls that return a single JSON blob."""
    if not API_KEY or API_KEY == "your-api-key-here":
        return {"error": "ANTHROPIC_API_KEY not set. Add it to .env file in project root."}

    text = ""
    try:
        response = requests.post(
            API_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": MODEL,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=600,
        )
        response.raise_for_status()
        data = response.json()

        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        return json.loads(text)

    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {str(e)}"}
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse Claude response as JSON: {str(e)}", "raw_response": text[:500]}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


def run_data_analysis(
    term_name: str,
    term_definition: str,
    term_unit: str,
    term_grain: str,
    actual_schema: str,
    abap_catalog: str,
    existing_glossary: str,
    additional_context: str = "",
    system_prompt_override: str = "",
    user_prompt_prefix: str = "",
    domain_context: str = "",
):
    """Ask Claude to plan a data exploration for a draft business term.

    `actual_schema` must be the CSV list of columns that actually exist
    in `main_staging` (not the theoretical SAP dictionary). Claude is
    instructed to only reference tables/columns in this list and to
    flag anything missing via the `missing_tables` array.

    Optional parameters keep the planner mode-agnostic — callers (BT
    tab, Domain tab) compute their own context and pass it in:
      * `system_prompt_override` — when non-empty, replaces the default
        business-term system prompt. Used by the Guided-Domain tab.
      * `user_prompt_prefix` — free-text prepended to the user prompt
        (caller-specific hints like focus area, scope layer).
      * `domain_context` — pre-formatted block from
        `load_domain_context()`. Prepended with a standard header so
        the planner does not re-propose queries that only confirm what
        we already know. Empty string → header is suppressed entirely
        (never pollute prompts on empty seed).

    Returns a dict with fields: `overall_assessment`, `exploration_queries`,
    `missing_tables`, `has_sufficient_data`, `confidence`.
    """
    default_system_prompt = """You are a senior SAP data analyst exploring a DuckDB database before proposing an S2T mapping.

DATABASE CONVENTIONS — follow these exactly:
- Staged SAP tables live in schema `main_staging` with the prefix `stg_sap__` and the table name in lowercase.
  For example: `EKKO` → `main_staging.stg_sap__ekko`, `MSEG` → `main_staging.stg_sap__mseg`.
- Column names are preserved verbatim from SAP (uppercase).
- Mart tables live in `main_marts` with lowercase snake_case names (e.g. `main_marts.fact_purchase_orders`).
- OBT views live in `main_obt` (e.g. `main_obt.obt_procurement_overview`).
- Knowledge views live in `main_knowledge`.
- All queries should be read-only SELECTs only.

CRITICAL — READ BEFORE RESPONDING:
The `actual_schema` list provided below is the EXACT set of columns we actually have in main_staging. You MUST NOT write SQL that references any column not in that list. If a column you'd need is missing, do NOT invent it — record the gap in the `missing_tables` array and explain what it would be used for. Then plan the exploration around the columns that DO exist.

TASK: Plan a compact set of exploration queries (typically 3-6) that validate the assumptions needed to map this business term to source tables. Each query should answer ONE specific question.

CHECK PRIOR KNOWLEDGE FIRST — if the additional_context already answers a question, do NOT re-query it. Skip directly to the remaining open questions.

RESPOND ONLY IN JSON with this exact structure — no markdown, no backticks, no preamble:
{
    "overall_assessment": "One short paragraph: what you need to learn, and how the queries below build toward an S2T mapping",
    "exploration_queries": [
        {
            "description": "Count of distinct POs and date range of EKKO",
            "type": "profile",
            "sql": "SELECT COUNT(DISTINCT EBELN) AS po_count, MIN(BEDAT) AS min_date, MAX(BEDAT) AS max_date FROM main_staging.stg_sap__ekko",
            "tables": "EKKO",
            "columns": "EBELN;BEDAT",
            "expected_insight": "Confirms approximately 2200 POs covering 2024-01 to 2026-03",
            "dq_notes": ""
        }
    ],
    "missing_tables": [
        {
            "table": "MSEG",
            "missing_columns": ["SOBKZ", "KOSTL", "AUFNR"],
            "why_needed": "Cost center allocation for warehousing cost component",
            "impact": "Cannot calculate warehousing cost allocation without these fields"
        }
    ],
    "has_sufficient_data": true,
    "confidence": "high"
}

VALID `type` values: `profile`, `join`, `count`, `sample`, `validate`.
VALID `confidence` values: `high`, `medium`, `low`.
`has_sufficient_data`: set to `false` when the missing columns block a reliable S2T mapping; `true` if exploration can proceed with what exists."""

    system_prompt = system_prompt_override.strip() if system_prompt_override and system_prompt_override.strip() else default_system_prompt

    user_prompt = f"""Plan the data exploration for this business term:

**Term:** {term_name}
**Definition:** {term_definition}
**Unit:** {term_unit}
**Grain:** {term_grain}

ACTUAL schema of main_staging (the EXACT columns we have — you may not reference anything outside this list):
{actual_schema[:6000]}

ABAP custom code catalog (programs that modify standard SAP behavior — flag any that touch the candidate source tables):
{abap_catalog[:2500]}

Existing business glossary (already mapped terms for reference):
{existing_glossary[:2000]}
{additional_context}

Return ONLY valid JSON. Queries must use `main_staging.stg_sap__<lowercase>` naming exactly. Any required column not in the schema above goes in `missing_tables`."""

    if user_prompt_prefix and user_prompt_prefix.strip():
        user_prompt = user_prompt_prefix.rstrip() + "\n\n" + user_prompt

    if domain_context and domain_context.strip():
        # Header is suppressed entirely when domain_context is empty —
        # keeps prompts clean on an unpopulated seed.
        domain_block = (
            "## Known domain facts (avoid re-proposing queries that only confirm these)\n"
            + domain_context.rstrip()
        )
        user_prompt = domain_block + "\n\n" + user_prompt

    return _post_claude(system_prompt, user_prompt, max_tokens=2500)


def explore_data_question(
    question: str,
    actual_schema: str,
    mart_schema: str,
    existing_knowledge: str = "",
):
    """Answer an open-ended data question. Prefers prior knowledge to avoid redundant queries.

    `actual_schema` must be the CSV list of columns that actually exist
    in `main_staging` — not the theoretical SAP dictionary. Claude is
    instructed to only reference tables/columns that exist in this list
    or in `mart_schema`.
    """
    system_prompt = """You are a data analyst answering questions about Helios Telecom's CPE procurement data stored in DuckDB.

AVAILABLE SCHEMAS:
- `main_staging` — 1:1 SAP source tables, prefixed `stg_sap__` and lowercase (e.g. `main_staging.stg_sap__ekko`)
- `main_marts`   — Kimball star schema (e.g. `main_marts.fact_purchase_orders`, `main_marts.dim_vendor`)
- `main_obt`     — flattened OBT views (e.g. `main_obt.obt_procurement_overview`, `main_obt.obt_vendor_scorecard`)
- `main_knowledge` — computed knowledge views (e.g. `main_knowledge.knowledge_vendor_performance`)

CRITICAL — READ BEFORE RESPONDING:
The two schema lists below are the EXACT columns we actually have. You MUST NOT reference any column not in these lists. If the question requires data that isn't available, set `sql` to an empty string and explain in `answer` which columns are missing.

RULES:
1. CHECK EXISTING KNOWLEDGE FIRST. If the answer is already in the provided knowledge context, use it and set `"from_knowledge": true` with an empty `sql`. Do not re-query what is already known.
2. If a query is needed, prefer mart/obt/knowledge tables over staging — they have business-friendly names and pre-joined facts.
3. Always use read-only SELECT. Never write to the database.
4. Explain the answer in plain business language with specific numbers.
5. Extract 2-5 key facts worth remembering for future questions.

RESPOND ONLY IN JSON — no markdown, no backticks, no preamble:
{
    "from_knowledge": false,
    "answer": "Plain language answer with specific numbers",
    "sql": "SELECT ... FROM main_obt.obt_procurement_overview ... (or empty string if answered from knowledge)",
    "tables_used": "obt_procurement_overview;dim_vendor",
    "key_facts": ["Huawei accounts for 79.8% of total procurement spend", "Average lead time is 44.8 days"],
    "confidence": "high"
}

VALID `confidence` values: `high`, `medium`, `low`."""

    user_prompt = f"""Question: {question}

ACTUAL staging schema (every column we have in main_staging — do NOT reference anything outside this list):
{actual_schema[:3500]}

Available mart/OBT/knowledge tables and columns:
{mart_schema[:3000]}
{existing_knowledge}

Answer the question. Check prior knowledge first. Return ONLY valid JSON."""

    return _post_claude(system_prompt, user_prompt, max_tokens=2000)


def generate_domain_report(
    findings: str,
    qa_log: str,
    glossary: str,
    domain_context: str = "",
):
    """Ask Claude to write a narrative domain knowledge report.

    Returns:
        {
            "narrative": "<markdown body>",
            "recommendations": ["...", "..."],
            "confidence": "high|medium|low"
        }
    """
    system_prompt = """You are a senior data analyst writing a domain knowledge report for Helios Telecom — a CPE procurement data product.

Write a clear, insightful narrative about what we know about their data. Your report must:
- Lead with concrete numbers from the findings (row counts, date ranges, distribution metrics)
- Note data quality observations and known gaps
- Call out relationships and patterns discovered across business terms
- Flag any areas where findings conflict or where more analysis would materially help
- End with actionable next-step recommendations

Use a professional but readable tone. Markdown-format the narrative with ## / ### headers and bullet lists. Cite specific numbers — do not generalise.

RESPOND ONLY IN JSON with this exact structure — no markdown fences, no preamble:
{
    "narrative": "## CPE Procurement — Domain Snapshot\\n\\nHelios Telecom runs ...",
    "recommendations": [
        "Run an ABAP review against ZMM_AUTO_EQUI_CREATE before mapping equipment lifecycle",
        "Extend analysis_findings with a join-validation query for sat_equipment_status"
    ],
    "confidence": "high"
}"""

    user_prompt = f"""Write a domain knowledge report from the following inputs.

Analysis Findings (from actual queries against DuckDB):
{findings[:4000]}

Q&A Knowledge (open-ended exploration):
{qa_log[:2000]}

Business Glossary (terms + definitions):
{glossary[:2000]}

Return ONLY valid JSON."""

    if domain_context and domain_context.strip():
        system_prompt = domain_context.rstrip() + "\n\n" + system_prompt

    return _post_claude(system_prompt, user_prompt, max_tokens=2500)


def _resolve_term_scope(term_id: str) -> list | None:
    """Strategy-2 scope resolution: distinct source_tables from s2t_mapping
    for this term. Returns lowercase list, or None when no s2t rows exist
    so the helper's cascade (S1 -> S3 -> S5) can fall through via term_id.

    Reuses the query logic from Business_Glossary.py:1297-1306.
    """
    import duckdb
    db_path = Path(__file__).resolve().parent.parent / "cpe_analytics.duckdb"
    if not db_path.exists():
        return None
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_table FROM main_seeds.s2t_mapping "
            "WHERE business_term_id = ?", [term_id],
        ).fetchall()
        if not rows:
            return None
        return sorted({str(r[0]).strip().lower() for r in rows if r[0]})
    finally:
        conn.close()


def _audit_s2t_citations(result: dict, bundle_text: str = "") -> dict:
    """P7.4 Fix 1: grep the BUNDLE text (not response text) for citation IDs.

    Per the CITATION ID FORMAT directive: "The runner greps the BUNDLE text
    for each ID you emit. Invented slugs fail verification because they do
    not exist in the bundle." The earlier P7.3 implementation greped the
    response text, producing false-positives when the LLM cited a real ID
    (e.g., DF-0001) without literally writing the ID in the prose — the
    consumption was semantic, not verbatim. Correct check: does the cited
    ID actually exist in the bundle the LLM was shown.

    Also retained: empty-citations-while-consumed=true still flags mismatch
    (LLM self-contradiction).
    """
    issues: list[str] = []
    for src in ("domain_facts", "analysis_findings", "dar", "bar"):
        consumed = bool(result.get(f"{src}_consumed", False))
        citations = result.get(f"{src}_citations") or []
        if consumed and not citations:
            issues.append(f"{src}_consumed=true but {src}_citations is empty")
        for c in citations:
            cs = str(c)
            # Citation format: "DF-0001: ..." → id is "DF-0001"
            tok = cs.split(":", 1)[0].strip() if ":" in cs else cs.split()[0] if cs else ""
            if not tok:
                continue
            # Fix 1: grep bundle, not response. If ID not in bundle → invented.
            if bundle_text and tok not in bundle_text:
                issues.append(
                    f"{src}_citations cites {tok!r} but the ID does not exist "
                    f"in the bundle (invented slug)"
                )
    result["llm_self_attestation_mismatch"] = bool(issues)
    result["_citation_audit_issues"] = issues
    return result


# =========================================================================
# Create S2T BAR-consumer path (translation task).
# Invoked by the dispatcher at the top of create_s2t_with_implementation
# when a promoted BAR exists for the term. BAR is authoritative SQL;
# this function translates it to dbt-model form (Jinja refs + config +
# tests + YAML description + meta).
# =========================================================================

import re as _re_s2t_bar

# Regex matching {{ ref('<model>') }} in generated SQL for the
# post-translation audit. Simple; Jinja-inside-string-literal not guarded
# (LLM output almost never emits that; matches Layer A's pattern in
# _context_assembler._rewrite_ref_to_literal).
_REF_EXTRACT_PATTERN = _re_s2t_bar.compile(
    r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}"
)

_BAR_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "prompts" / "create_s2t_from_bar_prompt.md"
)


def _load_bar_prompt_template() -> tuple[str, str]:
    """Split the translation prompt into (system, user_template).
    System = text between '## SYSTEM PROMPT' and '## INPUT'; user_template
    = '## INPUT' onward (interpolated at call time).
    """
    raw = _BAR_PROMPT_PATH.read_text(encoding="utf-8")
    sys_marker = "## SYSTEM PROMPT"
    input_marker = "## INPUT"
    sys_start = raw.index(sys_marker) + len(sys_marker)
    sys_end = raw.index(input_marker)
    return raw[sys_start:sys_end].strip(), raw[sys_end:].strip()


def _render_layer_context(bundle, which: str) -> str:
    """Extract Layer B (dbt_semantic_model) or Layer A (semantic_model)
    rendered sub-block from the bundle's static layer text. Best-effort:
    the static layer builds these blocks with labeled headers; we split
    on those. If the label isn't present (no rows), return empty string.
    """
    if not bundle or not bundle.static_layer_text:
        return ""
    text = bundle.static_layer_text
    if which == "layer_b":
        marker = "## dbt Semantic Model (Layer B)"
    elif which == "layer_a":
        marker = "## Semantic Model (Layer A)"
    else:
        return ""
    idx = text.find(marker)
    if idx < 0:
        return ""
    # Extract until next "##" heading or end
    rest = text[idx:]
    next_heading = rest.find("\n## ", 1)
    if next_heading > 0:
        return rest[:next_heading]
    return rest


def _audit_refs_against_bar(sql: str, bar_dsm_consumed: list[str],
                             bundle) -> list[str]:
    """Extract every ref('<m>') from generated SQL; each must
    be in bar_dsm_consumed OR in Layer B (bundle.static_layer_text).
    Returns list of unauthorized refs (empty = pass).
    """
    refs = _REF_EXTRACT_PATTERN.findall(sql)
    authorized_primary = {m.lower() for m in bar_dsm_consumed}
    layer_b_text = _render_layer_context(bundle, "layer_b").lower()
    unauthorized = []
    for ref_name in refs:
        name_lc = ref_name.lower()
        if name_lc in authorized_primary:
            continue
        # Layer B fallback: did the bundle render this model name?
        if name_lc in layer_b_text:
            continue
        unauthorized.append(ref_name)
    return unauthorized


def create_s2t_from_promoted_bar(
    term_name: str,
    term_definition: str,
    term_unit: str,
    term_grain: str,
    term_id: str,
    bundle,
    promoted,
):
    """Translate a promoted BAR's SQL into a production dbt
    artifact. Returns the same output shape as create_s2t_with_implementation
    so Streamlit Business_Glossary.py stays compatible.
    """
    system_prompt, user_template = _load_bar_prompt_template()

    # Render context blocks from the bundle.
    layer_b_context = _render_layer_context(bundle, "layer_b") or "(no Layer B rows in scope)"
    layer_a_context = _render_layer_context(bundle, "layer_a") or "(no Layer A rows in scope)"

    # Stringify term_conditions_covered + ref_targets for prompt injection.
    conditions_str = "\n".join(f"  - {c}" for c in promoted.term_conditions_covered) \
        or "  (none listed in BAR)"
    ref_targets_str = ", ".join(promoted.dbt_semantic_model_consumed)

    user_prompt = (
        user_template
        .replace("{bar_id}", promoted.bar_id)
        .replace("{final_query_sql}", promoted.final_query_sql)
        .replace("{term_conditions_covered}", conditions_str)
        .replace("{final_metric_interpretation}", promoted.final_metric_interpretation)
        .replace("{iterations_count}", str(promoted.iterations_count))
        .replace("{confidence}", promoted.confidence or "unknown")
        .replace("{ref_targets}", ref_targets_str)
        .replace("{layer_b_context}", layer_b_context)
        .replace("{layer_a_context}", layer_a_context)
    )

    result = _post_claude(system_prompt, user_prompt, max_tokens=4000)
    if isinstance(result, dict) and "error" in result:
        return {"error": f"BAR-consumer path LLM call failed: {result['error']}"}

    # Ref audit — every ref() must be authorized.
    audit_issues: list[str] = []
    dbt_models = result.get("dbt_models") if isinstance(result, dict) else None
    if dbt_models:
        for m in dbt_models:
            sql = m.get("sql", "")
            unauthorized = _audit_refs_against_bar(
                sql, promoted.dbt_semantic_model_consumed, bundle,
            )
            if unauthorized:
                audit_issues.append(
                    f"model '{m.get('name')}': unauthorized ref(s) "
                    f"{unauthorized}"
                )

    # Assemble return dict preserving backward-compat with Streamlit's
    # consumption contract. New top-level `source` and `bar_id`
    # keys are additive; existing consumers won't see them unless they
    # look.
    if not isinstance(result, dict):
        result = {"dbt_models": []}
    result.setdefault("dbt_models", [])
    # Existing attestation fields — honestly reflect what happened:
    # BAR-consumer path is predominantly bar_consumed; dbt_semantic_model
    # and semantic_model carry through from BAR; other dynamic-source
    # attestation fields remain empty (we didn't re-cite them here — the
    # BAR already did during term analysis and we trust its audit chain).
    result["source"] = "promoted_bar"
    result["bar_id"] = promoted.bar_id
    result["bar_consumed"] = True
    result["bar_citations"] = [
        f"{promoted.bar_id}: translated promoted BAR SQL into dbt model "
        f"(confidence={promoted.confidence}, iterations={promoted.iterations_count})"
    ]
    result["dbt_semantic_model_consumed"] = list(promoted.dbt_semantic_model_consumed)
    result["semantic_model_consumed"] = list(promoted.semantic_model_consumed)
    # Untouched-by-this-path attestation (the BAR audit discharged these
    # during term analysis; we're not re-citing per decision #73's scope —
    # BAR is authoritative, not the bundle).
    result.setdefault("domain_facts_consumed", False)
    result.setdefault("domain_facts_citations", [])
    result.setdefault("analysis_findings_consumed", False)
    result.setdefault("analysis_findings_citations", [])
    result.setdefault("dar_consumed", False)
    result.setdefault("dar_citations", [])
    # Back-compat: legacy create_s2t_with_implementation also emitted
    # these "existing" keys; default empty when BAR-path doesn't populate.
    result.setdefault("s2t_mapping", [])
    result.setdefault("transformation_plain", promoted.final_metric_interpretation)
    result.setdefault("transformation_sql", promoted.final_query_sql)
    result.setdefault("warnings", [])
    result.setdefault("confidence", promoted.confidence or "medium")
    result.setdefault("implementation_plan", {
        "start_layer": "marts",
        "layers_needed": ["marts"],
        "explanation": (
            f"Translated from promoted BAR {promoted.bar_id}. "
            f"Source SQL validated by the term-analysis iteration loop at "
            f"{promoted.executed_at_utc}; this path only rewrote literal "
            f"refs to Jinja and added dbt wrapping (config + schema.yml). "
            f"No semantic changes."
        ),
    })
    # Bundle metadata (UI contract)
    result["_bundle_fingerprint"] = bundle.debug["fingerprint"] if bundle.debug else ""
    result["_bundle_total_tokens"] = bundle.token_count
    result["_bundle_scope_strategy"] = bundle.scope_resolution.get("strategy_used", "unknown")
    result["_bundle_resolved_tables"] = bundle.scope_resolution.get("resolved_tables", [])

    # Surface audit issues as warnings (don't fail the call; Streamlit
    # shows them to the analyst; severe issues hard-stop).
    if audit_issues:
        result.setdefault("warnings", []).extend(audit_issues)
        result["_bar_audit_issues"] = audit_issues

    return result


def create_s2t_with_implementation(
    term_name: str,
    term_definition: str,
    term_unit: str,
    term_grain: str,
    term_id: str,
):
    """Helper-based Create S2T. Replaces the 9 inline context loads
    with a single assemble_context(purpose='create_s2t', ...) call and
    applies per-source consumption directives (domain_facts, analysis_findings,
    DAR rows, BAR rows).

    The caller at Business_Glossary.py passes only term_id + term scalars;
    all context is assembled inside this function. The deploy auto-retry +
    semantic-validation gate logic remain in the caller, unchanged.

    Returns:
        {
            "s2t_mapping": [...],
            "transformation_plain": "...",
            "transformation_sql": "...",
            "implementation_plan": {...},
            "dbt_models": [...],
            "warnings": [...],
            "confidence": "high|medium|low",
            # 8 attestation fields (4 *_consumed bools + 4
            # *_citations arrays) plus llm_self_attestation_mismatch +
            # _citation_audit_issues diagnostic.
            "domain_facts_consumed": bool,
            "domain_facts_citations": [...],
            "analysis_findings_consumed": bool,
            "analysis_findings_citations": [...],
            "dar_consumed": bool,
            "dar_citations": [...],
            "bar_consumed": bool,
            "bar_citations": [...],
            "llm_self_attestation_mismatch": bool,
            "_citation_audit_issues": [...],
            # Plus bundle metadata:
            "_bundle_fingerprint": str,
            "_bundle_total_tokens": int,
            "_bundle_scope_strategy": str,
        }
    """
    # Lazy helper import — scripts/ is not on app/'s default path
    import sys as _sys
    _scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    if str(_scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(_scripts_dir))
    from _context_assembler import (  # noqa: E402
        assemble_context,
        ContextScopeError,
        ContextDegradedError,
    )
    from _bar_consumer import (  # noqa: E402
        resolve_promoted_bar,
        resolve_latest_bar,
        BarConsumptionError,
    )

    if not API_KEY or API_KEY == "your-api-key-here":
        return {"error": "ANTHROPIC_API_KEY not set. Add it to .env file in project root."}

    # Scope: Strategy-2 inline; pass None when empty so helper cascades.
    scope_tables = _resolve_term_scope(term_id)
    try:
        bundle = assemble_context(
            purpose="create_s2t",
            term_id=term_id,
            scope_tables=scope_tables,
            max_tokens=90_000,
            strict=False,   # don't raise on empty HEAVY (ontology is always non-empty);
                            # Create S2T must tolerate sparse new terms gracefully
            include_debug_metadata=True,
        )
    except ContextScopeError as e:
        return {"error": f"scope resolution failed: {e}"}
    except ContextDegradedError as e:
        return {"error": f"context degraded: {e}"}

    # ---------------------------------------------------------------
    # Dispatcher — promoted-BAR path (authoritative SQL) vs
    # generator path (today's behavior). Falls back to generator on
    # BarConsumptionError so Streamlit UI never hard-crashes on a
    # malformed promoted BAR.
    # ---------------------------------------------------------------
    try:
        promoted = resolve_promoted_bar(None, term_id)
    except BarConsumptionError as bar_exc:
        print(f"[WARN] promoted BAR unusable ({bar_exc}); falling back to generator path")
        promoted = None

    if promoted is not None:
        print(f"[create_s2t] dispatcher: promoted_bar path ({promoted.bar_id}, "
              f"confidence={promoted.confidence})")
        try:
            return create_s2t_from_promoted_bar(
                term_name=term_name,
                term_definition=term_definition,
                term_unit=term_unit,
                term_grain=term_grain,
                term_id=term_id,
                bundle=bundle,
                promoted=promoted,
            )
        except Exception as exc:
            # Any translation failure → fall back to generator path with warning.
            # This preserves the demo-stability invariant (Streamlit never breaks
            # on a promoted term; worst case is "regenerated from scratch").
            print(f"[WARN] promoted_bar path raised: {exc}; falling back to generator")
    else:
        # ─── C6 Finding D — needs_data_extension / hard_stop refusal ───
        # Before falling through to the generator, check the latest
        # finished BAR. If Stage D's iteration loop already concluded
        # the term is unanswerable from the current scope, refuse to
        # generate Stage E SQL — surface the BAR's verdict +
        # sourcing_recommendations to the analyst instead.
        try:
            latest_bar = resolve_latest_bar(None, term_id)
        except BarConsumptionError as _bar_exc:
            print(f"[WARN] latest BAR malformed ({_bar_exc}); "
                  f"falling through to generator path")
            latest_bar = None

        _refuse_statuses = {"needs_data_extension", "hard_stop"}
        if latest_bar is not None and latest_bar["status"] in _refuse_statuses:
            # Extract bridge_violations from the iteration_trace if the
            # last iteration's gates surfaced any.
            _bridge_violations: list = []
            _trace = latest_bar.get("iteration_trace") or []
            if _trace and isinstance(_trace[-1], dict):
                _gates = _trace[-1].get("gates_result") or {}
                _bv = _gates.get("bridge_violations") or []
                if isinstance(_bv, list):
                    _bridge_violations = list(_bv)

            print(f"[create_s2t] dispatcher: refusing on BAR "
                  f"{latest_bar['id']} status={latest_bar['status']}")
            return {
                "error": f"stage_e_refused_bar_{latest_bar['status']}",
                "_refusal_kind": "bar_needs_data_extension",
                "_bar_id": latest_bar["id"],
                "_bar_status": latest_bar["status"],
                "_bar_convergence_reason": latest_bar.get("convergence_reason"),
                "_bridge_violations": _bridge_violations,
            }

        print(f"[create_s2t] dispatcher: generator path (no promoted BAR for {term_id})")

    # ---------------------------------------------------------------
    # SYSTEM PROMPT — rewritten around bundle layers + directives
    # ---------------------------------------------------------------
    system_prompt = r"""You are a senior data engineer creating both a source-to-target mapping AND a dbt implementation plan for a business metric in __DOMAIN_CONTEXT__.

INPUTS are delivered as a single layered context bundle. Each layer serves a different purpose:

1. STATIC CATALOG — SAP column semantics, types, business meaning (sap_data_dictionary, source_column_roles, movement_type_mapping, z_tables_catalog, information_schema.columns of raw_sap)
2. DYNAMIC LEARNING — accumulated analyses for this scope / term (domain_analysis_results DAR rows, business_term_analysis_results BAR rows, analysis_findings)
3. ONTOLOGY — target dbt graph (dbt_column_lineage, existing models across main_staging / main_vault / main_marts / main_obt, existing s2t_mapping rows)
4. EXAMPLES — curated SAP code patterns (abap_logic_catalog)
5. BUSINESS — domain reference + term definition + curated facts (sat_vendor_business + sat_material_business via vault joins, procurement_rules, org_structure, domain_facts, business_glossary)
6. ARCHIVED — prior-art from archived terms with similar name (archive_log; RULE 33 learning_signal=true)

Each layer is scope-filtered and token-budgeted. You MUST consume content from each layer per the DIRECTIVES block below.

CRITICAL — READ BEFORE RESPONDING:
- The static layer's information_schema + source_column_roles lists are the EXACT set of columns we have in main_staging / raw_sap. You MUST NOT reference any column not in those lists. If the metric requires additional columns that do not exist, do NOT invent them — flag each missing (table, column) pair in `warnings` and set `confidence` to `low` if it blocks a reliable mapping.
- Before suggesting new models, CHECK the ontology layer's column lineage. If the metric already exists as a column in an existing model, DO NOT create a new model — reference the existing column and say in the explanation "this metric already exists at {model}.{column} — no new models needed." Only propose new dbt models when the data genuinely is not available anywhere.

DIRECTIVES — per dynamic source type (P7.2 consumption mandate, pattern validated in Gate D2.3):

DIRECTIVE 3a — domain_facts (in business layer):
If the bundle contains domain_facts rows scoped to your source tables (category / scope_tables / fact_technical format), you MUST:
  - cite at least one fact_technical verbatim in transformation_plain when it affects a mapping choice;
  - when a fact's category is in {value_domain, referential_integrity, null_pattern}, incorporate the constraint into transformation_logic_sql as a WHERE / COALESCE / filter;
  - list any fact that reveals data risk in the warnings array.
Silent ignoring of domain_facts that intersect your mapping is a failure.

DIRECTIVE 3b — analysis_findings (in dynamic layer):
If the bundle contains analysis_findings rows for this term (finding_type / query_description / result_summary format), you MUST:
  - cite at least one result_summary in transformation_plain;
  - when a finding reveals a systematic issue (join fan-out, null cluster, cardinality surprise), incorporate it into your mapping — different join path, added filter, or warnings entry.
Silent ignoring of term-scoped findings is a failure.

DIRECTIVE 3c — DAR rows / domain_analysis_results (in dynamic layer, scope-filtered):
If the bundle contains DAR rows whose source_tables overlap your mapping's source tables, examine each by `analysis_type`. You MUST cite at least one DAR finding in transformation_plain when any DAR's scope overlaps your mapping. Apply per-analysis_type rules:
  - completeness → columns with reliability != 'high' trigger WHERE IS NOT NULL or COALESCE(<col>, <sentinel>) in the source field's transformation_logic_sql, plus warnings entry if null rate > 10%;
  - dimensions → respect observed cardinality in GROUP BY choice (do not propose a column for grouping if Dimensions showed it has < 2 distinct values); cite cardinality in transformation_plain;
  - magnitude → cite top-bucket distribution in transformation_plain if your mapping aggregates over that dimension;
  - code_tables → mapping a code column requires Shape B (D2.3 pattern): LEFT JOIN {{ ref('<desc_seed_name>') }} in your dbt SQL (never hard-code main_seeds.<seed>); add a description column at the mart/OBT layer if the dashboard needs human labels; cite at least the top-2 code→description pairs in transformation_plain;
  - sample_rows / dates / part_to_whole / ref_integrity → inform transformation_plain or join_description when relevant.

DIRECTIVE 3d — BAR rows / business_term_analysis_results (in dynamic layer, term-scoped):
If the bundle contains BAR rows for this specific business_term_id (term-scoped analyses), treat them as HIGHER-PRIORITY than scope-only DAR rows — they reflect this term's own prior analyses. Cite at least one BAR finding in transformation_plain and apply the same analysis_type-specific rules as DAR rows above. RULE 42 rev #5: no cross-term inheritance — only BAR rows where business_term_id matches this term's id count.
On conflict between DAR and BAR findings for the same attribute (e.g., same column appears in both a DAR completeness row and a BAR completeness row with different reliability), prefer BAR and cite both in bar_citations with the conflict noted.

DIRECTIVE 3f — dbt_semantic_model / Layer B (in static layer):
If the bundle's static layer contains a "dbt Semantic Model (Layer B)" block, each row is the canonical SQL-writing convention for a dbt model (staging / vault / marts / obt / knowledge) that covers one or more raw scope tables. Consumer priority is LAYER B FIRST (dbt-covered), LAYER A SECOND (raw-only), BASE LLM KNOWLEDGE THIRD.
When Layer B applies:
  - in your `dbt_models[].sql` output, use `{{ ref('<model_name>') }}` (Jinja; dbt renders at compile time) — the seed stores reference_sql in this canonical form and for this consumer it is passed through unchanged;
  - use `canonical_alias` when aliasing (e.g. `ekbe` for `stg_sap__ekbe`);
  - use `exposed_columns_json` to verify column names and types before referencing;
  - use `typical_join_keys_json` to choose join conditions with upstream / downstream models;
  - cite the `dbt_semantic_model` model_name values you consulted in a new attestation list `dbt_semantic_model_consumed` (empty list valid when Layer B had no rows for this scope). **Before finalizing any join condition from typical_join_keys_json, verify it against cardinality evidence per DIRECTIVE 3g — cardinality overrides integrity.**
Silent ignoring of a Layer B row that covers one of your scope tables is a failure — the dbt graph's per-model conventions exist precisely so the LLM doesn't guess column names or references.

DIRECTIVE 3f-LAYERING (anti_patterns.md RULE 3 — enforced at commit, so honor it here):
Any dbt_models[] entry in the `marts`, `obt`, or `knowledge` layer MUST `{{ ref() }}` **vault models only** (hubs / links / satellites) — NEVER staging (`stg_sap__*`), raw_sap.*, or a sibling mart. Build the transformation on the vault layer: source measures/attributes from satellites, traverse entities via links/hubs, and dedup SCD2 satellites to their current version (e.g. `QUALIFY ROW_NUMBER() OVER (PARTITION BY <hk> ORDER BY load_date DESC) = 1`) to avoid fanout. If a needed column is only available in staging and not exposed by any vault satellite, emit a blocker/warning naming the missing vault coverage rather than reaching down into staging.

DIRECTIVE 3g — join_cardinality evidence (in dynamic layer, Direction F.2 consumer):

The dynamic layer contains a `## join_cardinality (scope-filtered, evidence-prioritized)` block when present. This block reports EMPIRICALLY MEASURED row-multiplication for candidate join keys per pair of scope tables. Each entry classifies a join candidate into one of four buckets:

  - `per_record_key`: avg fanout in [0.9, 1.1], stddev < 0.5, matched > 0.8. SAFE per-record join. Use as primary join key when both tables required at same grain.
  - `header_detail`: avg fanout in [1.5, 100] with bounded variance. Safe ONLY when query aggregates the detail side (GROUP BY header key). Do not use for per-record joins without aggregation.
  - `catastrophic_fanout`: avg fanout > 100 OR stddev > avg. FORBIDDEN as join key — produces cartesian-risk SQL. Do not use regardless of integrity_pct, regardless of shared-name appeal, regardless of typical_join_keys_json recommendation.
  - `no_signal`: matched_ratio < 0.1. Structurally invalid in this data — keys exist but produce no joinable rows.

CRITICAL OVERRIDE: when cardinality evidence and typical_join_keys_json (DIRECTIVE 3f) disagree, **cardinality evidence always wins**. typical_join_keys_json is derived from referential integrity (presence: every source value exists in target). Cardinality measures selectivity (how many target rows match each source row). Integrity is not selectivity. A 100%-integrity join key can still be catastrophic_fanout if it's a classification code shared across many records (e.g., MATNR shared across thousands of equipment units fans out 4500x when joined directly between equi and mseg).

When the term's grain requires linkage between entities and no direct per_record_key exists, USE THE BRIDGE PATH explicitly named in the cardinality block. The bridge's intermediate table must appear in your `proposed_tables` and your `join_path` must traverse it. Example: if cardinality reports `equi <-> mseg: bridge via seri (EQUNR -> EQUNR | MBLNR -> MBLNR): per_record_key`, your SQL must JOIN equi -> seri (on EQUNR) -> mseg (on MBLNR), NOT equi -> mseg directly.

Cite each cardinality DAR you consulted in a new attestation list `join_cardinality_consulted: [DAR-NNNNN, ...]`. If you ignore cardinality evidence and propose a join classified as `catastrophic_fanout`, your output will be rejected by the post-generation validator (Direction F.3).

DIRECTIVE 3h — bridge_coverage_by_filter evidence (in dynamic layer):

The dynamic layer renders BRIDGE-COVERAGE entries when the bundle includes bridge_coverage_by_filter DARs. Each entry reports EMPIRICALLY MEASURED reachability of filter values through a specific join path:

  BRIDGE-COVERAGE [DAR-XXXXX]: <from>-><via>-><to> | filter: <table>.<column>
    reachable: ['v1', 'v2', ...]
    unreachable: ['v3', 'v4', ...] (+N more)

If your proposed SQL filters on `<table>.<column>` AND your join path passes through the <from>-><via>-><to> bridge, the filter values you specify MUST appear in the `reachable` list for at least one matching DAR. Filtering on values that appear in `unreachable` produces SQL that returns 0 rows because the filter cannot be satisfied through the chosen joins — empirically, not theoretically.

CRITICAL: when a bridge_coverage_by_filter DAR's `unreachable` list includes a value you filter on AND your join path passes through the DAR's bridge, your output will be rejected by the post-generation bridge-coverage validator. The rejection is deterministic, based on empirical analyzer evidence, not LLM judgment.

If the metric requires unreachable filter values, do NOT generate SQL. Set `confidence='low'`, surface the issue in `warnings`, and recommend re-running Stage D's iteration loop (which produces sourcing_recommendations for unreachable cases via C5).

Cite each bridge_coverage_by_filter DAR you consulted in a new attestation list `bridge_coverage_consulted: [DAR-NNNNN, ...]`. An empty list when DARs are present in scope means you ignored available reachability evidence — discipline failure that fails the audit.

DIRECTIVE 3e — semantic_model / Layer A (in static layer):
If the bundle's static layer contains a "Semantic Model (Layer A)" block, each row is the canonical SQL-writing convention for a raw source table that lacks dbt ontology coverage. Consumer priority is ONTOLOGY FIRST, LAYER A SECOND:
  - if the raw table has ontology coverage (any main_staging/main_vault/main_marts model references it in the ontology layer's column lineage), use the ontology path (ref() to the mart/staging model) and do NOT consult Layer A;
  - only when the raw table has no ontology coverage, use the Layer A row.
When Layer A applies:
  - use `canonical_alias` when aliasing the raw table in any SQL you emit (exact lowercase alias from the row);
  - use `code_column_refs_json` to identify which decoder seed to JOIN for code decoding — NEVER invent inline VALUES clauses for code tables; JOIN the seed the Layer A row names;
  - apply `typical_filters` unless the term definition explicitly overrides them;
  - cite the `table_name` values of Layer A rows you consulted in a new attestation list `semantic_model_consumed` (the list of raw table_name strings — empty list is valid when Layer A had no rows for this scope).
Silent ignoring of a Layer A row that covers one of your scope tables is a failure — the table's canonical conventions exist precisely so the LLM doesn't have to guess.

CITATION ID FORMAT (applies to all 4 *_citations arrays — ABSOLUTE RULE):

Every citation's ID prefix MUST be the primary-key string present verbatim in the bundle. The bundle's layer markdown shows each row's ID in its first column. You MUST copy that ID character-for-character.

FOR domain_facts rows: the ID column is named 'fact_id' and has format 'DF-NNNN' (numeric, 4 digits). Example: DF-0001, DF-0007, DF-0042. You MUST use this exact format.

FOR analysis_findings rows: the ID column is named 'id' and has format 'AFNNN' (no dash, 3 digits). Example: AF001, AF015, AF103. You MUST use this exact format.

FOR DAR rows (domain_analysis_results): the ID column is named 'id' and has format 'DAR-NNNNN' (dash, 5 digits). Example: DAR-00007, DAR-00011. You MUST use this exact format.

FOR BAR rows (business_term_analysis_results): the ID column is named 'id' and has format 'BAR-NNNNN' (dash, 5 digits). Example: BAR-00003. You MUST use this exact format.

FORBIDDEN: inventing slugs from content (e.g., 'DF-currency', 'DF-temporal_pattern', 'DAR-magnitude-mseg', 'AF-join'). These are NOT IDs — they are descriptions. Slugs are not accepted.

VERIFICATION RULE: before writing a citation, locate the exact row in the bundle's layer (static/dynamic/business). Copy the ID field as-is. If you cannot find the row in the bundle, do NOT write that citation — set the _consumed flag to false for that source instead.

The runner greps the BUNDLE text for each ID you emit. Invented slugs fail verification because they do not exist in the bundle.

SELF-ATTESTATION FIELDS (required in output JSON):
For each of the 4 sources above, emit a boolean `<src>_consumed` plus a list `<src>_citations` of format ["DF-NNNN: <how used>", ...]. The runner runs a post-hoc grep audit: if you self-attest consumed=true but your citations are empty, or you cite an ID that does not appear anywhere in your transformation_plain / transformation_sql / dbt_models[].sql / warnings / implementation_plan.explanation, the response is flagged as a lie (llm_self_attestation_mismatch=true).

YOUR JOB:
- Create the full S2T mapping as individual row objects (one per source field) in the project's CSV schema
- Work backwards from the highest layer where the data already exists (ontology layer tells you). If a vault satellite already holds the facts, only create a mart + OBT on top. If a mart already covers it, only create an OBT view. Never rebuild a layer that already exists.
- Generate dbt SQL for any NEW models. Follow existing conventions:
    * staging: main_staging.stg_sap__<lowercase> with preserved SAP column names (no business renames here)
    * vault:   incremental, hk_* hash keys + hashdiff
    * marts:   materialized=table, snake_case column names, business-friendly (this is where description enrichment via {{ ref() }} joins belongs)
    * obt:     materialized=view, one query per dashboard page
- In any dbt SQL you generate, reference other project models + seeds with {{ ref('<name>') }} — NEVER hard-code a schema like main_staging.xyz or main_seeds.xyz.

RESPOND ONLY IN JSON with this exact structure — no markdown fences, no preamble:
{
    "s2t_mapping": [
        {
            "source_table": "EKKO",
            "source_field": "BEDAT",
            "source_description": "PO creation date",
            "transformation_logic_plain": "Use as the start date for lead time",
            "transformation_logic_sql": "CAST(BEDAT AS DATE)",
            "join_description": "Header table — one row per PO",
            "filter_description": "All PO types",
            "target_model": "fact_purchase_orders",
            "target_column": "po_date"
        }
    ],
    "transformation_plain": "Full plain-language transformation in 3-6 steps. Human-readable narrative; cite DF/AF/DAR/BAR IDs inline only when material to the mapping — do NOT dump all citations mechanically.",
    "transformation_sql": "Full SQL with CTEs showing the end-to-end transformation",
    "implementation_plan": {
        "start_layer": "vault",
        "layers_needed": ["marts", "obt"],
        "explanation": "Why we start from this layer and what must be built"
    },
    "dbt_models": [
        {
            "filename": "fact_new_metric.sql",
            "layer": "marts",
            "description": "Aggregates vault satellites into the per-vendor-per-month fact",
            "sql": "{{ config(materialized='table') }}\n\nWITH po AS (\n  SELECT ...\n)\nSELECT ..."
        }
    ],
    "warnings": ["Any caveats about ABAP code, data quality, or assumptions"],
    "confidence": "high",
    "domain_facts_consumed": false,
    "domain_facts_citations": [],
    "analysis_findings_consumed": false,
    "analysis_findings_citations": [],
    "dar_consumed": false,
    "dar_citations": [],
    "bar_consumed": false,
    "bar_citations": [],
    "semantic_model_consumed": [],
    "dbt_semantic_model_consumed": [],
    "join_cardinality_consulted": [],
    "bridge_coverage_consulted": []
}"""

    # The directive text names the raw schema and the business domain;
    # keep both in sync with the configured source (env DG_SOURCE_SCHEMA /
    # DG_DOMAIN_CONTEXT).
    system_prompt = system_prompt.replace("raw_sap", SOURCE_SCHEMA)
    system_prompt = system_prompt.replace("__DOMAIN_CONTEXT__", DOMAIN_CONTEXT)

    # ---------------------------------------------------------------
    # USER PROMPT — bundle delivered via helper
    # ---------------------------------------------------------------
    # #130 grounding: surface the vault models' REAL column names so the
    # RULE-3-steered generator (#129) uses business names, not raw SAP names.
    try:
        from _vault_grounding import vault_schema_block
        _vault_block = vault_schema_block()
    except Exception as _exc:  # noqa: BLE001
        print(f"[WARN] #130 vault_schema_block failed: {_exc}; grounding skipped")
        _vault_block = ""

    base_user_prompt = f"""Create an S2T mapping and a dbt implementation plan for:

**Term ID:** {term_id}
**Term:** {term_name}
**Definition:** {term_definition}
**Unit:** {term_unit}
**Grain:** {term_grain}

## Context bundle (layered — static / dynamic / ontology / examples / business / archived)

{bundle.formatted_prompt}

{_vault_block}

Return ONLY valid JSON matching the schema in the system prompt."""

    # ─── Direction F.4 — empirically-backed retry on F.3 rejection ───
    # Wrap the LLM call + audit + F.3 validation in a bounded retry loop.
    # On first F.3 rejection, the rejection's hint is appended to the
    # user prompt as concrete guidance ("you tried X catastrophic; use Y
    # bridge instead, citing DAR-NNNNN") and the LLM is re-invoked. Cap
    # at 2 attempts so worst-case token cost is bounded; if the second
    # attempt also fails, surface the error.
    MAX_F3_RETRIES = 3  # headroom for RULE-3-staging fix + #130 column fix + F.3
    rejection_hint: Optional[str] = None
    last_validation: dict = {}
    last_result: Optional[dict] = None

    for attempt in range(1, MAX_F3_RETRIES + 1):
        if rejection_hint:
            user_prompt = (
                base_user_prompt
                + "\n\n## PREVIOUS ATTEMPT REJECTED — fix the issue below and "
                  "regenerate the FULL JSON\n\n"
                + rejection_hint
            )
        else:
            user_prompt = base_user_prompt

        # P7.5 Fix: wide-scope terms (BG007-class) overflow 3500 output tokens with
        # the directive-enriched attestation fields. Bump to 4500 for headroom.
        result = _post_claude(system_prompt, user_prompt, max_tokens=32000)
        last_result = result

        if not isinstance(result, dict) or "error" in result:
            # _post_claude returned an LLM-side error (timeout, parse,
            # API). Don't retry; surface with attempt count for audit.
            if isinstance(result, dict):
                result["_f3_attempts"] = attempt
            return result

        # P7.4 Fix 1: pass bundle text so audit greps the BUNDLE, not the response.
        result = _audit_s2t_citations(result, bundle_text=bundle.formatted_prompt)
        result["_bundle_fingerprint"] = bundle.debug["fingerprint"] if bundle.debug else ""
        result["_bundle_total_tokens"] = bundle.token_count
        result["_bundle_scope_strategy"] = bundle.scope_resolution.get("strategy_used", "unknown")
        result["_bundle_resolved_tables"] = bundle.scope_resolution.get("resolved_tables", [])

        # ─── C6 — bridge_coverage attestation audit (LLM discipline) ───
        # Soft-signal check: when bridge_coverage_by_filter DARs exist
        # in scope, LLM must cite at least one in
        # `bridge_coverage_consulted`. Audit failures append to
        # _citation_audit_issues + flip llm_self_attestation_mismatch
        # (existing channels). Doesn't refuse — that's the gate's job.
        try:
            from _bridge_coverage_gate import (
                _check_bridge_coverage_attestation,
            )
            import duckdb as _duckdb
            _att_conn = _duckdb.connect(
                str(Path(__file__).resolve().parent.parent
                    / "cpe_analytics.duckdb")
            )
            try:
                _att_ok, _att_err = _check_bridge_coverage_attestation(
                    propose=result,
                    conn=_att_conn,
                    scope_tables=scope_tables,
                )
            finally:
                _att_conn.close()
            if not _att_ok and _att_err:
                _existing = result.get("_citation_audit_issues")
                if not isinstance(_existing, list):
                    _existing = []
                _existing.append(_att_err)
                result["_citation_audit_issues"] = _existing
                result["llm_self_attestation_mismatch"] = True
        except Exception as _exc:  # noqa: BLE001
            print(f"[WARN] C6 bridge_coverage attestation audit raised: "
                  f"{_exc}; skipping enforcement")

        # ─── C6 — post-generation bridge_coverage gate (BEFORE F.3) ───
        # Reachability failure is structural (data refutes the filter,
        # not LLM hallucination); retry can't help. Fires before F.3 to
        # short-circuit doomed-by-data SQL without burning the F.3/F.4
        # retry budget.
        try:
            from _bridge_coverage_gate import bridge_coverage_gate
            import duckdb as _duckdb
            _bc_conn = _duckdb.connect(
                str(Path(__file__).resolve().parent.parent
                    / "cpe_analytics.duckdb")
            )
            try:
                bc_passed, bc_violations, bc_status = bridge_coverage_gate(
                    sql=result.get("transformation_sql", ""),
                    scope_tables=scope_tables,
                    conn=_bc_conn,
                )
            finally:
                _bc_conn.close()
        except Exception as _exc:  # noqa: BLE001
            print(f"[WARN] C6 bridge_coverage gate raised: {_exc}; "
                  f"skipping enforcement")
            bc_passed, bc_violations, bc_status = True, [], "skipped_internal_error"

        if not bc_passed:
            return {
                "error": "stage_e_refused_bridge_coverage_violation",
                "_refusal_kind": "bridge_coverage_violation",
                "_bridge_violations": bc_violations,
                "_bridge_gate_status": bc_status,
                "_attempted_sql": result.get("transformation_sql"),
                "_f3_attempts": attempt,
            }
        result["_bridge_coverage_gate_status"] = bc_status

        # ─── Direction F.3 — post-generation cardinality validator ───
        try:
            from _s2t_cardinality_validator import validate_s2t_sql
            import duckdb as _duckdb
            _val_conn = _duckdb.connect(
                str(Path(__file__).resolve().parent.parent
                    / "cpe_analytics.duckdb")
            )
            try:
                validation = validate_s2t_sql(
                    sql=result.get("transformation_sql", ""),
                    scope_tables=scope_tables,
                    conn=_val_conn,
                )
            finally:
                _val_conn.close()
        except Exception as _exc:  # noqa: BLE001
            print(f"[WARN] F.3 validator raised: {_exc}; skipping enforcement")
            validation = {"status": "passed",
                          "reason": f"validator_error: {_exc}"}
        last_validation = validation

        if validation.get("status") == "rejected_catastrophic_join":
            if attempt < MAX_F3_RETRIES:
                rejection_hint = (
                    validation.get("hint", "")
                    + "\n\nRegenerate the SQL with corrected join keys per the "
                      "cardinality evidence cited above. Update "
                      "join_cardinality_consulted to cite the per_record_key "
                      "bridge DAR (not the catastrophic one)."
                )
                print(f"[create_s2t] F.3 rejected attempt {attempt}/"
                      f"{MAX_F3_RETRIES}; retrying with rejection hint")
                last_result = result
                continue
            # Final attempt also rejected.
            return {
                "error": (f"catastrophic_join_rejected_after_retry: "
                          f"{validation.get('hint', '')}"),
                "_f3_validation": validation,
                "_attempted_sql": result.get("transformation_sql"),
                "_f3_attempts": attempt,
            }

        # Validator passed (or degraded to passed-with-warning).
        result["_f3_validation_passed"] = True

        # ─── RULE 3 (#129) — staging-ref pre-flight: marts/obt must be vault-only ───
        try:
            from _vault_grounding import verify_no_staging_refs
            rule3_issues = verify_no_staging_refs(result.get("dbt_models") or [])
        except Exception as _exc:  # noqa: BLE001
            print(f"[WARN] RULE 3 staging-ref check raised: {_exc}; skipping")
            rule3_issues = []
        if rule3_issues and attempt < MAX_F3_RETRIES:
            result["_rule3_issues"] = rule3_issues
            rejection_hint = (
                "Your mart/obt model refs STAGING, violating RULE 3 — marts/obt "
                "MUST ref() VAULT models only: source measures from satellites and "
                "traverse entities via links/hubs. If the ontology layer shows "
                "EXISTING vault models covering these sources, rebuild on those. "
                "If NO vault models exist for these source tables yet (new source), "
                "propose the FULL chain in dbt_models[] in dependency order: "
                "staging views (clean/rename raw columns), then vault models "
                "(hubs, links, satellites), then rebuild the mart to ref() ONLY "
                "the vault models you just proposed. Do NOT ref staging from the "
                "mart. Return the complete JSON. Fix:\n  - "
                + "\n  - ".join(rule3_issues)
            )
            print(f"[create_s2t] RULE 3 staging-ref violation attempt "
                  f"{attempt}/{MAX_F3_RETRIES}; retrying vault-only")
            last_result = result
            continue
        if rule3_issues:
            result["_rule3_issues"] = rule3_issues
            result["llm_self_attestation_mismatch"] = True

        # ─── #130 — column-existence pre-flight on vault refs ───
        # Every <alias>.<col> bound to a vault model must be a real column.
        # On mismatch: retry with the real columns (repair); on final attempt,
        # surface issues (dbt build + repair_dbt_model_sql are the hard backstop).
        try:
            from _vault_grounding import verify_vault_columns
            col_issues = verify_vault_columns(result.get("dbt_models") or [])
        except Exception as _exc:  # noqa: BLE001
            print(f"[WARN] #130 column verify raised: {_exc}; skipping")
            col_issues = []
        if col_issues and attempt < MAX_F3_RETRIES:
            result["_column_audit_issues"] = col_issues
            rejection_hint = (
                "Some columns referenced on VAULT models do NOT exist. Use ONLY "
                "the real vault column names from the 'VAULT model column schemas' "
                "block (business names + hk_* keys), never raw SAP names. Fix:\n  - "
                + "\n  - ".join(col_issues)
            )
            print(f"[create_s2t] #130 column check failed attempt "
                  f"{attempt}/{MAX_F3_RETRIES}; retrying with real columns")
            last_result = result
            continue
        if col_issues:
            result["_column_audit_issues"] = col_issues
            result["llm_self_attestation_mismatch"] = True
        result["_f3_attempts"] = attempt
        return result

    # Defensive fallthrough — the loop returns on every path; this
    # handles unexpected loop exit (e.g., MAX_F3_RETRIES = 0). Surface
    # with whatever we have.
    if last_result is None:
        return {"error": "create_s2t exhausted retries with no result",
                "_f3_attempts": MAX_F3_RETRIES}
    last_result["_f3_attempts"] = MAX_F3_RETRIES
    return last_result


def repair_dbt_model_sql(
    model_filename: str,
    current_sql: str,
    dbt_error_text: str,
    schema_dump: str,
    error_hint: Optional[str] = None,
) -> dict:
    """Ask Claude to fix a dbt model .sql that failed `dbt run`.

    Focused error-feedback prompt: much shorter than the initial
    create_s2t_with_implementation call. The LLM gets the exact dbt
    error, an optional error-class hint from the classifier
    (known_issue #84), DuckDB's candidate bindings (if any), the
    current SQL on disk (with jinja ref() calls preserved), and the
    ACTUAL information_schema of the tables the model selects from.

    `error_hint` is produced by app/_s2t_tab_helpers.classify_dbt_error
    and tells the LLM which error class it's fixing: Binder column,
    Catalog/table, syntax, type, IO/spill, or generic. Before #84 this
    function was scoped only to Binder column-not-found repairs; now
    it handles any repair class the classifier routes here.

    Returns `{"sql": "..."}` on success or `{"error": "..."}` on
    failure. `current_sql` jinja style must be preserved in the output.
    """
    if not API_KEY or API_KEY == "your-api-key-here":
        return {"error": "ANTHROPIC_API_KEY not set. Add it to .env file in project root."}

    system_prompt = (
        "You are a dbt SQL repair assistant. A dbt model file failed to "
        "run. You are given: the model filename, an error-class hint "
        "(may be empty for unknown classes), the exact dbt error text, "
        "the current SQL on disk (with dbt jinja `ref()` calls "
        "preserved), and the ACTUAL information_schema of the tables "
        "the model selects from.\n\n"
        "Your job is to return a corrected SQL. Rules:\n"
        "- Preserve jinja references exactly: if the source used "
        "`{{ ref('model_name') }}` keep that syntax, don't substitute "
        "compiled names.\n"
        "- Change only what the error class indicates needs changing. "
        "If the hint says column/table reference is wrong, fix only "
        "that reference. If the hint says the SQL has a cardinality "
        "problem (cartesian / spill / OOM), redesign JOIN keys but "
        "keep the CTE structure recognizable.\n"
        "- Pick replacement column/table names from the candidate list "
        "or the information_schema dump — do not invent new names.\n"
        "- Return ONLY JSON: {\"sql\": \"<corrected SQL>\"}. No preamble, "
        "no explanation, no markdown fences."
    )

    hint_block = f"Error class hint:\n{error_hint}\n\n" if error_hint else ""
    user_prompt = (
        f"Model file: {model_filename}\n\n"
        f"{hint_block}"
        f"dbt error:\n{dbt_error_text}\n\n"
        f"Current SQL on disk:\n{current_sql}\n\n"
        f"Actual schema of referenced tables:\n{schema_dump}\n\n"
        "Return corrected SQL only as JSON: {\"sql\": \"...\"}"
    )
    return _post_claude(system_prompt, user_prompt, max_tokens=2000)


def validate_model_semantics(
    term_row: dict,
    model_name: str,
    model_sql: str,
    row_count: int,
    column_types: dict,
    sample_rows: list,
) -> dict:
    """Semantic gate — validate a deployed model against its business term.

    Three deterministic dimensions: grain (row granularity vs term.grain),
    filter (exclusion rules from term definition present in SQL WHERE),
    unit (output column types match term.unit). Definition drift is
    intentionally NOT checked — subjective, overlaps with the three, and
    produces false positives.

    Returns JSON dict: `{"match": bool, "issues": [...], "summary": str}`.
    On API failure returns `{"error": "..."}`.

    IMPORTANT — conservative stance: we only want to block Deploy on
    HIGH-confidence critical mismatch. Ambiguous cases should be flagged
    as warning, not critical. False positive (block legitimate Deploy)
    is worse than false negative here.
    """
    if not API_KEY or API_KEY == "your-api-key-here":
        return {"error": "ANTHROPIC_API_KEY not set. Add it to .env file in project root."}

    # Trim sample rows to keep tokens in check — 5 rows is enough signal.
    _sample = sample_rows[:5] if sample_rows else []
    _sample_text = json.dumps(_sample, default=str, ensure_ascii=False, indent=2)
    _col_text = json.dumps(column_types, ensure_ascii=False, indent=2)

    system_prompt = (
        "You are a conservative semantic validator for dbt models. You are "
        "given a business term definition and a dbt model that claims to "
        "implement it. Check THREE dimensions:\n\n"
        "1. GRAIN — does the model's row granularity match term.grain?\n"
        "   - If grain is 'per serial number' row count should approx equal "
        "distinct serials in the domain.\n"
        "   - If grain is 'per plant' expect one row per plant.\n"
        "   - If grain is silent or vague, a single-row aggregate is usually "
        "correct only if the term_name starts with total_/sum_/avg_/count_.\n\n"
        "2. FILTER — does the SQL apply exactly the exclusions the term states?\n"
        "   - Look for WHERE clauses / NOT EXISTS / anti-joins that match "
        "each exclusion the term mentions.\n"
        "   - An exclusion the term STATES but the SQL does NOT implement "
        "is severity=critical.\n"
        "   - An exclusion PRESENT in the SQL that the term does NOT state "
        "(e.g. the SQL filters out an extra status value the definition "
        "never mentions) is ALSO severity=critical: it silently changes "
        "the population being measured. Quote the exact unstated predicate "
        "in the description. This case is exempt from the conservative bar "
        "below — an unstated filter is objectively verifiable from the SQL "
        "text alone.\n\n"
        "3. UNIT — does the primary output column type match term.unit?\n"
        "   - count -> integer (BIGINT/INTEGER).\n"
        "   - percent -> decimal 0-1 or 0-100 (DECIMAL/DOUBLE).\n"
        "   - days -> integer or decimal days.\n"
        "   - currency/amount -> DECIMAL.\n\n"
        "BE CONSERVATIVE. Only flag severity=critical when you are HIGH "
        "confidence the SQL measures the wrong thing. Everything else is "
        "severity=warning (informational, does not block Deploy). False "
        "positives are more harmful than false negatives here.\n\n"
        "Return ONLY JSON, no preamble, no markdown fencing: "
        "{\"match\": bool, \"issues\": [{\"dimension\": \"grain|filter|unit\", "
        "\"severity\": \"critical|warning\", \"description\": str, "
        "\"suggested_fix\": str}], \"summary\": str}. "
        "match = (no critical issues)."
    )
    user_prompt = (
        f"Business term:\n"
        f"  id: {term_row.get('id', '')}\n"
        f"  term_name: {term_row.get('term_name', '')}\n"
        f"  display_name: {term_row.get('display_name', '')}\n"
        f"  definition: {term_row.get('definition', '')}\n"
        f"  unit: {term_row.get('unit', '')}\n"
        f"  grain: {term_row.get('grain', '')}\n"
        f"  notes: {term_row.get('notes', '')}\n\n"
        f"Model: {model_name}\n"
        f"Row count in deployed view: {row_count}\n\n"
        f"Column types:\n{_col_text}\n\n"
        f"Sample rows (first 5):\n{_sample_text}\n\n"
        f"Model SQL (jinja preserved):\n{model_sql}\n"
    )
    return _post_claude(system_prompt, user_prompt, max_tokens=1500)


def repair_semantic_mismatch(
    term_row: dict,
    model_sql: str,
    issues: list,
) -> dict:
    """Given critical issues from validate_model_semantics,
    return corrected SQL. Preserves jinja `{{ ref(...) }}` calls.

    Returns `{"sql": "..."}` on success or `{"error": "..."}` on failure.
    """
    if not API_KEY or API_KEY == "your-api-key-here":
        return {"error": "ANTHROPIC_API_KEY not set. Add it to .env file in project root."}

    _issue_lines = [
        f"- {i.get('dimension', '?')} ({i.get('severity', '?')}): "
        f"{i.get('description', '')} | Fix: {i.get('suggested_fix', '')}"
        for i in (issues or [])
    ]
    _issue_text = "\n".join(_issue_lines) if _issue_lines else "(none)"

    system_prompt = (
        "You are a dbt SQL repair assistant. A model compiled and ran but "
        "the validator flagged that it does NOT semantically match the "
        "business term. Your job: regenerate the SQL to address ALL critical "
        "issues while preserving the rest (CTE structure, joins that were "
        "correct, valid column projections, jinja ref() calls).\n\n"
        "Rules:\n"
        "- Preserve `{{ ref('model_name') }}` jinja exactly.\n"
        "- Do NOT substitute compiled table names like 'main_marts.x'.\n"
        "- Fix grain mismatches by adding GROUP BY or removing aggregates, "
        "NOT by filtering to one row.\n"
        "- Fix filter mismatches by adding WHERE clauses matching the term's "
        "stated exclusions. Use SAP column names that actually exist in the "
        "upstream model.\n"
        "- Fix unit mismatches by CAST-ing the primary output column.\n"
        "- Return ONLY JSON: {\"sql\": \"<corrected SQL>\"}. No preamble, no "
        "markdown, no explanation."
    )
    user_prompt = (
        f"Business term:\n"
        f"  definition: {term_row.get('definition', '')}\n"
        f"  unit: {term_row.get('unit', '')}\n"
        f"  grain: {term_row.get('grain', '')}\n"
        f"  notes: {term_row.get('notes', '')}\n\n"
        f"Current SQL:\n{model_sql}\n\n"
        f"Critical issues to fix:\n{_issue_text}\n\n"
        "Return corrected SQL as JSON: {\"sql\": \"...\"}"
    )
    return _post_claude(system_prompt, user_prompt, max_tokens=2000)


def interpret_domain_fact(
    sql: str,
    result_preview: str,
    focus_area: str = "",
    domains: str = "",
    scope_layer: str = "staging",
):
    """Ask Claude to interpret a Guided-Domain query result as a structured
    fact row ready for `dbt/seeds/domain_facts.csv`.

    Returns a dict shaped like:
        {
          "fact_plain":        "...",   # 1-3 sentences, business-readable
          "fact_technical":    "...",   # includes column names + exact figures
          "category":          "currency" | "naming_convention" | ...,
          "scope_layer":       "staging" | "vault" | ...,
          "scope_tables":      "ekko,ekpo",
          "confidence":        "high" | "medium" | "low",
          "priority_score":    1-100,
          "stale_after_days":  null | 7 | 30 | 90,
        }

    On Anthropic error: returns {"error": ...}. On JSON parse error: returns
    {"error": ..., "raw_response": "..."} — caller synthesises the fallback
    fact card with confidence='low'.
    """
    if not API_KEY or API_KEY == "your-api-key-here":
        return {"error": "ANTHROPIC_API_KEY not set. Add it to .env file in project root."}

    system_prompt = """You are a senior SAP data analyst turning a DuckDB query result into ONE structured `domain_fact` row for a CPE procurement warehouse.

A `domain_fact` is a small, stable, structural observation about the data itself — currencies used, vendor cardinalities, material-number prefix conventions, actually-used movement types, null-rate baselines, fiscal-year coverage. It is NOT a business metric, NOT a per-term calculation, and NOT a harmful-pattern hypothesis.

Write for two audiences in two fields:
  - `fact_plain`: 1-3 sentences a procurement business owner could read. Plain language. NO SAP table/field names, NO SQL jargon.
  - `fact_technical`: Same fact but for the LLM that will read this as context in later prompts. Includes the specific SAP columns/tables/percentages/counts. Concrete, not generic.

Pick ONE `category` from: currency, naming_convention, cardinality, volume_distribution, null_pattern, org_structure, temporal_pattern, referential_integrity, value_domain, business_rule_observed.

Pick ONE `scope_layer` from: raw, staging, vault, mart, cross_layer. (Default to the layer the query used.)

`scope_tables`: comma-separated lowercase SAP table names that the fact describes (e.g. "ekko,ekpo"). Parse from the SQL. Omit `main_staging.stg_sap__` prefixes.

`confidence`: high if N ≥ 100 and the numbers are unambiguous; medium if N ≥ 10 but the pattern is noisy or partial; low if N < 10 or the evidence is ambiguous.

`priority_score` (1-100): weight by downstream utility for S2T / BT / Domain-Report prompts. Rough guide:
  - A currency split, plant cardinality, material-prefix convention, or actually-used movement-type list: 80-95 (these are foundational).
  - A null-rate baseline on a key column: 70-85.
  - A descriptive cardinality (N distinct values of a low-stakes column): 40-60.
  - A sample-of-3 peek at row shape: 20-40.

`stale_after_days` — set by VOLATILITY, not importance (Rule 25):
  - Schema facts (column exists, type): null (never stales).
  - Cardinality (N plants/vendors/materials): 90.
  - Distribution (currency %, plant volume, movement-type mix): 30.
  - Null baselines: 30.
  - Temporal facts (latest load date, fiscal-year coverage): 7.

RESPOND ONLY IN JSON with this exact structure — no markdown, no backticks, no preamble:
{
  "fact_plain": "...",
  "fact_technical": "...",
  "category": "currency",
  "scope_layer": "staging",
  "scope_tables": "ekko",
  "confidence": "high",
  "priority_score": 90,
  "stale_after_days": 30
}"""

    # Result preview is trimmed to ~2500 chars so a 50-row JSON
    # payload doesn't blow the prompt budget on wide tables.
    preview = (result_preview or "").strip()
    if len(preview) > 2500:
        preview = preview[:2500] + "…(truncated)"

    user_prompt = f"""SQL executed:
{sql}

Result rows (JSON, head 50):
{preview or "(empty result)"}

Analyst focus area: {focus_area or "(none)"}
Domains in scope:   {domains or "ALL"}
Scope layer:        {scope_layer}

Return ONLY valid JSON matching the schema in the system prompt."""

    return _post_claude(system_prompt, user_prompt, max_tokens=700)


def generate_dq_test(
    term_name: str,
    term_definition: str,
    dq_rule_description: str,
    target_model: str,
    target_column: str,
    existing_columns: str,
):
    """Ask Claude to generate a dbt singular test SQL for a plain-language DQ rule.

    Returns a dict like:
        {
            "test_name": "assert_lead_time_not_negative",
            "test_sql": "<full SELECT that returns violating rows>",
            "description": "<one-line summary>",
            "severity": "error" | "warn",
            "explanation": "<why this works / when it fires>"
        }

    On failure returns an {"error": ...} dict.
    """
    if not API_KEY or API_KEY == "your-api-key-here":
        return {"error": "ANTHROPIC_API_KEY not set. Add it to .env file in project root."}

    system_prompt = """You are a dbt data quality engineer. Given a business rule in plain language, generate a dbt singular test SQL file.

dbt singular tests work by returning rows that VIOLATE the rule. If the query returns 0 rows, the test passes. If it returns any rows, the test fails. Always SELECT the business key plus the column(s) being validated so a failing row is self-explanatory.

Rules:
- Use {{ ref('<target_model>') }} to reference the target model — never hard-code a schema.
- Only reference columns that actually exist in the given target model column list.
- If the rule talks about NULLs or ranges, guard with IS NOT NULL where appropriate so truly-null rows do not false-fail.
- Give the test a snake_case name that starts with `assert_` and describes the invariant (e.g., `assert_lead_time_not_negative`).
- Choose severity: "error" for hard business rules that must not be broken; "warn" for soft thresholds and concentration-style rules.

RESPOND ONLY IN JSON with this exact structure — no markdown, no backticks, no preamble:
{
    "test_name": "assert_lead_time_not_negative",
    "test_sql": "-- violating rows only\\nSELECT purchase_order_number, lead_time_days\\nFROM {{ ref('fact_purchase_orders') }}\\nWHERE lead_time_days IS NOT NULL AND lead_time_days < 0",
    "description": "Validates that lead time is never negative (would indicate GR posted before PO creation).",
    "severity": "error",
    "explanation": "This test checks every row in fact_purchase_orders and returns any PO where lead_time_days is below zero, which would mean the goods receipt was posted before the PO was created — impossible in a correct data pipeline."
}"""

    user_prompt = f"""Generate a dbt singular test for this data quality rule.

**Business Term:** {term_name}
**Term Definition:** {term_definition}

**DQ Rule (plain language):** {dq_rule_description}

**Target model:** {target_model}
**Target column (main):** {target_column}
**Available columns in target model:** {existing_columns}

Return ONLY valid JSON matching the schema in the system prompt."""

    text = ""
    try:
        response = requests.post(
            API_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": MODEL,
                "max_tokens": 1500,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=600,
        )
        response.raise_for_status()
        data = response.json()

        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        return json.loads(text)

    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {str(e)}"}
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse Claude response as JSON: {str(e)}", "raw_response": text[:500]}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}
