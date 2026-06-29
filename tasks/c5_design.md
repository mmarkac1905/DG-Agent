# C5 Design Pass — Sourcing Recommendations on Unanswerable Terms

**Status:** Draft for review. Pre-implementation. Two read-only experiments (Q1, Q2) refine specific design questions before Phase 1 begins.

---

## Background and motivation

### The reframed value proposition

The Phase-15b/Theme-1 demo arc converged on an audit-discipline thesis: *the system either produces data-grounded SQL with traceable evidence, or refuses to ship a wrong number*. BG027 BAR-00003 (post-attestation-fix) demonstrated the second half of that thesis working correctly — the runner identified that the term `cpe_active_deployed_count` was structurally unanswerable from the confirmed scope and hard-stopped with `convergence_reason = hard_stop_scope_mismatch`.

That is the **right behavior** but **incomplete output**. A demo viewer sees "the system refused to answer" without seeing what would *make* the term answerable. The reframe: when the system refuses, it should also tell the analyst **what to ingest** to close the gap. That is the C5 capability.

### The trigger case: BG027 BAR-00003

- Term: `cpe_active_deployed_count` ("active deployed CPE count, per serial").
- Confirmed scope (Stage A): `[equi, mkpf, mseg, objk, seri]`.
- Iteration 2 SQL: `seri ⨝ mseg ON (MBLNR, ZEILE)`, filter `BWART='201'`. Compiles, runs, returns `count = 0`.
- LLM iter-2 self-reflection (verbatim): *"TAR-00010 shows 27,000 BWART='201' transactions; SERI.EQUNR join may have data quality issue or SERI excluded from scope."*
- Empirical root cause (verified read-only against `raw_sap`): `seri.MBLNR` only references BWART='101' (Goods Receipt) movements; deployment movements (BWART='201') exist in `mseg` at material-grain only (`mseg.SERNP` = sentinel `'HT01'` across all 32K rows). No per-serial deployment-movement bridge exists in the seeded data. Recorded as known_issue #99 (BG027 structurally unanswerable in seeded sample) and known_issue #98 (cardinality analyzer validates fanout but not filter-column reachability).

### Why C3/C4 don't address this

C3 (TAR attestation/citation) and C4 (Stage A blocker surfacing) are sub-items of Theme 1's audit-discipline umbrella — they harden the system's accounting of *existing* evidence. C5 is qualitatively different: it introduces a new capability — *outbound* recommendations about evidence the system doesn't yet have. The C5 trigger condition only fires when audit-discipline has already determined the term is unanswerable from current scope.

### Viability experiment findings (decision #84)

A one-shot LLM call ($0.013, 562 in / 772 out tokens, claude-sonnet-4-20250514) tested whether free-recall LLM SAP knowledge produces useful sourcing recommendations from BG027's gap context. Result: **partially viable.**

- **Top-1 recommendation correct.** The LLM identified SER01 as the missing inventory-movement-to-serial bridge in standard SAP. An analyst following only the primary recommendation would reach the right answer.
- **Confidence calibration failed.** SER02 was marked "High confidence — Status History" when SER02 is actually a doc-header-index for physical-inventory documents. SER03 description was wrong. ITOB was named as a table when it's a DDIC structure. The LLM's confidence labels reflected "is this a real-namespace SAP name?" not "is my description correct?" — the two questions are conflated.
- **Conclusion:** C5 is technically viable but requires a validation layer between LLM output and analyst-facing recommendations. Free-recall is not shippable.

---

## High-level architecture

```
[ runner detects scope_sanity=no on consecutive iterations ]
                          |
                          v
[ C5 prompt fires: term + iteration trace + catalog rows for relevant module ]
                          |
                          v
[ LLM emits structured recommendations with citations to catalog row IDs ]
                          |
                          v
[ validation layer: catalog allowlist match + empirical raw_sap cross-check ]
                          |
                          v
[ BAR row updated: status=needs_data_extension, sourcing_recommendations=[...] ]
                          |
                          v
[ Streamlit UI surfaces actionable recommendation list per BAR ]
```

The system still hard-stops — C5 doesn't change the convergence verdict. What it changes is the *output* of the hard-stop: from "we refuse, here's why" to "we refuse, here's why, **and here's what to ingest to close the gap**."

---

## Component 1 — Catalog seed scraping

