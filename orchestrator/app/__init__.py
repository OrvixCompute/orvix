"""Orvix Orchestrator — FastAPI backend for the Orvix decentralized AI compute network."""

import os
import sys

# Add shared packages to Python path so `orvix_protocol` is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "protocol"))

__version__ = "0.2.0"
