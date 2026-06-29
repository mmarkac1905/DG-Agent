Architectural diagnostic — BG029 stopped at DM (mart) layer
without OBT layer generation, but all existing dashboards
read from OBT layer. Need to understand why.

Pure investigation. Read-only. $0.

(1) Identify the layer architecture

dbt/dbt_project.yml + dbt/models/ directory structure:
- What layers exist? (raw → staging → vault → mart →
  obt? Or different shape?)
- Are layers required or optional in the standard
  pipeline?

(2) Find the S2T generation logic

scripts/ or app/claude_api.py — find where Stage E S2T
specification gets generated. Identify:
- What determines which layers a term's S2T mapping
  produces?
- Is OBT layer always generated, sometimes generated,
  or never generated automatically?
- For BG029 specifically: trace why fact_goods_receipts_monthly
  was generated but no OBT companion model.

(3) Examine existing approved terms' S2T mappings

Query s2t_mapping for approved terms (other than BG029):
SELECT business_term_id, target_layer, target_model,
       target_column
FROM main_seeds.s2t_mapping
WHERE business_term_id IN (
    SELECT id FROM main_seeds.business_glossary
    WHERE status = 'approved'
)
ORDER BY business_term_id, target_layer;

For each approved term, surface:
- How many distinct target_layer values?
- Does each approved term have an OBT layer entry?
- What's the typical layer chain? (staging → vault →
  mart → obt? Or different?)

This tells us whether OBT layer in s2t_mapping is the
canonical pattern or an exception.

(4) Compare BG029's s2t_mapping to that pattern

Query for BG029 specifically:
SELECT target_layer, target_model, target_column
FROM main_seeds.s2t_mapping
WHERE business_term_id = 'BG029'
ORDER BY target_layer;

What layers got generated? What layers are missing
compared to approved terms' patterns?

(5) Identify how dashboards consume metrics

grep streamlit dashboard code for which models it reads
from. Confirm dashboards read from OBT layer per user's
report.

If dashboards read OBT and BG029 has no OBT mapping:
that's a real consumption gap.

(6) Determine bug vs design

Three possibilities to classify:

(a) Design — system intentionally skips OBT for
    pre-aggregated metrics (mart fact is "good enough").
    BG029's 26-row fact is consumable directly. No bug;
    user's expectation differs from system's design.

(b) Bug — OBT generation should have run but didn't.
    Some condition in Stage E logic is incorrectly
    excluding BG029. Real fix needed.

(c) Workflow gap — OBT generation requires explicit
    trigger that user hasn't done yet. Manual step missing
    from current walk.

For each: identify evidence in the code/data.

=== Output ===

Surface findings + classification + recommended next
action. Don't fix anything.

Cost: $0 (read-only).