{{ config(materialized='view') }}

-- KI-114 — Unified blocker-state view reconciling the three persistent
-- representations of a Stage A blocker:
--
--   A (filed):   business_glossary.scope_derivation_history_json
--                .iterations[*].llm_response.blockers[*]
--                — defines which blockers exist; carries no resolution flag.
--
--   B (claimed): domain_analysis_results.result_json.blockers_addressed[*]
--                — historical: Stage B analyzers were asked to claim
--                addressed blockers. KI-115 closed this representation:
--                the prompt directive was removed (see
--                _stage_a_blocker_loader.render_analyst_concerns_block);
--                field stays in result_json schema for backward
--                compatibility but is intentionally always-empty going
--                forward. This view does not consume it.
--
--   C (closed):  term_analysis_results.sufficiency_json.blockers_resolution[*]
--                — Stage C terminal verdict per blocker. Authoritative for
--                resolution status.
--
-- One row per (term_id, blocker_short_title) for terms in the eligible
-- statuses {scope_confirmed, domain_eda_pending, term_eda_pending,
-- ready_for_s2t}. Each row carries the original blocker JSON blob, a
-- handful of indexed fields, and a derived `current_status` enum:
--
--   PENDING     — no resolution row in latest TAR sufficiency, OR
--                 resolution_status in {could_not_resolve,
--                 escalated_to_analyst}.
--   RESOLVED    — resolution_status in {resolved, not_applicable}.
--   AMBIGUOUS   — resolution_status outside the documented enum (defensive).
--
-- Consumers (e.g. _stage_a_blocker_loader.load_blockers_for_table) filter
-- to current_status='PENDING' to surface only blockers that still need
-- analyst attention. Resolved blockers no longer pollute the Domain
-- Analysis per-table panels (the BG029 BWART symptom that motivated this
-- view).

WITH terms_eligible AS (
    SELECT
        id AS term_id,
        term_name,
        status AS term_status,
        scope_derivation_history_json AS hist
    FROM main_seeds.business_glossary
    WHERE status IN (
            'scope_confirmed', 'domain_eda_pending',
            'term_eda_pending', 'ready_for_s2t'
        )
        AND scope_derivation_history_json IS NOT NULL
        AND scope_derivation_history_json NOT IN ('', '{}')
),

iterations_indexed AS (
    SELECT
        t.term_id,
        t.term_name,
        t.term_status,
        gs.i AS iter_idx,
        json_extract(t.hist, '$.iterations[' || gs.i || ']') AS iter_json
    FROM terms_eligible t,
        generate_series(
            CAST(0 AS BIGINT),
            CAST(
                COALESCE(
                    json_array_length(json_extract(t.hist, '$.iterations')), 0
                ) - 1 AS BIGINT
            )
        ) AS gs (i)
),

confirmed_iter AS (
    SELECT *
    FROM iterations_indexed
    WHERE json_extract_string(iter_json, '$.analyst_action') = 'confirmed'
),

blockers_indexed AS (
    SELECT
        ci.term_id,
        ci.term_name,
        ci.term_status,
        ci.iter_idx,
        bgs.i AS blocker_idx,
        json_extract(
            ci.iter_json,
            '$.llm_response.blockers[' || bgs.i || ']'
        ) AS blocker_json
    FROM confirmed_iter ci,
        generate_series(
            CAST(0 AS BIGINT),
            CAST(
                COALESCE(
                    json_array_length(
                        json_extract(ci.iter_json, '$.llm_response.blockers')
                    ),
                    0
                ) - 1 AS BIGINT
            )
        ) AS bgs (i)
),

blockers_filed AS (
    SELECT
        term_id,
        term_name,
        term_status,
        iter_idx,
        blocker_idx,
        json_extract_string(blocker_json, '$.short_title')
            AS blocker_short_title,
        json_extract_string(blocker_json, '$.type') AS blocker_type,
        json_extract_string(blocker_json, '$.resolves_in') AS resolves_in,
        json_extract(blocker_json, '$.tables') AS tables_json,
        blocker_json
    FROM blockers_indexed
),

latest_sufficiency_per_term AS (
    SELECT
        term_id,
        sufficiency_json,
        ROW_NUMBER() OVER (
            PARTITION BY term_id ORDER BY executed_at_utc DESC
        ) AS rn
    FROM main_seeds.term_analysis_results
    WHERE row_type = 'sufficiency' AND status = 'success'
),

suff_latest AS (
    SELECT term_id, sufficiency_json
    FROM latest_sufficiency_per_term
    WHERE rn = 1
),

resolutions_indexed AS (
    SELECT
        sl.term_id,
        rgs.i AS res_idx,
        json_extract(
            sl.sufficiency_json,
            '$.blockers_resolution[' || rgs.i || ']'
        ) AS res_json
    FROM suff_latest sl,
        generate_series(
            CAST(0 AS BIGINT),
            CAST(
                COALESCE(
                    json_array_length(
                        json_extract(
                            sl.sufficiency_json, '$.blockers_resolution'
                        )
                    ),
                    0
                ) - 1 AS BIGINT
            )
        ) AS rgs (i)
),

resolutions AS (
    SELECT
        term_id,
        json_extract_string(res_json, '$.blocker_short_title')
            AS blocker_short_title,
        json_extract_string(res_json, '$.status') AS resolution_status,
        json_extract_string(res_json, '$.evidence') AS evidence
    FROM resolutions_indexed
)

SELECT
    bf.term_id,
    bf.term_name,
    bf.term_status,
    bf.iter_idx,
    bf.blocker_idx,
    bf.blocker_short_title,
    bf.blocker_type,
    bf.resolves_in,
    bf.tables_json,
    bf.blocker_json,
    r.resolution_status,
    r.evidence,
    CASE
        WHEN r.resolution_status IN ('resolved', 'not_applicable')
            THEN 'RESOLVED'
        WHEN r.resolution_status IN (
                'could_not_resolve', 'escalated_to_analyst'
            )
            THEN 'PENDING'
        WHEN r.resolution_status IS NULL THEN 'PENDING'
        ELSE 'AMBIGUOUS'
    END AS current_status
FROM blockers_filed bf
LEFT JOIN resolutions r
    ON
        r.term_id = bf.term_id
        AND r.blocker_short_title = bf.blocker_short_title
