"""Orvix Node Software — Python agent that runs on GPU provider machines.

Connects to the Orvix Orchestrator over WebSocket, registers its GPU, receives
inference jobs, executes them, and returns results.
"""

import os
import sys

# Add shared packages to Python path so `orvix_protocol` is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "protocol"))

from orvix_node.version import __version__

__all__ = ["__version__"]
