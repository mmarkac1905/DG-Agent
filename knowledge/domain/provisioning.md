# Domain: provisioning

_Last generated: 2026-07-07 11:22:47_

Keywords: `provisioning, service activation, network activation, dslam, olt, port assignment, ip assignment`

## Related Decisions (5)

- **#3** (2026-04-14) — abap_logic_catalog_created: ABAP documentation layer complete — covers serial validation equipment lifecycle provisioning bridge vendor scoring warranty tracking and financial depreciation. In real engagement this would be auto-populated by Claude scanning exported ABAP source.
- **#15** (2026-04-14) — full_glossary_alignment: Self-documenting data product complete. Any business user can hover any KPI and see definition + source tables + transformation in one tooltip. Honest sample data caveat in sidebar.
- **#42** (2026-04-17) **[NEVER_REPEAT]** — bt_warn_but_dont_block: BT warns never blocks. Write-path actions block on red. The four-site split is deliberate and documented.
- **#64** (2026-04-18) **[NEVER_REPEAT]** — archive_filter_propagated_to_all_selectors: Every business_term selector AND every LLM prompt that includes glossary rows must filter WHERE status != "archived". Archived stays in table for audit but is invisible to operational UI and AI. Lookup-by-ID call sites (e.g. tooltip from metric_card) are exempt because they legitimately render data for any id passed in. See also decision #47 learning_signal filter and #58 empty_csv_corrupts_duckdb_schema_inference.
- **#66** (2026-04-18) **[NEVER_REPEAT]** — lookup_scope_matches_selector_scope_projectwide: Selector-lookup scope must match selector-list scope. Any post-selection resolution that feeds UI content must scope to the same filter as the list of options — mismatched scope lets non-listed rows win the lookup. Centralise the resolution in a helper (_resolve_active_term) so future selectors get correct behavior by default. LLM-prompt context is a separate scope question: archive usually wants exclusion (same filter as selector) unless the feature is explicitly about audit. RULE 42 revision emphasises project-wide applicability — audit every selector-lookup pair on any significant refactor.

## Related Domain Relationships (0)

_(none)_

## Open Issues (0)

_(none)_

## DO NOT (Anti-patterns)

- **#42** (2026-04-17) **[NEVER_REPEAT]** — bt_warn_but_dont_block: BT warns never blocks. Write-path actions block on red. The four-site split is deliberate and documented.
- **#64** (2026-04-18) **[NEVER_REPEAT]** — archive_filter_propagated_to_all_selectors: Every business_term selector AND every LLM prompt that includes glossary rows must filter WHERE status != "archived". Archived stays in table for audit but is invisible to operational UI and AI. Lookup-by-ID call sites (e.g. tooltip from metric_card) are exempt because they legitimately render data for any id passed in. See also decision #47 learning_signal filter and #58 empty_csv_corrupts_duckdb_schema_inference.
- **#66** (2026-04-18) **[NEVER_REPEAT]** — lookup_scope_matches_selector_scope_projectwide: Selector-lookup scope must match selector-list scope. Any post-selection resolution that feeds UI content must scope to the same filter as the list of options — mismatched scope lets non-listed rows win the lookup. Centralise the resolution in a helper (_resolve_active_term) so future selectors get correct behavior by default. LLM-prompt context is a separate scope question: archive usually wants exclusion (same filter as selector) unless the feature is explicitly about audit. RULE 42 revision emphasises project-wide applicability — audit every selector-lookup pair on any significant refactor.
