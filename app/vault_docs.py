"""Shared vault column documentation used by Data Catalog and Data Model pages.

Exact-name match wins; prefix entries whose key ends in `_` match any column
that starts with that prefix (used for `hk_*` hash keys)."""

VAULT_COLUMN_DESCRIPTIONS = {
    # Standard DV columns (appear in every model)
    'hk_':             'Hash key (MD5) — primary/foreign key for vault joins',
    'hashdiff':        'Hash of all descriptive columns — detects changes for SCD2',
    'load_date':       'Timestamp when this record was first loaded into the vault',
    'record_source':   'Source system identifier (e.g., SAP_MM_EKKO)',

    # Hub business keys
    'purchase_order_number':    'SAP PO document number (EKKO.EBELN)',
    'vendor_id':                'SAP vendor account number (LFA1.LIFNR)',
    'material_number':          'SAP material/CPE type code (MARA.MATNR)',
    'equipment_number':         'SAP equipment record ID — one per physical CPE device (EQUI.EQUNR)',
    'plant_code':               'SAP plant/warehouse code (T001W.WERKS)',
    'material_document_number': 'SAP goods movement document number (MKPF.MBLNR)',
    'fiscal_year':              'SAP fiscal year — part of composite key for documents that reset annually',
    'invoice_number':           'SAP invoice document number (RBKP.BELNR)',
    'requisition_number':       'SAP purchase requisition number (EBAN.BANFN)',

    # Link business keys
    'po_item_number': 'Line item number within a PO (EKPO.EBELP)',

    # Satellite — vendor
    'vendor_name':            'Company name of the vendor/supplier',
    'country_code':           'ISO country code of vendor headquarters',
    'city':                   'City where vendor is located',
    'street_address':         'Street address of vendor',
    'phone_number':           'Primary phone contact',
    'payment_terms':          'Agreed payment terms (NET30, NET45, NET60)',
    'reconciliation_account': 'GL account for vendor payables',
    'payment_method':         'How payments are made (T=transfer)',

    # Satellite — material
    'material_type':              'SAP material type (HAWA=trading goods)',
    'material_group':             'CPE category (CPE-RTR, CPE-ONT, CPE-STB, CPE-SWT, CPE-MDM)',
    'base_unit_of_measure':       'Default counting unit (ST=piece)',
    'gross_weight_kg':            'Physical weight per unit in kilograms',
    'material_description':       'Human-readable product name',
    'product_hierarchy':          'Classification path (CPE/category/material)',
    'serial_number_profile':      'SAP serialization profile — HT01 means serial-tracked',
    'planned_delivery_time_days': 'Expected vendor lead time in days',

    # Satellite — PO
    'po_date':                 'Date the purchase order was created',
    'document_type':           'PO type (NB=standard purchase order)',
    'purchasing_organization': 'HT procurement org code',
    'purchasing_group':        'Buyer group responsible for this purchase',
    'currency':                'Transaction currency (EUR)',
    'total_po_value':          'Sum of all line item values on this PO',
    'processing_status':       'Current PO status in SAP workflow',
    'created_by':              'SAP user who created this record',
    'ordered_quantity':        'Number of units ordered on this line item',
    'unit_price':              'Price per unit in transaction currency',
    'net_value':               'Total value for this line item (qty × price)',
    'item_short_text':         'Brief description of ordered item',
    'scheduled_delivery_date': 'Promised delivery date from vendor',
    'goods_received_quantity': 'Cumulative quantity received against this schedule line',
    'gl_account':              'General ledger account for cost posting',
    'cost_center':             'Organizational cost center charged',

    # Satellite — GR
    'posting_date':          'Date the transaction was posted in SAP',
    'document_date':         'Date on the physical document',
    'posted_by':             'SAP user who posted this transaction',
    'header_text':           'Free text description on the document header',
    'reference_document':    'Cross-reference to related document (e.g., PO number on GR)',
    'movement_type':         'SAP movement type code (101=GR, 201=deploy, 161=return, 122=vendor return)',
    'storage_location':      'Specific storage area within a plant',
    'quantity':              'Number of units in this movement',
    'amount_local_currency': 'Financial value of this movement in EUR',
    'vendor_id_on_movement': 'Vendor associated with this goods movement',

    # Satellite — equipment
    'serial_number':      'Manufacturer serial number for this specific device',
    'manufacturer':       'Company that manufactured this CPE device',
    'model_description':  'Product model name/number',
    'startup_date':       'Date device was first deployed to a customer',
    'equipment_category': 'Type classification (CPE)',
    'status_code':        'Current lifecycle code (AVLB/INST/RET/DLFL)',
    'status_description': 'Human-readable lifecycle status',
    'status_from_date':   'Date this status became effective',

    # Satellite — invoice
    'invoice_date':             'Date shown on the vendor invoice',
    'invoice_total_amount':     'Total invoiced amount',
    'vendor_invoice_reference': "Vendor's own invoice number",
    'purchase_order_reference': 'PO this invoice relates to',

    # Satellite — stock
    'unrestricted_stock':       'Available stock that can be used immediately',
    'quality_inspection_stock': 'Stock held for quality review',
    'blocked_stock':            'Stock that cannot be used (defective, disputed)',

    # Satellite — PR
    'requested_quantity': 'Number of units requested in the PR',
    'estimated_price':    'Estimated unit cost at time of requisition',
    'requisition_date':   'Date the PR was created',
    'release_date':       'Date the PR was approved/released',
    'source_indicator':   'How PR was created (B=MRP-generated, blank=manual)',
    'status':             'PR processing status (B=converted to PO, N=open)',
}


def describe_vault_column(column_name: str) -> str:
    """Look up a vault column's description. Prefix entries (ending in `_`)
    match any column name that starts with that prefix; used for `hk_*`."""
    name = str(column_name or '').lower()
    if name in VAULT_COLUMN_DESCRIPTIONS:
        return VAULT_COLUMN_DESCRIPTIONS[name]
    for prefix, desc in VAULT_COLUMN_DESCRIPTIONS.items():
        if prefix.endswith('_') and name.startswith(prefix):
            return desc
    return '—'
