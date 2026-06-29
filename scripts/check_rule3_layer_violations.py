"""Enforce RULE 3 — mart/obt/knowledge models only ref() vault models.

RULE 3 (knowledge/anti_patterns.md): "Mart fact-dim relationships are
LOGICAL, not in dbt ref(): Mart models only ref() vault models, not each
other."

Violations caught:
  1. {{ ref('stg_sap__*') }} from a downstream layer — staging-bypass
  2. Direct `raw_sap.<table>` SQL reference — Layer 0->3+ skip (worse)
  3. {{ ref('<other_mart>') }} between two marts — sibling-layer ref

Allowlist entries are model names with an open KI explaining why they
violate the rule. Adding to the allowlist is a deliberate act tied to a
filed known_issue; remove the entry once the KI resolves.

Exit code:
  0 — no violations or only allowlisted ones
  1 — at least one un-allowlisted violation found
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

# Project root resolves from this file's location.
ROOT = Path(__file__).resolve().parent.parent
MODELS_ROOT = ROOT / "dbt" / "models"
DOWNSTREAM_LAYERS = ("marts", "obt", "knowledge")

# Models with open KIs documenting why the violation is currently
# unavoidable. Each entry must point to a known_issue with the fix shape.
ALLOWLIST: dict[str, str] = {}


def _model_layer(name: str, seed_names: set[str] | None = None) -> str:
    if seed_names is not None and name in seed_names:
        return "seed"
    if name.startswith("stg_sap__"):
        return "staging"
    if name.startswith(("hub_", "link_", "sat_")):
        return "vault"
    if name.startswith(("dim_", "fact_")):
        return "marts"
    if name.startswith("obt_"):
        return "obt"
    if name.startswith("knowledge_"):
        return "knowledge"
    return "other"


def _load_seed_names() -> set[str]:
    """Names of dbt seeds (CSV files in dbt/seeds/), used to classify
    refs that resolve to a seed rather than another model."""
    seeds_dir = ROOT / "dbt" / "seeds"
    return {p.stem for p in seeds_dir.glob("*.csv")}


_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _strip_sql_comments(text: str) -> str:
    """Remove /* ... */ and -- ... comments so refs/raw mentions inside
    docstrings or commentary aren't flagged as violations."""
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    return text


def _scan() -> dict[str, list[tuple[str, str]]]:
    """Returns {model_name: [(violation_kind, detail), ...]}."""
    seed_names = _load_seed_names()
    violations: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for layer in DOWNSTREAM_LAYERS:
        for sql in (MODELS_ROOT / layer).rglob("*.sql"):
            text = _strip_sql_comments(sql.read_text(encoding="utf-8"))
            host = sql.stem

            # 1. {{ ref('stg_sap__*') }} — downstream layer refs staging
            # 2. {{ ref('<other_mart>') }} — sibling mart-to-mart ref
            # 3. {{ ref('<seed_name>') }} — mart/obt/knowledge refs a seed
            #    (per RULE 3, marts only ref vault; refs to seeds skip vault)
            for ref_name in re.findall(
                r"\{\{\s*ref\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}", text
            ):
                ref_layer = _model_layer(ref_name, seed_names)
                if ref_layer == "staging":
                    violations[host].append(("STAGING_REF", ref_name))
                elif ref_layer == "seed":
                    violations[host].append(("SEED_REF", ref_name))
                elif (
                    layer == "marts"
                    and ref_layer == "marts"
                    and ref_name != host
                ):
                    violations[host].append(("MART_TO_MART_REF", ref_name))

            # 3. Direct raw_sap.<table> SQL reference (bypasses dbt ref()
            # AND skips both staging and vault). Word-boundary on the dot
            # to avoid matching "raw_sap_" identifiers.
            for raw in re.findall(r"\braw_sap\.([a-zA-Z_][a-zA-Z0-9_]*)", text):
                violations[host].append(("RAW_SAP_DIRECT", f"raw_sap.{raw}"))

    return violations


def main() -> int:
    violations = _scan()
    if not violations:
        print("[RULE 3] OK — no layer violations found.")
        return 0

    blocking = {h: v for h, v in violations.items() if h not in ALLOWLIST}
    allowed = {h: v for h, v in violations.items() if h in ALLOWLIST}

    if allowed:
        print("[RULE 3] allowlisted violations (open KIs):")
        for host, refs in sorted(allowed.items()):
            print(f"  {host}: {ALLOWLIST[host]}")
            kinds = defaultdict(list)
            for kind, detail in refs:
                kinds[kind].append(detail)
            for kind, items in kinds.items():
                print(f"    -> {kind}: {', '.join(sorted(set(items)))}")
        print()

    if not blocking:
        print("[RULE 3] OK — all remaining violations are allowlisted.")
        return 0

    print("[RULE 3] BLOCKING violations:")
    for host, refs in sorted(blocking.items()):
        print(f"  {host}")
        kinds = defaultdict(list)
        for kind, detail in refs:
            kinds[kind].append(detail)
        for kind, items in kinds.items():
            print(f"    -> {kind}: {', '.join(sorted(set(items)))}")
    print()
    print(
        "Mart/obt/knowledge models must ref() vault models only, not "
        "staging or sibling marts. Direct raw_sap.* references skip "
        "even more layers. See knowledge/anti_patterns.md (RULE 3)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
