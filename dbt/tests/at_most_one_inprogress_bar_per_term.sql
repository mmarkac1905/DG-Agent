-- Piece 8 §9 singular test.
-- Invariant: at most one status='in_progress' BAR row per business_term_id.
-- Enforces §3e concurrency at the data layer. Runner's step 0a rejection
-- is the primary enforcement; this test is the post-hoc data-integrity
-- backstop in case a race slips through.

SELECT business_term_id, COUNT(*) AS inprogress_count
FROM {{ ref('business_term_analysis_results') }}
WHERE status = 'in_progress'
GROUP BY business_term_id
HAVING COUNT(*) > 1
