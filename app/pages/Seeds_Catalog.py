"""Seeds Catalog — per-table documentation for the main_seeds schema.

Answers: "Why does this seed exist? Who fills it? Who reads it? What
columns does it have, and what does the data look like?"

Reads:
- DuckDB main_seeds schema (column types, row counts, sample rows)
- dbt/seeds/<name>.csv (last-modified timestamps)
- scripts/, app/ source trees (writer + reader discovery via grep)
- dbt/models/ source tree (downstream ref() discovery)
- knowledge/seeds_catalog.md (hand-maintained category + purpose)
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from collections import defaultdict

import pandas as pd
import streamlit as st

# Reuse the project's read-only Parquet-backed query layer.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import query  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
SEEDS_DIR = ROOT / "dbt" / "seeds"
MODELS_DIR = ROOT / "dbt" / "models"
APP_DIR = ROOT / "app"
SCRIPTS_DIR = ROOT / "scripts"


# The taxonomy is itself a seed (main_seeds.seed_taxonomy) so it is
# governed like everything it describes: versioned, queryable, and
# pinned to the CSVs on disk by tests/test_seed_taxonomy_invariant.py.
@st.cache_data(ttl=60)
def _load_taxonomy() -> dict[str, tuple[str, str]]:
    try:
        df = query(
            "SELECT seed_name, category, purpose FROM main_seeds.seed_taxonomy"
        )
        return {r["seed_name"]: (r["category"], r["purpose"])
                for _, r in df.iterrows()}
    except Exception:
        return {}


SEED_TAXONOMY: dict[str, tuple[str, str]] = _load_taxonomy()

CATEGORY_ORDER = [
    "Knowledge graph",
    "Application state",
    "Auto-generated (Stage B DAR)",
    "Auto-generated (Stage C TAR)",
    "Auto-generated (Stage C BAR)",
    "Auto-generated (LLM)",
    "Auto-generated (analyst)",
    "Auto-generated (scanner)",
    "Auto-generated (Layer A, LLM)",
    "Auto-generated (Layer B, deterministic)",
    "Auto-generated + manual",
    "Auto-generated (scrape)",
    "Audit log",
    "Reference (data)",
    "Reference (LLM context)",
    "Reference (cross-cutting policy)",
    "Reference (governance)",
    "Reference (config)",
    "Reference (architecture)",
    "Reference (Z-table dictionary)",
    "Reference (custom code)",
    "Reference (custom tables)",
]


_WRITE_KEYWORD_RE = re.compile(
    r"\b(to_csv|DictWriter|write_text|write_bytes|save_csv|"
    r"sync_parquet_and_invalidate)\b"
)
_PROXIMITY_CHARS = 300  # how close a write keyword must be to a <seed>.csv mention


@st.cache_data(ttl=300, show_spinner=False)
def _scan_writers_readers() -> dict[str, dict[str, list[str]]]:
    """Walk scripts/ + app/ once and return:
      {seed_name: {"writers": [path...], "readers": [path...]}}

    Tighter heuristics (avoid false positives from earlier "anywhere in
    file" matching):
      - WRITER if (a) `seed_name='<seed>'` / `seed_name="<seed>"` appears
        anywhere (canonical write-helper signal), OR (b) a write keyword
        (to_csv / DictWriter / write_text / write_bytes / save_csv /
        sync_parquet_and_invalidate) appears within ~300 chars of a
        `<seed>.csv` mention.
      - READER if the file queries `main_seeds.<seed>` OR uses
        `ref('<seed>')` OR mentions `<seed>.csv` without qualifying as
        a writer.
    """
    seeds = sorted(p.stem for p in SEEDS_DIR.glob("*.csv"))
    out: dict[str, dict[str, list[str]]] = {
        s: {"writers": [], "readers": []} for s in seeds
    }

    files = []
    for root in (SCRIPTS_DIR, APP_DIR):
        for p in root.rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            files.append(p)

    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        # Cache write-keyword positions per file
        write_positions = [m.start() for m in _WRITE_KEYWORD_RE.finditer(text)]

        for seed in seeds:
            sn_ref = (
                f"seed_name='{seed}'" in text
                or f'seed_name="{seed}"' in text
            )
            csv_mark = f"{seed}.csv"
            csv_positions = []
            start = 0
            while True:
                idx = text.find(csv_mark, start)
                if idx == -1:
                    break
                csv_positions.append(idx)
                start = idx + 1
            seeds_ref = f"main_seeds.{seed}" in text
            ref_macro = (
                f"ref('{seed}')" in text or f'ref("{seed}")' in text
            )

            is_writer = sn_ref
            if not is_writer and csv_positions and write_positions:
                for cp in csv_positions:
                    if any(abs(cp - wp) < _PROXIMITY_CHARS
                           for wp in write_positions):
                        is_writer = True
                        break

            rel = str(p.relative_to(ROOT)).replace("\\", "/")
            if is_writer:
                out[seed]["writers"].append(rel)
            elif seeds_ref or ref_macro or csv_positions:
                out[seed]["readers"].append(rel)

    for seed in seeds:
        out[seed]["writers"] = sorted(set(out[seed]["writers"]))
        out[seed]["readers"] = sorted(set(out[seed]["readers"]))
    return out


@st.cache_data(ttl=300, show_spinner=False)
def _scan_dbt_refs() -> dict[str, list[str]]:
    """Walk dbt/models/ and find every model that ref()s a seed.

    Returns: {seed_name: [model_path...]}
    """
    seeds = sorted(p.stem for p in SEEDS_DIR.glob("*.csv"))
    refs: dict[str, list[str]] = {s: [] for s in seeds}
    pattern = re.compile(r"\{\{\s*ref\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")

    for sql_path in MODELS_DIR.rglob("*.sql"):
        try:
            text = sql_path.read_text(encoding="utf-8")
        except Exception:
            continue
        # Strip comments so docstring mentions don't count.
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
        text = re.sub(r"--[^\n]*", " ", text)
        for m in pattern.findall(text):
            if m in refs:
                rel = str(sql_path.relative_to(ROOT)).replace("\\", "/")
                refs[m].append(rel)

    for seed in seeds:
        refs[seed] = sorted(set(refs[seed]))
    return refs


@st.cache_data(ttl=60, show_spinner=False)
def _seed_columns(seed: str) -> pd.DataFrame:
    return query(
        f"""
        SELECT column_name, data_type, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = 'main_seeds' AND table_name = '{seed}'
        ORDER BY ordinal_position
        """
    )


@st.cache_data(ttl=60, show_spinner=False)
def _seed_row_count(seed: str) -> int:
    df = query(f'SELECT COUNT(*) AS n FROM main_seeds."{seed}"')
    return int(df.iloc[0]["n"]) if not df.empty else 0


@st.cache_data(ttl=60, show_spinner=False)
def _seed_sample(seed: str, limit: int = 10) -> pd.DataFrame:
    return query(f'SELECT * FROM main_seeds."{seed}" LIMIT {limit}')


@st.cache_data(ttl=300, show_spinner=False)
def _seed_csv_mtime(seed: str) -> str:
    p = SEEDS_DIR / f"{seed}.csv"
    if not p.exists():
        return "(no .csv on disk)"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))


# ─── UI ────────────────────────────────────────────────────────────────

st.title("🌱 Seeds Catalog")
st.caption(
    "Per-table documentation for the `main_seeds` schema. "
    "Answers: why each seed exists, who fills it, who reads it, "
    "what's in it. Source map mirrors `knowledge/seeds_catalog.md`."
)
st.divider()

# Pull live list from DuckDB (authoritative).
db_seeds = sorted(
    query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main_seeds' ORDER BY table_name"
    )["table_name"].tolist()
)
csv_seeds = sorted(p.stem for p in SEEDS_DIR.glob("*.csv"))

# Build categorized index
by_cat: dict[str, list[str]] = defaultdict(list)
unclassified: list[str] = []
for s in db_seeds:
    if s in SEED_TAXONOMY:
        by_cat[SEED_TAXONOMY[s][0]].append(s)
    else:
        unclassified.append(s)

# Sidebar
with st.sidebar:
    st.subheader("Filter")
    cats_present = [c for c in CATEGORY_ORDER if c in by_cat]
    cat = st.selectbox(
        "Category",
        ["(all)"] + cats_present + (["(uncategorized)"] if unclassified else []),
    )
    if cat == "(all)":
        candidate_seeds = db_seeds
    elif cat == "(uncategorized)":
        candidate_seeds = sorted(unclassified)
    else:
        candidate_seeds = sorted(by_cat[cat])
    seed = st.selectbox("Seed", candidate_seeds)

    st.divider()
    st.caption(
        f"**{len(db_seeds)}** seeds in `main_seeds` · "
        f"**{len(csv_seeds)}** CSV files in `dbt/seeds/`"
    )
    orphan_db = set(db_seeds) - set(csv_seeds)
    orphan_csv = set(csv_seeds) - set(db_seeds)
    if orphan_db:
        st.warning(f"In DB but no CSV: {sorted(orphan_db)}")
    if orphan_csv:
        st.warning(f"CSV but no DB table: {sorted(orphan_csv)}")

# Main panel
if not seed:
    st.info("Pick a seed from the sidebar.")
    st.stop()

cat_label, purpose = SEED_TAXONOMY.get(
    seed, ("(uncategorized)", "_No taxonomy entry — add a row to "
           "dbt/seeds/seed_taxonomy.csv and run `dbt seed`._")
)

# Header card
st.subheader(f"`{seed}`")
col_a, col_b, col_c = st.columns([2, 1, 1])
col_a.markdown(f"**Category:** {cat_label}")
col_b.metric("Rows", _seed_row_count(seed))
col_c.metric("CSV mtime", _seed_csv_mtime(seed))
st.markdown(f"**Purpose:** {purpose}")
st.divider()

tab_schema, tab_lineage, tab_source = st.tabs([
    "📊 Schema & Sample",
    "🔗 Lineage",
    "📄 Source File",
])

# ─── TAB 1: Schema + Sample ───
with tab_schema:
    st.markdown("##### Columns")
    cols_df = _seed_columns(seed)
    if cols_df.empty:
        st.info("No columns found in `main_seeds` (table not materialized yet?).")
    else:
        st.dataframe(cols_df, hide_index=True, use_container_width=True)

    st.markdown("##### Sample (first 10 rows)")
    sample = _seed_sample(seed)
    if sample.empty:
        st.info("Table is empty.")
    else:
        st.dataframe(sample, hide_index=True, use_container_width=True)

# ─── TAB 2: Lineage ───
with tab_lineage:
    wr_rd = _scan_writers_readers().get(seed, {"writers": [], "readers": []})
    dbt_refs = _scan_dbt_refs().get(seed, [])

    st.markdown("##### ✍️ Filled by")
    st.caption(
        "Python scripts and Streamlit pages that write to this seed "
        "(detected by grep — heuristic but accurate for the common patterns)."
    )
    if wr_rd["writers"]:
        for w in wr_rd["writers"]:
            st.markdown(f"- `{w}`")
    else:
        st.markdown(
            "_No writers found — this seed is **hand-maintained** "
            "(edited directly in `dbt/seeds/` and committed to git)._"
        )

    st.markdown("##### 📥 Consumed by (Python)")
    st.caption(
        "Scripts and pages that READ this seed (queries `main_seeds.<name>` "
        "or references the CSV without writing it)."
    )
    if wr_rd["readers"]:
        for r in wr_rd["readers"]:
            st.markdown(f"- `{r}`")
    else:
        st.markdown("_No Python readers found._")

    st.markdown("##### 🔗 Consumed by (dbt models)")
    st.caption(
        "dbt models that `ref()` this seed — i.e., the seed is an "
        "upstream of these models in the dbt DAG."
    )
    if dbt_refs:
        for r in dbt_refs:
            st.markdown(f"- `{r}`")
    else:
        st.markdown(
            "_No dbt models ref() this seed. It exists in DuckDB but is "
            "consumed by Python/Streamlit only, not by the dbt pipeline._"
        )

# ─── TAB 3: Source file ───
with tab_source:
    csv_path = SEEDS_DIR / f"{seed}.csv"
    if csv_path.exists():
        rel = csv_path.relative_to(ROOT).as_posix()
        st.markdown(f"**Path:** `{rel}`")
        st.markdown(f"**Size:** {csv_path.stat().st_size:,} bytes")
        st.markdown(f"**Modified:** {_seed_csv_mtime(seed)}")
        try:
            head = csv_path.read_text(encoding="utf-8").splitlines()[:15]
            st.markdown("##### Raw CSV (first 15 lines)")
            st.code("\n".join(head), language="csv")
        except Exception as e:
            st.warning(f"Could not read CSV preview: {e}")
    else:
        st.warning(
            f"No `{seed}.csv` in `dbt/seeds/`. This table is in `main_seeds` "
            "but the CSV file is absent — likely populated by Python "
            "(scripts write directly to DuckDB without a backing seed file) "
            "or it's been decommissioned."
        )
