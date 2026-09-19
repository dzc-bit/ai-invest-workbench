"""Stable AI error codes consumed by the frontend.

Extends the service-level error contract (no_local_data / validation_error /
payload_error / request_failed) with AI-specific codes; the mapping lives here
so every AI error path carries a stable ``code``.
"""

from __future__ import annotations


class AiError(Exception):
    """Base class for AI module failures with a machine-readable code."""

    code = "request_failed"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class AiNotConfigured(AiError):
    """Raised when the AI module is used before base_url/model/api_key are set."""

    code = "ai_not_configured"


class AiUpstreamError(AiError):
    """Raised when the configured LLM provider fails (network, auth, quota...)."""

    code = "ai_upstream_error"


class AiSessionBusy(AiError):
    """Raised when a chat turn is requested while the same session is still
    generating its previous answer (e.g. after the user pressed 停止 but the
    worker is still running)."""

    code = "ai_session_busy"


class AiSessionNotFound(AiError):
    """Raised when a stored session is requested but no longer exists
    (never created, already deleted, or its JSON file is unreadable)."""

    code = "ai_session_not_found"


def ai_error_code(exc: Exception) -> str:
    if isinstance(exc, AiError):
        return exc.code
    return "request_failed"
