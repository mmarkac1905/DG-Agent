-- Term-analysis (BAR) runner singular test.
-- Invariant: no status='in_progress' BAR rows older than 24 hours.
-- dbt-test analog of the runner's step 0b TTL sweep — catches stale
-- in_progress rows when the runner hasn't executed within a day.
-- Non-zero result fails the test, surfacing the stale row in `dbt test`
-- output even when no analyst is running the BAR runner.

SELECT id, business_term_id, inprogress_since_utc
FROM {{ ref('business_term_analysis_results') }}
WHERE status = 'in_progress'
  AND inprogress_since_utc < NOW() - INTERVAL '24 hours'
