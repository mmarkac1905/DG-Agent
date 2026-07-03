# Business Term: Vendor Performance Grade

_Last generated: 2026-07-03 21:52:59_

## Definition

Composite letter grade (A/B/C/D) based on on-time delivery rate. A: OTD >= 80, B: 60-79, C: 40-59, D: < 40

- **ID:** `BG016`
- **Owner:** Analytics Team
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** grade
- **Grain:** vendor
- **Domain:** procurement
- **Related terms:** [on_time_delivery_rate](on_time_delivery_rate.md) · [vendor_fulfillment_rate](vendor_fulfillment_rate.md)

**Notes:** Computed in knowledge_vendor_performance. Grades trigger procurement review if D for two consecutive quarters.

## Source-to-Target Mapping

### Source Tables (SAP)

| Table | Field | Description |
| --- | --- | --- |
| EKBE | BUDAT | GR posting date |

### Transformation (plain language)

1. The vendor performance grade assigns letter grades based on the vendor's on-time delivery rate, where vendors with 80% or higher on-time delivery receive an 'A', 60-79% receive a 'B', 40-59% receive a 'C', and below 40% receive a 'D'.
   - *Join:* Aggregated from fact_purchase_orders.is_on_time
   - *Filter:* All POs

### SQL (from dbt models)

**knowledge_vendor_performance.performance_grade:**
```sql
CASE
        WHEN AVG(CASE WHEN f.is_on_time THEN 1.0 ELSE 0.0 END) >= 0.80 THEN 'A'
        WHEN AVG(CASE WHEN f.is_on_time THEN 1.0 ELSE 0.0 END) >= 0.60 THEN 'B'
        WHEN AVG(CASE WHEN f.is_on_time THEN 1.0 ELSE 0.0 END) >= 0.40 THEN 'C'
        ELSE 'D'
    END
```

### Target Models

- `knowledge_vendor_performance`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-14)