**Purpose:** Build a curated SAP table allowlist for the validation layer (option (a) from decision #84) and a structured grounding source for the C5 prompt (Pattern B).

**Source:** `sapdatasheet.org` (URL pattern `/abap/tabl/<table>.html`, verified working in this Stage's investigation).
**Not used:** `erpexplorer.com` returns HTTP 403 to programmatic fetch (anti-bot). `leanx.eu` is an alternative if `sapdatasheet.org` fails.

**Coverage target:** ~150-300 tables across the modules currently represented in `raw_sap`:
- Procurement (E\*): EKKO, EKPO, EBAN, EBKN, EKBE, EKET, EKKN
- Materials (M\*): MARA, MARC, MARD, MARM, MAKT, MSEG, MKPF, MVKE, MCHB, MSKA
- Vendor (LF\*): LFA1, LFB1, LFM1
- Equipment / Serial (EQ\*, OBJ\*, SER\*): EQUI, EQBS, EQUZ, OBJK, SER01-SER09, IFLOT, ILOA
- Inventory / Whse: LQUA, LTAP, RKPF, RESB
- Accounting: BKPF, BSEG, RBKP, RSEG (NOT ACDOCA — seed is ECC topology, see SAP version investigation)
- Org / Master (T\*): T001, T001W, T001L, T024, T024E, T023, T156

**Seed shape:** Create new seed `dbt/seeds/sap_table_catalog.csv` at per-table grain. Existing `dbt/seeds/sap_data_dictionary.csv` (per-field, analyst-curated business meanings + translations + examples) stays untouched.

Schema for `sap_table_catalog.csv`:
- `table_name` (PK)
- `module` — procurement / materials / vendor / equipment_serial / inventory_warehouse / accounting / org_master
- `table_category` — one of `{TRANSP, CLUSTER, POOL}` (data-bearing categories). The scraper filters at scrape time; `{VIEW, STRUCT, INTTAB, APPEND, GENERIC, ...}` are excluded as non-data-bearing.
- `source_release_stamp` — sapdatasheet.org's "Last changed by/on SAP YYYYMMDD" stamp (e.g., 2013-06-04 for EKKO)
- `brief_description` — table purpose, ~100 chars (e.g., "Purchasing Document Header")
- `key_fields` — comma-separated string of top 15-20 canonical fields (e.g., "MANDT, EBELN, BUKRS")
- `brief_field_descriptions` — JSON-encoded string of `{field_name: short_description}` for the top 15-20 fields
- `scrape_source` — always `'sapdatasheet.org'` for v1; future extension point if `leanx.eu` fallback added
- `scrape_date` — date of the scrape run

The grain mismatch with `sap_data_dictionary.csv` was an implicit assumption in the design pass that surfaced at Phase 1 Step 0. Per-table catalog metadata doesn't compose with per-field analyst curation; clean schema separation is the right call.

**Scrape mechanics:**
- One-time idempotent script: `scripts/scrape_sap_catalog.py`.
- Iterates over a hardcoded target list of Variant C tables.
- Per-table fetch + HTML parse + CSV row write. Polite throttling (1-2 req/sec, User-Agent header).
- Re-running updates existing rows in place rather than duplicating. Manual re-run when needed; not a runtime dependency.

**LOC estimate:** 100-150.

**Q1 finding — top-N sampling bias.** sapdatasheet.org's page-rendered "top fields" listing is structural/alphabetical, **not** ranked by business importance. Two divergence flags in Q1 (LFA1, EQUI) traced to the catalog's top-20 happening to skip the seed's most-common business fields (LFA1: NAME1/ORT01/STRAS in seed; BAHNS/BBBNR/BUBKZ in catalog top-20 — all real LFA1 fields, just different selection). Phase 1 proceeds with top-N (15-20 fields) per Q1+Q2 calibration; threshold 0.30 stays GREEN. Phase 2 replaces with full-schema scrape (every page exposes the full field count, e.g., MARA "239+ components"; cost remains under $0.05/BAR even at 5× per-row size). See Phase 2 backlog.

> **~~OPEN QUESTION 1~~ RESOLVED by Q1.** Token cost of including catalog rows: Variant A=$0.0046, Variant B=$0.0065 first / $0.0027 cached, Variant C=$0.0105 first / $0.0031 cached — all 5-10× under the $0.05/BAR design ceiling. **Cost is not the binding constraint.** Per-row mean is 77 tokens (vs 200 the design assumed). See `tasks/c5_q1_token_cost.md`.

---

## Component 2 — C5 prompt design

**Pattern:** B (LLM consults catalog as grounding, not free recall). The viability experiment showed free recall produces real-namespace tables but mis-described purposes — Pattern B grounds the descriptions empirically.

**Prompt structure (proposed):**

```
You are a SAP data architect. The analytics system has determined a business term is
unanswerable from currently-ingested SAP tables. Recommend tables to extend the scope.

[TERM CONTEXT]
- term name + definition + grain
- term conditions (filters, exclusions)
- confirmed scope tables (currently ingested)
- iteration trace summary: what SQL was tried, why it produced an empty/wrong result,
  what the LLM's self-reflection said about the gap

[CATALOG] — ground truth, only recommend from here
For each candidate table (module-filtered to procurement/equipment/materials/vendor/...
based on confirmed scope):
- table_name | brief_description | source_release_stamp | key_fields |
  brief_field_descriptions

[CONSTRAINTS]
- Recommend ONLY tables present in [CATALOG] above.
- Each recommendation must cite the catalog row's table_name as validation_source.
- Tier recommendations:
  * primary (1-2): the table you are most confident closes the specific gap, with
    join keys you can name from the catalog row's key_fields.
  * hypothesis (1-3): plausible alternates that would help if primary doesn't fit.
  * customer_namespace (0-1): if a Z* / customer-custom table is likely needed,
    flag it explicitly as low-confidence requires-customer-investigation.
- Do NOT recommend tables already in confirmed scope (those have been tried).
- Do NOT recommend tables not in [CATALOG] — if you think a table is needed but it's
  absent, say so in a separate "catalog_gaps" field rather than recommending.

[OUTPUT JSON]
{
  "recommendations": [
    {
      "table_name": "...",            // must match a [CATALOG] table_name
      "tier": "primary|hypothesis|customer_namespace",
      "join_keys": ["..."],           // must come from catalog key_fields
      "rationale": "...",             // what gap this closes
      "validation_source": "...",     // catalog row's table_name (= self-reference)
      "confidence_grade": "high|medium|low"
    }
  ],
  "catalog_gaps": [
    "table_X is likely needed but I don't see it in the catalog"
  ]
}
```

**Why this addresses the viability-experiment failures:**
- SER02 mis-description: in Pattern B, the catalog's `brief_description` for SER02 reads "Doc Header for Serial Numbers in Physical Inventory Documents." If the LLM still recommends SER02 for status-history use, the catalog text is in front of it — it must either describe SER02 correctly or stop recommending it.
- ITOB hallucination: ITOB never enters the catalog (it's a structure, not a table; the scrape's table-list source filters it out). LLM cannot recommend it without violating the constraint.
- Confidence calibration: tier separation forces the LLM to commit "primary" to only its strongest recommendation, with rationale tied to a specific gap.

> **OPEN QUESTION 2: catalog scoping in the prompt — module-adjacent vs all.**
> For BG027's equipment/serial domain, do we surface only EQ\*/OBJ\*/SER\*/IL\* tables (~20 rows) or also adjacent modules (M\*, E\* — another ~30 rows)? Tighter scope = lower cost, but risk of missing a cross-module bridge (e.g., Sales Document → Equipment links). Q1 experiment can measure both.

---

## Component 3 — Runner integration

**Trigger detection.** Two equivalent surfaces:
- The runner already computes `convergence_reason = hard_stop_scope_mismatch` when `scope_sanity_answer == "no"` on two consecutive iterations. C5 fires *before* the BAR row is finalized with that reason.
- Alternative: fire on iteration 2's gates_result if `scope_sanity_answer == "no"` AND iteration 1 also had `scope_sanity_answer == "no"`. This is observation-equivalent.

**Sequence:**
1. Iteration loop completes with consecutive `scope_sanity=no`.
2. **Before** writing the BAR row with `convergence_reason=hard_stop_scope_mismatch`, fire the C5 prompt.
3. C5 prompt receives: term + scope + iteration trace summary (last iteration's SQL + reflection prose + scope_sanity rationale).
4. LLM emits structured JSON.
5. Validation layer (Component 4) processes the recommendations.
6. BAR row writes with:
   - `status = needs_data_extension` (new status, distinct from existing `hard_stop` / `failed` / `converged_*`)
   - `convergence_reason = hard_stop_scope_mismatch` (preserved for audit trail)
   - `sourcing_recommendations` = JSON-serialized validated recommendation list (new field on BAR table)

**LLM-call discipline:**
- Per-BAR budget: existing $1.00/term ceiling. C5's projected ~$0.05 fits comfortably within remaining budget after iteration loop typically uses $0.30-0.50.
- If existing budget is exhausted at the trigger point: skip C5 (don't blow the ceiling). BAR finalizes with the original `hard_stop_scope_mismatch` reason. Mark `c5_skipped=budget_exhausted` in the BAR row's audit field for observability.
- Single C5 call per BAR. Not iterative. The recommendation either lands or doesn't; refinement is for the analyst.

**Schema changes:**
- `business_term_analysis_results` table: add columns `sourcing_recommendations VARCHAR` (JSON), `c5_input_tokens INT`, `c5_output_tokens INT`, `c5_cost_usd DOUBLE`, `c5_skipped_reason VARCHAR`.
- New status enum value: `needs_data_extension`. Update `schema.yml` `accepted_values` accordingly.

**LOC estimate:** 100-150 (trigger detection + prompt assembly + LLM call + validation invocation + BAR row update).

### Trigger edge cases not covered by Phase 1-3

The Phase 1-3 trigger fires only on the canonical case: two consecutive iterations where the LLM emits `scope_sanity_answer == "no"`. Two adjacent edge cases are explicitly **not addressed** by this design and are deferred to Phase 4+:

- **Single-iteration `scope_sanity=no` + budget exhaustion before iter 2.** If the iteration loop terminates after iter 1 due to budget projection (existing Piece 8 behavior) and iter 1 declared `scope_sanity=no`, the trigger condition (two consecutive) never materializes. C5 does not fire. The BAR row finalizes with its original convergence reason (typically `hard_stop_budget_exhaustion` or similar). **This is acceptable for Phase 1-3** — the analyst still sees the BAR row and the iter-1 reflection prose; they just don't get a structured sourcing recommendation. **Phase 4+ extension:** could fire C5 on a single-iteration `scope_sanity=no` if iter-1 confidence (e.g., shadow_rubric ≥ threshold) is sufficient to trust the gap diagnosis.

- **`scope_sanity=no` + other gates pass (compile + run + row_count_ok).** The runner currently treats this as `converged_soft` — the LLM's mechanical gates passed and convergence is declared, even though the LLM itself flagged a scope concern. C5 does not fire. **Open question for Phase 4+:** should C5 fire whenever `scope_sanity=no` is emitted regardless of other gates? Argument for: the LLM is signaling it doesn't trust its own answer; sourcing recommendations would help the analyst decide whether to extend scope. Argument against: this could fire C5 on every `converged_soft` BAR with a hesitant LLM, increasing cost and noise. Documented here for future tuning; not a Phase 1-3 commitment.

---

## Component 4 — Validation layer

**Purpose:** Filter LLM-emitted recommendations through two grounded checks before they reach the analyst.

**Layer A — catalog allowlist match.** Each recommended `table_name` is looked up in `sap_table_catalog.csv` (post-Component-1 scrape).
- Match → row marked `catalog_verified = True`. The catalog's `brief_description` is included as the authoritative description in the BAR's `sourcing_recommendations` field, *replacing* the LLM's free-text rationale where they disagree (LLM's rationale is preserved as a secondary field, but the catalog wins on the description label).
- No match → row marked `catalog_verified = False`. Surfaced separately as "LLM-suggested, not in our reference catalog — verify externally" or rejected outright (decision below).

**Layer B — empirical cross-check against `raw_sap` schema.** For each `table_name`, query `information_schema.tables WHERE table_schema='raw_sap'`.

| Catalog | raw_sap | Classification | Action |
|---|---|---|---|
| YES | NO | **(A) Recommend ingestion** — canonical C5 use case. Real SAP table, not yet ingested. | Surface to analyst with verified catalog description and join keys. |
| YES | YES, columns match catalog `key_fields` | **(B) Already in scope, weird recommendation** — LLM recommended what's already ingested. | Surface as "verify scope coverage" warning. Possible scope-derivation gap (Stage A may have missed it). |
| YES | YES, columns DIVERGE from catalog reference | **(C) Seed-vs-reference nomenclature divergence** — known SER0x case. Seed has the table name but with different semantics. | Surface as "real SAP convention vs your seed's nomenclature differs — see catalog for canonical structure." |
| NO | * | **(D) Not in catalog** — likely hallucinated (e.g., ITOB) or genuine catalog gap (rare). | Default: reject. Optional: surface in a separate "unverified suggestions" section with explicit "consult your SAP team" framing. |

**Nomenclature divergence detection.** Q1 surfaced **at least three** genuine "table name matches reference but semantics differ" cases in the seeded SAP data, not just the SER0x family originally identified:
- **SER01:** seed = `OBKNR, OBZAE, SDESSION_TYPE, EBELN, EBELP, MATNR, MENGE, MEINS` (PO-flavored). Reference = `MANDT, OBKNR, LIEF_NR, POSNR, KUNDE, ANZSN, VBTYP, BWART, VKORG, VTWEG, SPART, ...` (SD-delivery-flavored). Different documents.
- **EQBS:** seed = `EQUNR, BEGDT, USTXT, STAT_DESC` (equipment-status fields). Reference = `MANDT, EQUNR, LBBSA, B_WERK, B_LAGER, B_CHARGE, SOBKZ, KUNNR, LIFNR, KDAUF, KDPOS, PS_PSP_PNR` (Serial Number Stock Segment). Different table semantics.
- **SERI:** seed = `OBKNR, MBLNR, ZEILE, ACCESSION_DATE, SERNR, MATNR, EQUNR` (serial+goods-doc-bridge hybrid). Reference SERI = `MANDT, MATNR, SERNR, ERNAM, ERDAT, AENAM, AEDAT, EQUNR, STATUS, WERK, LAGER, CHARGE, KUNDE, SDBELN, WABELN, BLART` (serial-number master with status/timestamps/customer/delivery). The seed's SERI mixes master-table fields with doc-index fields not present in either canonical SERI or the SER0x family.

Detection rule: **jaccard similarity of seed columns vs catalog `key_fields` < 0.30** (Q2-calibrated, replacing the original 0.4 placeholder). Q1 confirmed the validation logic catches all three cases plus the thin-slice cases (SER03, EQUI, MARC, LFA1) — sanity flag GREEN, 0% true false positives.

**Recommendation-grade synthesis.**
The LLM's `confidence_grade` is treated as input, not output. The validation layer emits its own `recommendation_grade`:
- `verified` — Catalog match + raw_sap absent (case A) + LLM confidence ≥ medium.
- `verified_low_priority` — Case A + LLM confidence = low.
- `divergence_warning` — Case C, requires analyst review of seed vs reference.
- `unverified` — Case D, surfaced separately or rejected based on configuration flag.

**LOC estimate:** 80-120 (table lookups + classification logic + divergence detection + grade synthesis).

> **OPEN QUESTION 3: real-SAP-not-yet-ingested vs hallucinated discrimination.**
> Layer A's catalog match is the discriminator: catalog YES = real SAP, NO = hallucinated (or catalog gap). This presumes the catalog has near-complete coverage of real SAP tables in the relevant modules. **Q2 experiment below tests this against the viability experiment's actual recommendations** (SER01 ✓, SER02 ✓-with-divergence-warning, SER03 ✓-with-divergence, EQUZ ✓, ITOB ✗).

---

## Component 5 — UI surface

**Streamlit page.** The Business Glossary page already displays BAR rows for each term. Extend it to detect `status = needs_data_extension` and render a dedicated section.

**Layout (per affected BAR):**

```
[BAR-NNNNN — needs_data_extension — (date)]
The term '<term>' cannot currently be answered from the confirmed scope
(<scope_tables>). Iteration trace identified the gap as: <last iteration's
scope_sanity rationale>.

To answer this term, the system recommends ingesting the following tables:

[ Primary (1-2 recommendations, "verified" grade) ]
- TABLENAME — <catalog brief_description>
  Join keys: <join_keys>
  Rationale: <LLM rationale, post-validation>
  [ Source: catalog (sapdatasheet.org, release: <release_stamp>) ]
  [ "Mark as ingestion-planned" ] [ "Discuss with team" ]

[ Hypothesis (0-3, "verified_low_priority" or "verified" + low confidence) ]
- ...

[ Divergence warnings (if any "C" classifications) ]
- TABLENAME exists in your data but with different columns than standard SAP.
  Standard reference: <catalog key_fields>. Your seed: <observed columns>.
  Verify whether your seed's TABLENAME serves the same purpose.

[ Unverified suggestions (LLM-recommended, not in catalog — only shown if
  validation layer is configured to surface them) ]
- ...
```

**Action affordances:**
- "Mark as ingestion-planned" — appends a row to a new `ingestion_recommendations` seed (or updates the BAR's `analyst_review_reason`). Nothing automated; this is a tracking marker.
- "Discuss with team" — placeholder; could link to ticket-system later.

**LOC estimate:** 50-100 (Streamlit rendering + simple action handlers).

---

## Phasing and total estimate

| Phase | Scope | LOC |
|---|---|---|
| Phase 1 | Catalog scraper + seed extension | ~100-150 |
| Phase 2 | C5 prompt + runner integration + validation layer | ~250-350 |
| Phase 3 | UI display + action affordances | ~50-100 |
| **Total** | | **~400-600 LOC across 3-4 focused sessions** |

**Pre-Phase-1 gate:** Q1 + Q2 experiments. Both are read-only / no-LLM-cost or single-LLM-call-cost. Run them, write findings to `tasks/c5_q1_token_cost.md` and `tasks/c5_q2_validation_accuracy.md`. If Q1 shows token cost is unmanageable or Q2 shows validation accuracy too low, revisit design before Phase 1.

---

## Open questions requiring experiments

### Q1 experiment — catalog-row token cost

**Question:** What's the prompt-context size when we include catalog rows for module-relevant tables in a C5 prompt?

**Method:**
1. Scrape ~30 representative tables across procurement + equipment + materials + vendor + accounting modules into a draft catalog format.
2. Format each row as the C5 prompt would include it (table_name, brief_description, key_fields, brief_field_descriptions JSON).
3. Assemble a sample C5 prompt (BG027 context + catalog block + constraints).
4. Tokenize with the API's tokenizer or a tiktoken-equivalent estimate.
5. Compute: total input tokens, projected cost at sonnet-4 rates, vs the per-BAR budget.
6. **Secondary measurement — divergence-warning frequency per variant.** For each catalog table in the variant that ALSO exists in `raw_sap.*`, compute the jaccard similarity of seed columns vs catalog `key_fields` (using the threshold value Q2 will calibrate empirically; if Q2 hasn't run yet, use a placeholder threshold of 0.4). Count how many tables would trigger `divergence_warning` (Component 4 case C). The expected genuine-divergence count is ~2 (the SER0x family); anything substantially higher indicates the threshold is too aggressive or the catalog reference and the seed disagree on canonical column names for non-SER0x reasons.

**Scope variants to measure:**
- Variant A: only equipment/serial module (~10 tables) — minimal, BG027-relevant only.
- Variant B: equipment + materials modules (~20 tables) — module-adjacent.
- Variant C: all 5 modules above (~50 tables) — broad.

**Cost:** $0 (read-only scrape + tokenization, no LLM call).

**Q1 Result (resolved):** see `tasks/c5_q1_token_cost.md`. Headline numbers:
- Variant A (10 tables): 1,541 tokens, $0.0046 first call.
- Variant B (20 tables): 2,153 tokens, $0.0065 first call / **$0.0027 cached**.
- Variant C (41 tables): 3,495 tokens, $0.0105 first call / **$0.0031 cached**.
- Per-row mean = 77 tokens (vs 200 the design assumed).
- Sanity flag: **GREEN.** 0% true false positives across all variants.
- Recommendation: **adopt Variant C** for Phase 1. Cost is negligible at C's level; broader scope future-proofs across modules; counter-intuitively C also has the *lowest* divergence rate (47%) because more non-divergent control tables are added.

---

### Q2 experiment — validation cross-check accuracy

**Question:** Does the empirical cross-check correctly classify the C5 viability experiment's actual recommendations?

**Method:** Take the 5 specific tables the viability experiment surfaced (SER01, SER02, SER03, EQUZ, ITOB) and run each through the proposed validation logic (assuming Component 1's catalog has been scraped to a draft state). Expected classifications:

| Table | Expected catalog | Expected raw_sap | Expected classification | Notes |
|---|---|---|---|---|
| SER01 | YES (catalog has reference SER01 = SD-delivery) | YES (seed has SER01, but PO-flavored) | **(C) divergence_warning** | This is the real SER0x case — seed and reference disagree on what SER01 is. |
| SER02 | YES (catalog has SER02 = phys-inventory-doc-index) | NO (not in seed) | **(A) recommend ingestion** | LLM described it as "Status History" — catalog correction surfaces here. |
| SER03 | YES (catalog has SER03 = production-order serial-doc-index) | YES (seed has SER03, inventory-doc-flavored) | **(C) divergence_warning** | Seed's SER03 ≈ reference SER01 semantically. |
| EQUZ | YES (catalog has EQUZ = equipment time segment) | NO (not in seed) | **(A) recommend ingestion** | Clean canonical case. |
| ITOB | NO (catalog excludes structures; ITOB is a DDIC type, not a transparent table) | NO | **(D) reject** | Hallucination correctly filtered. |

**Phase 1 implementation note (filter correction).** Q2 derived a "Table Category = TRANSP only" rule from the single ITOB-as-VIEW counter-example. Phase 1 implementation surfaced that this rule was too narrow: BSEG is a `CLUSTER` table (rows stored in the RFBLG cluster) but is real, queryable, and data-bearing. The corrected rule is **"Table Category in `{TRANSP, CLUSTER, POOL}`"** — the data-bearing categories. `{VIEW, STRUCT, INTTAB, APPEND, GENERIC, ...}` remain excluded. The scraper applies this corrected rule.

**Threshold calibration (added to Q2 method):** The classification logic depends on a jaccard-similarity threshold for distinguishing case (B) "already in scope, columns match" from case (C) "already in scope, columns diverge." The design's placeholder is 0.4. Q2 calibrates this empirically by extending the test set:

1. For each of the 5 viability-experiment tables, compute jaccard(seed_columns, catalog_key_fields) where both sides exist (i.e., excluding ITOB which is catalog-absent and the non-seeded tables SER02/EQUZ which are raw_sap-absent — these reduce to the 2 SER0x tables that have both sides).
2. Add **2 control tables** where divergence is NOT expected: **MARA** and **EKKO**. Both are present in the seed and in the catalog reference; both should have high jaccard. Compute their jaccards too.
3. Recommended threshold = the value that cleanly separates the divergent SER0x jaccards (expected low) from the non-divergent MARA/EKKO jaccards (expected high). E.g., if SER0x scores ~0.2 and MARA/EKKO score ~0.7, recommend threshold = 0.45.
4. If the jaccards don't cleanly separate (e.g., SER0x at 0.3 and MARA at 0.4), surface this as a signal that column-set jaccard is too coarse a divergence detector — Phase 2 may need to refine to a column-name + datatype check or a hand-curated overrides table.

**Cost:** $0 (pure logic + seed/catalog lookups, no LLM calls).

**Output:** `tasks/c5_q2_validation_accuracy.md` with:
- Per-table actual classification vs expected.
- Edge cases discovered (e.g., a real SAP table that the catalog scrape missed → false-negative reject).
- **Empirically-calibrated jaccard threshold value** (replacing the design's 0.4 placeholder; Component 4 and Q1's secondary measurement both consume this value).
- Validation-logic adjustments needed before Phase 2.

**Decision rule pre-experiment:** if all 5 classifications match expectations AND the calibrated threshold cleanly separates divergent from non-divergent controls, validation logic is design-ready. If any mismatch, surface it and adjust the layer rules.

---

## Risks and mitigations

**R1 — sapdatasheet.org anti-bot evolution.** It works today (verified this Stage), but could block in the future. **Mitigation:** the catalog is a **cached seed**, not a runtime dependency. If sapdatasheet.org goes dark later, the existing seed continues to serve C5; manual re-scrape is only needed when adding new tables. `leanx.eu` is a documented alternative.

**R2 — catalog vintage vs SAP releases.** Reference content on sapdatasheet.org is stamped 2011-2013 (ECC 6.0 era). New SAP fields may be missing from the reference and from the seed. **Mitigation:** target ECC explicitly (per SAP version investigation Q4 — seed is ECC topology). `source_release_stamp` column makes vintage transparent. If user later upgrades to S/4HANA, re-scrape against modern reference (different URL pattern likely needed).

**R3 — SER0x nomenclature divergence false-positives.** Seed's SER01/SER03 have shuffled meanings vs reference. The validation layer's classification (C) flags these as divergence warnings — but if the LLM correctly recommends one of the seed-flavored tables for the seed-context use case, the divergence flag could be misleading noise. **Mitigation:** maintain an explicit `seed_vs_reference_overrides` table for known intentional divergences; document in the catalog row's `notes`. For the BG027 case specifically: seed's SER03 ≈ reference's SER01, and the C5 recommendation should ideally point to seed's SER03 with a note.

**R4 — LLM confidence-label persistence despite catalog grounding.** Even with Pattern B grounding, the LLM may still over-confidently rank hypotheses. **Mitigation:** validation layer overrides LLM confidence with its own `recommendation_grade` (catalog-verified + raw_sap-absent = `verified`; mismatched = `unverified`). The LLM's confidence is preserved as a secondary signal but not the primary grade displayed to the analyst.

**R5 — cost amplification from catalog grounding.** Including 30-50 catalog rows per C5 invocation could push per-call cost from $0.013 (free recall, viability experiment) to $0.05-0.20. Multiplied across multiple unanswerable terms in a demo session, this is real spend. **Mitigation:** Q1 experiment quantifies it. Use prompt caching for the catalog block (large stable context — ideal cache target). Cap catalog scope to module-relevant tables per OQ2.

**R6 — false sense of completeness.** UI shows "ingest these 5 tables" — but the seed coverage gap (BSEG 12 cols of 287 real, EQUI 11 of 83, MSEG 15 of 197) means even the "right" recommended table needs **field-level** ingestion completeness, which C5 doesn't address. **Mitigation:** UI surfaces a coverage warning when an existing scope-table has thin column coverage relative to the catalog reference. Out of Phase 1-3 scope; document as a known gap.

---

## Phase 2 backlog (refinements deferred from Phase 1)

Captured from Q1 + Q2 findings. None block Phase 1; all are concrete refinements with specific evidence.

- **Case-C sub-classification: C1 thin-slice vs C2 genuine wrong-table.** Q1 showed 4 of 7 divergence flags are thin-slice cases (right table, slim or differently-displayed columns) and only 3 are genuinely-wrong-table cases. Without sub-classification, Phase 1's analyst-facing UI lumps these into one "divergence_warning" tier — drowning the genuinely-confusing SER01/EQBS/SERI cases in routine SER03/EQUI/MARC/LFA1 noise. Proposed C1/C2 discriminator: catalog primary-key fields' presence in seed columns. PK match → C1 (right table, refine ingestion); PK miss → C2 (different table, investigate).
- **Full-schema catalog (replaces top-N).** Q1's top-N sampling bias finding: sapdatasheet.org's "top fields" listing is structural/alphabetical, not importance-ranked. Phase 1 ships with top-N (15-20 fields) because Q1+Q2 calibration is anchored to that size and stays GREEN. Phase 2 replaces with full-schema scrape (every page exposes total field counts; estimated 5× per-row size; Variant C still under $0.05/BAR uncached, well under cached). Eliminates the LFA1/EQUI flagging-on-display-bias artifacts.
- **Field-synonym normalization** for known SAP variants (e.g., `ERDAT` ↔ `ERSDA` on MARA; ~20-30 known synonyms cover common cases). Hand-curated CSV + ~10 LOC normalization in the validation layer. Removes ~0.04 jaccard-drag on controls (MARA scored 0.438 instead of ~0.48 due to this single mismatch).

## Decision log

- **Phase-1 Step-0 grain correction (this commit).** Component 1's catalog output lands in a new seed `sap_table_catalog.csv` at per-table grain rather than extending `sap_data_dictionary.csv` (per-field). Surfaced when Step 0 read the existing schema; denormalizing per-table catalog metadata across per-field rows would have collided with existing analyst-curated `description_en` / `description_hr` / `business_meaning` columns. Captured in commit message of Phase 1's implementation.
- **Phase-1 implementation Q2-filter correction (this commit).** TRANSP-only filter (derived from single ITOB-as-VIEW counter-example) was too narrow. Broadened to `{TRANSP, CLUSTER, POOL}` when BSEG was incorrectly skipped during initial scrape. Captured here for audit; the structural fix is in Component 1's filter spec, not a Phase-2 backlog item.
- **Decision #73 (audit-discipline thesis, Phase 14/15).** Per-source consumption directives and BAR-level attestation form the foundation C5 builds on. C5 is the *outbound* counterpart: the system not only verifies what it consumed but recommends what it didn't have.
- **Decision #83 (attestation-preservation runner-bug fix, this Stage).** The fix that turned BG027's `hard_stop_finalization_attestation_failure` into the legitimate `hard_stop_scope_mismatch` signal — the trigger condition C5 will hook into.
- **Decision #84 (C5 viability experiment, this Stage).** Documents the partial viability finding and the validation-layer requirement that this design centers on. C5 implementation will produce **decisions #85+** as Phase 1 (catalog scraping) and Phase 2 (runner integration) progress, including:
  - **D-#85 (anticipated):** catalog-scope decision (Variant A / B / C from Q1).
  - **D-#86 (anticipated):** validation-grade-vs-LLM-confidence reconciliation rule (post-Q2 results).
  - **D-#87 (anticipated):** per-BAR `c5_skipped_reason` taxonomy (budget_exhausted, no_recommendations_passed_validation, catalog_unavailable).

---

## Pre-implementation checklist

- [x] Q1 experiment complete (token-cost measurement) → `tasks/c5_q1_token_cost.md` + scope-variant decision (Variant C).
- [x] Q2 experiment complete (validation-logic accuracy) → `tasks/c5_q2_validation_accuracy.md` + threshold = 0.30, TRANSP filter rule.
- [ ] Design doc reviewed by user, OPEN QUESTIONs resolved or accepted as-is.
- [ ] Phase 1 spec drafted (catalog scraper + seed extension) — ready for implementation session.
