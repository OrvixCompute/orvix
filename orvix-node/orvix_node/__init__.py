"""Orvix Node Software — Python agent that runs on GPU provider machines.

Connects to the Orvix Orchestrator over WebSocket, registers its GPU, receives
inference jobs, executes them, and returns results.
"""

from orvix_node.version import __version__

__all__ = ["__version__"]
