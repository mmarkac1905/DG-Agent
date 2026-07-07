# Business Term: Total Cost of Ownership (TCO)

_Last generated: 2026-07-07 10:48:23_

## Definition

Full lifecycle cost per CPE unit: procurement cost + warehousing cost allocation + installation cost + return/repair cost, per material per year

- **ID:** `BG007`
- **Owner:** Finance
- **Approved by:** CFO
- **Status:** `draft`
- **Unit:** EUR
- **Grain:** material × year
- **Domain:** cost_analysis

**Notes:** Warehousing cost = (avg days in stock × daily warehouse cost per unit). Installation cost from service order. Repair cost from vendor return credit notes.

## Source-to-Target Mapping

_(no S2T mapping defined yet)_

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

_(lineage will be documented when dbt models are built)_

## Validation Status

DRAFT — awaiting business owner approval

## Related Decisions (4)

- **#16** (2026-04-14) — business_glossary_audience_split: Two-audience split is the default pattern for any governance UI in this project. Never collapse back into a single tab.
- **#67** (2026-04-18) **[NEVER_REPEAT]** — fix_b_reverted_archive_is_final: Archive is final — once a term is archived, its analysis_findings stay with the archived term_id as audit trail and do not follow a re-created same-named term. A re-created term starts with zero findings and must run fresh Guided Analysis. This produces a clean per-term-id semantic that matches how s2t_mapping and domain_facts already behave. See RULE 42 revision #5. Cross-term data bleeding was causing more bugs than it solved (decision #66 hotfix 8 caught the first; hotfix 8 extension caught the second; a third surfaced with analyst confusion about which term "owned" a profiling run).
- **#77** (2026-04-19) — ab_test_p75_accepts_partial_quality_evidence_token_efficiency_core_claim: A/B results accepted. Known issues #28 and #29 logged. The term-analysis runner design must include an ontology-layer consumption directive. Migration closure stands — retry loop + gate infrastructure are the validation layer for LLM output quality in production, not regression tests. The A/B results JSON log preserved; token-efficiency comparison table is the empirical evidence. IMPORTANT: this decision does NOT claim NEW path produces higher-quality SQL than OLD path. A/B quality evidence is single-run and partial (4 of 6 quality signals lost to harness bug #28). The defensible claims are: (a) NEW path is 54-79% more token-efficient; (b) NEW path has zero invented citation IDs across 3 runs; (c) NEW path architectural properties (fingerprinting, explicit directives, audit) enable the term-analysis injection loop which OLD path could not support. Quality parity between paths for single-shot Create S2T remains an assumption validated only in production by the deploy auto-retry + semantic gate. If a production regression emerges on NEW path that OLD path would not have shown, re-open this decision.
- **#79** (2026-04-26) — issue_93_filter_join_cardinality_from_generic_dar_dump_option_alpha: #93 closed. C1's STATUS-prefix rendering now reaches the LLM in production. Theme 1 C2 (deterministic-analyzer directives), C3 (TAR attestation), C4 (Stage A blocker surfacing) can now ship with the confidence that their DAR-targeting work has visible production effect. Recommended next: C2 → C3 → C4. Beta (#94) deferred; partition-by-analysis_type (#95) not filed yet (only file if 50/134 EDA coverage proves insufficient for a specific term). Pre-existing weakness noted but not addressed: the dedicated cardinality block does not emit DAR-NNNNN ids verbatim, so cardinality DARs cannot be cited in dar_consumed attestation. Pre-existing condition (was true pre-#93 too); directive #3 doesn't teach cardinality consumption either, so LLM doesn't cite cardinality DAR ids in practice anyway. Future refinement, not a Theme 1 blocker.

## Open Issues (1)

- **#94** [open/low] Scope-aware ordering for the term-analysis DAR loader (Option beta) - deferred follow-up to #93 — During #93 fix design, scope-aware ORDER BY was investigated as Option beta: replace LIMIT 50 ORDER BY executed_at_utc DESC with a scope-overlap-first prioritization (rows whose source_tables overlap the term scope rank above non-overlapping rows; recency within priority). Premis…
