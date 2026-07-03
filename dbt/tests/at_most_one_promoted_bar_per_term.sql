-- Term-analysis (BAR) runner singular test.
-- Invariant: at most one status='promoted' BAR row per business_term_id.
-- Backstops the UI's promote-and-supersede atomic action.
-- Non-zero result fails the test.

SELECT business_term_id, COUNT(*) AS promoted_count
FROM {{ ref('business_term_analysis_results') }}
WHERE status = 'promoted'
GROUP BY business_term_id
HAVING COUNT(*) > 1
