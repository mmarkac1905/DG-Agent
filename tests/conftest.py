"""Shared test configuration.

The suite's fixtures are built against the default SAP demo source.
Pin the source-selection env vars so a shell that has pointed the
pipeline at another source (DG_SOURCE_SCHEMA=raw_olist, ...) doesn't
silently break fixture assumptions.
"""
import os

os.environ.pop("DG_SOURCE_SCHEMA", None)
os.environ.pop("DG_ENABLE_OLIST", None)
os.environ.pop("DG_DOMAIN_CONTEXT", None)
