"""Single source of truth for the raw source schema the EDA analyzers profile.

The analyzers are source-agnostic SQL — the only SAP-specific thing about
them is which DuckDB schema they scan. Point them at another source by
setting the DG_SOURCE_SCHEMA environment variable (e.g. `raw_olist`);
the default remains the repo's SAP demo schema.
"""
import os

SOURCE_SCHEMA = os.environ.get("DG_SOURCE_SCHEMA", "raw_sap")

# One-line business framing injected into generation prompts. Override to
# match the source you point the agent at (e.g. "a Brazilian e-commerce
# marketplace (Olist) order-to-delivery data product").
DOMAIN_CONTEXT = os.environ.get(
    "DG_DOMAIN_CONTEXT",
    "a telecom (Helios Telecom) CPE procurement data product",
)
