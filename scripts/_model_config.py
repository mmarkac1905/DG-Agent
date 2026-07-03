"""Single source of truth for the Claude model id used across the pipeline.

Every pipeline script and the app import MODEL from here instead of
hardcoding the model string. Override per run with the DG_AGENT_MODEL
environment variable (e.g. to try a smaller/cheaper model).
"""
import os

MODEL = os.environ.get("DG_AGENT_MODEL", "claude-sonnet-4-6")
