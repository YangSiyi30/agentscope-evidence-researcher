"""Public model-error categories; never retain provider text, credentials or headers."""

import asyncio
import logging

import openai


class _SafeRetryLog(logging.Filter):
    """Redact the pinned SDK's retry messages before its handler sees them.

    AgentScope 2.0.8 formats str(provider_exception) into these records. Keep
    other framework logs intact; safe per-attempt details live in request_events.
    """

    def filter(self, record):
        if record.module == "_base" and str(record.msg).startswith(
            (
                "Attempt %d failed for model",
                "Structured output attempt",
                "Structured output strategy",
            )
        ):
            record.msg = "Model attempt failed; consult the sanitized model_requests record."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def protect_retry_logging():
    logger = logging.getLogger("as")
    if not any(isinstance(item, _SafeRetryLog) for item in logger.filters):
        logger.addFilter(_SafeRetryLog())


SAFE_PROVIDER_CODES = frozenset(
    {
        "invalid_request_error",
        "invalid_api_key",
        "authentication_error",
        "insufficient_quota",
        "rate_limit_exceeded",
        "context_length_exceeded",
        "model_not_found",
    }
)
SAFE_PARAMETERS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "thinking",
        "reasoning_effort",
        "stream",
    }
)


def model_error_details(exc: Exception) -> dict:
    """Classify by SDK type/status; optional provider code/param are allowlisted."""
    status = getattr(exc, "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    if isinstance(exc, (openai.APITimeoutError, TimeoutError)):
        code, kind = "model_timeout", "APITimeoutError"
    elif isinstance(exc, openai.APIConnectionError):
        code, kind = "model_connection_error", "APIConnectionError"
    elif isinstance(exc, openai.APIStatusError):
        code = {
            400: "model_invalid_request",
            401: "model_authentication_error",
            403: "model_permission_denied",
            404: "model_not_found",
            429: "model_rate_limited",
        }.get(status, "model_server_error" if status and status >= 500 else "model_http_error")
        kind = "APIStatusError"
    elif isinstance(exc, asyncio.CancelledError):
        code, kind = "model_cancelled", "CancelledError"
    else:
        code, kind = "model_adapter_error", "ModelAdapterError"
    result = {"error_code": code, "error_type": kind, "http_status": status}
    provider_code, parameter = getattr(exc, "code", None), getattr(exc, "param", None)
    if isinstance(provider_code, str) and provider_code in SAFE_PROVIDER_CODES:
        result["provider_code"] = provider_code
    if isinstance(parameter, str) and parameter in SAFE_PARAMETERS:
        result["parameter"] = parameter
    return result
