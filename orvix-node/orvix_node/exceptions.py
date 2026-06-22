"""Custom exception hierarchy for the node software."""


class OrvixNodeError(Exception):
    """Base class for all node-software errors."""


class ConfigError(OrvixNodeError):
    """Configuration is missing or invalid."""


class ConnectionError(OrvixNodeError):  # noqa: A001 — intentional domain name
    """Failed to establish or maintain the orchestrator connection."""


class AuthError(OrvixNodeError):
    """The orchestrator rejected our credentials. Non-retryable."""


class InferenceError(OrvixNodeError):
    """An inference backend failed to produce a result."""


class GPUError(OrvixNodeError):
    """GPU detection or access failed."""
