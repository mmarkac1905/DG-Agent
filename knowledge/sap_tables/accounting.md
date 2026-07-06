# SAP Tables: accounting

_Last generated: 2026-07-06 19:11:41_

Keywords: `bkpf, bseg, accounting, posting, knjiženje, fi document`

## Related Decisions (2)

- **#112** (2026-06-25) — path3_sd_fi_infra_build: SD + FI are now first-class in the warehouse. Margin term cost side v1 = revenue - procurement cost; returns (MSEG 161/122 are DMBTR=0, unvalued) and warranty (ZHT_WARRANTY_LOG catalog-only, no data) deferred. Reference: scripts/generate_sd_billing.py, scripts/generate_fi_shadows.py, dbt/models/vault/link_sales_order_equipment.sql.
- **#114** (2026-06-25) **[NEVER_REPEAT]** — fi_link_join_key_must_be_single_table_per_side: A join key expression must depend on only one table per side or DuckDB falls back to nested-loop. For AWKEY-style concatenated keys, derive each side independently (the other two FI links join on r.BELNR\|\|r.GJAHR / m.MBLNR\|\|m.MJAHR and built in <0.5s). Diagnose slow queries by reading the plan BEFORE assuming locks/contention. Do not leave heavy read-only DuckDB queries running - one stray handle blocks every dbt write.

## Related Domain Relationships (0)

_(none)_

## Open Issues (0)

_(none)_

## DO NOT (Anti-patterns)

- **#114** (2026-06-25) **[NEVER_REPEAT]** — fi_link_join_key_must_be_single_table_per_side: A join key expression must depend on only one table per side or DuckDB falls back to nested-loop. For AWKEY-style concatenated keys, derive each side independently (the other two FI links join on r.BELNR\|\|r.GJAHR / m.MBLNR\|\|m.MJAHR and built in <0.5s). Diagnose slow queries by reading the plan BEFORE assuming locks/contention. Do not leave heavy read-only DuckDB queries running - one stray handle blocks every dbt write.
