-- Composite uniqueness on (term_name) filtered to non-archived rows.
-- A new active (draft OR approved) term may reuse a term_name whose prior
-- instances are archived — but only one non-archived row per name at any time.

SELECT term_name, COUNT(*) AS n_active_rows
FROM {{ ref('business_glossary') }}
WHERE status != 'archived'
GROUP BY term_name
HAVING COUNT(*) > 1
