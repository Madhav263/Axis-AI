"""
exceptions.py — Axis AI custom exception hierarchy.
All internal errors descend from AxisError so callers
can catch them with a single except clause when needed.
"""


class AxisError(Exception):
    """Base for all Axis AI errors."""


# ── Provider errors ───────────────────────────────────────────────────────────

class ProviderError(AxisError):
    """Any AI provider failed to return a result."""

class ProviderQuotaError(ProviderError):
    """Rate-limit / quota exhausted (HTTP 429)."""

class ProviderAuthError(ProviderError):
    """Invalid or missing API key (HTTP 401/403)."""

class ProviderModelError(ProviderError):
    """Requested model not found (HTTP 404)."""

class ProviderUnavailableError(ProviderError):
    """Provider overloaded or offline (HTTP 503)."""

class ProviderBadResponseError(ProviderError):
    """Model returned non-JSON or malformed output."""

class AllProvidersFailedError(ProviderError):
    """Every provider in the chain failed."""


# ── Layer errors ──────────────────────────────────────────────────────────────

class LayerError(AxisError):
    """An intelligence layer could not handle the request."""

class NoLayerMatchError(LayerError):
    """No layer matched the input — escalate to the next."""

class AllLayersFailedError(LayerError):
    """Every intelligence layer failed."""


# ── Safety errors ─────────────────────────────────────────────────────────────

class SafetyError(AxisError):
    """A dangerous action was blocked pending confirmation."""

    def __init__(self, action: str, description: str = ""):
        self.action = action
        self.description = description
        super().__init__(f"Safety block on '{action}': {description}")


# ── Config / startup errors ───────────────────────────────────────────────────

class ConfigError(AxisError):
    """Configuration file is missing or invalid."""

class MissingApiKeyError(AxisError):
    """Required environment variable not set."""
