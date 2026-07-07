# SAP Tables: purchase_requisitions

_Last generated: 2026-07-07 10:48:23_

Keywords: `eban, ebkn, requisition, zahtjev`

## Related Decisions (1)

- **#105** (2026-05-05) — phase_beta_slim_vendor_material_z_tables_to_genuinely_ht_fields_only: This closes the SAP-native-default cleanup cycle the user requested with 'why dont we keep with true SAP native fields mostly'. Z-tables now carry ONLY fields with no SAP home: vendor (equipment_types - HT CPE taxonomy, quality_rating - HT internal scoring, contract_status - HT lifecycle), material (lifecycle_months - HT warranty/TCO horizon, primary_vendor_id - HT preferred supplier; SAP EORD can list many sources without designating a primary). Lesson generalized in feedback_default_sap_native memory: default assumption is real SAP carries the data; invent only for genuinely HT-domain content. Remaining seeds (org_structure, abap_logic_catalog, z_tables_catalog) still need their own architectural decisions - out of scope for this pass.

## Related Domain Relationships (0)

_(none)_

## Open Issues (0)

_(none)_
