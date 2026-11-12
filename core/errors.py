"""Turn low-level exceptions into precise, actionable errors - and keep secrets out of every message/log."""
from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from typing import Optional

# --------------------------------------------------------------------------- secret redaction
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),          # OpenAI (sk-..., sk-proj-...)
    re.compile(r"vach_[A-Za-z0-9_\-]{8,}"),        # Gnani
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|x-api-key-id|authorization)\s*[:=]\s*)['\"]?[A-Za-z0-9._\-]{8,}"),
]


def redact(text: object) -> str:
    s = str(text)
    for pat in _SECRET_PATTERNS:
        s = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", s)
    return s


class RedactingFormatter(logging.Formatter):
    """Formats the whole record (message AND traceback) then scrubs secrets."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


# --------------------------------------------------------------------------- error types
class LufyError(Exception):
    """`.user_message` is safe to show in the UI; `.spoken` is a short sentence for TTS."""

    kind = "error"

    def __init__(self, user_message: str, *, fatal: bool = False, retryable: bool = False, blocking: bool = False,
                 spoken: Optional[str] = None, status: Optional[int] = None, code: Optional[str] = None,
                 request_id: Optional[str] = None, retry_after: Optional[float] = None):
        super().__init__(redact(user_message))
        self.user_message = redact(user_message)
        self.fatal = fatal            # never retry (STT reconnect loop stops)
        self.retryable = retryable    # worth retrying with backoff
        self.blocking = blocking      # don't send more requests until the user fixes something
        self.spoken = spoken
        self.status, self.code, self.request_id, self.retry_after = status, code, request_id, retry_after


class AuthError(LufyError):
    kind = "auth"


class BillingError(LufyError):
    kind = "billing"


class BudgetError(BillingError):
    kind = "budget"


class ModelNotFoundError(LufyError):
    kind = "model"


class RateLimitError(LufyError):
    kind = "rate_limit"


class ServerError(LufyError):
    kind = "server"


class NetworkError(LufyError):
    kind = "network"


class PolicyError(LufyError):
    """Request declined by moderation / a guardrail / a content filter. Only this request is affected."""
    kind = "policy"


class EmptyResponseError(LufyError):
    kind = "empty"


# --------------------------------------------------------------------------- API error extraction
@dataclass
class ApiErrorInfo:
    status: Optional[int] = None
    code: Optional[str] = None
    type: Optional[str] = None
    message: str = ""
    request_id: Optional[str] = None
    retry_after: Optional[float] = None
    error_type: Optional[str] = None            # OpenRouter typed code (error.metadata.error_type)
    metadata: Optional[dict] = None


def api_error_info(exc: BaseException) -> ApiErrorInfo:
    """Pull HTTP status / API error code / request id / Retry-After from OpenAI-, OpenRouter- or Anthropic-style exceptions.

    OpenAI:      {"error": {"message", "type", "code": "insufficient_quota"}}   (code is a string)
    OpenRouter:  {"error": {"code": 402, "message", "metadata": {"error_type", ...}}}   (code is the HTTP status, an int)
    Mid-stream OpenRouter errors arrive with HTTP 200 inside the SSE stream; the SDK raises APIError with no status,
    so the numeric code in the body is used as the status.
    """
    resp = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(resp, "status_code", None) or getattr(exc, "status", None)
    body = getattr(exc, "body", None)
    code = typ = None
    msg = ""
    meta: dict = {}
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            code, typ, msg = err.get("code"), err.get("type"), err.get("message") or ""
            meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    if code is None:
        code = getattr(exc, "code", None)
    if not isinstance(status, int) and isinstance(code, int) and 100 <= code <= 599:
        status = code
    headers = getattr(resp, "headers", None) or {}
    rid = getattr(exc, "request_id", None) or headers.get("x-request-id") or headers.get("request-id")
    retry_after = None
    try:
        if headers.get("retry-after-ms"):
            retry_after = float(headers["retry-after-ms"]) / 1000
        elif headers.get("retry-after"):
            retry_after = float(headers["retry-after"])
    except (ValueError, TypeError):
        pass
    return ApiErrorInfo(status if isinstance(status, int) else None, str(code) if code is not None else None,
                        str(typ) if typ else None, redact(msg or getattr(exc, "message", "") or ""), rid, retry_after,
                        str(meta.get("error_type")) if meta.get("error_type") else None, meta)


_BILLING_HINTS = ("insufficient_quota", "billing", "exceeded your current quota", "credit balance is too low",
                  "payment required")
_VENDOR_NAMES = {"openrouter": "OpenRouter", "openai": "OpenAI"}


def classify(exc: BaseException, service: str = "service", vendor: Optional[str] = None) -> LufyError:
    """Map any exception to a LufyError carrying the *actual* HTTP status / API code in its message.

    `vendor` ("openrouter" | "openai" | other) selects vendor-specific wording and rules.
    """
    if isinstance(exc, LufyError):
        return exc
    i = api_error_info(exc)
    name = type(exc).__name__
    or_ = vendor == "openrouter"
    vname = _VENDOR_NAMES.get(vendor or "", "the AI provider")
    meta, et = i.metadata or {}, i.error_type or ""
    detail = et or (i.code if i.code and i.code != str(i.status) else "")
    tag = (f"HTTP {i.status}" + (f", {detail}" if detail else "")) if i.status else ""
    rid = f" Request id: {i.request_id}." if i.request_id else ""
    common = dict(status=i.status, code=detail or i.code, request_id=i.request_id, retry_after=i.retry_after)
    low = f"{i.code or ''} {i.type or ''} {et} {i.message}".lower()
    limit_source = str(meta.get("limit_source") or "")

    # ---- 401
    if i.status == 401 or i.code == "invalid_api_key" or et == "authentication" or "AuthenticationError" in name:
        extra = (" OpenRouter keys start with 'sk-or-'; an OpenAI key (sk-proj-...) will not work with OpenRouter."
                 if or_ else "")
        return AuthError(f"{service}: the API key was rejected ({tag or 'invalid_api_key'}). Check OPENAI_API_KEY in .env - correct, "
                         f"not revoked/disabled.{extra}{rid}", fatal=True, blocking=True,
                         spoken="My AI key was rejected. Please check the key in the settings file.", **common)
    # ---- 402 (OpenRouter: insufficient credits; OpenAI-compatible gateways may also use it)
    if i.status == 402 or et == "payment_required":
        if i.retry_after and "in_flight" in limit_source:          # temporary spending budget: wait and retry
            return RateLimitError(f"{service}: temporary spending-budget limit ({tag}) - waiting {i.retry_after:.0f}s.{rid}",
                                  retryable=True, spoken="The AI service is busy right now. Please try again in a moment.", **common)
        where = "Add credits at openrouter.ai/credits" if or_ else "Add credits / a payment method with your provider"
        return BillingError(f"{service}: insufficient credits ({tag or 'HTTP 402'}). {where}, then try again. Not retrying.{rid}",
                            fatal=True, blocking=True,
                            spoken=f"{vname} says the account is out of credits. Please add credits.", **common)
    # ---- 403: budget / moderation / guardrail / permission
    if i.status == 403 or et in ("permission_denied", "content_policy_violation", "refusal") or "PermissionDenied" in name:
        if "budget" in limit_source or limit_source == "openrouter_key_limit":
            reset = f" Resets at {meta['resets_at']}." if meta.get("resets_at") else ""
            return BudgetError(f"{service}: spending limit reached ({tag}; {limit_source}).{reset} Raise the limit in OpenRouter "
                               f"or wait for the reset.{rid}", fatal=True, blocking=True,
                               spoken="My spending limit has been reached. Please check the OpenRouter limits.", **common)
        if meta.get("reasons") or et in ("content_policy_violation", "refusal") or any(
                w in low for w in ("moderation", "flagged", "guardrail", "blocked", "content policy")):
            why = f" Reasons: {', '.join(map(str, meta['reasons']))}." if meta.get("reasons") else ""
            return PolicyError(f"{service}: this request was declined by content moderation or a guardrail ({tag}).{why} "
                               f"Try rephrasing.{rid}", spoken="Sorry, the AI service declined that request.", **common)
        return AuthError(f"{service}: access denied ({tag or 'HTTP 403'}). The key lacks permission for this model, or the "
                         f"account/region is restricted.{rid}", fatal=True, blocking=True,
                         spoken="My AI key doesn't have access to that model.", **common)
    # ---- 404 / unavailable model
    if i.status == 404 or i.code == "model_not_found" or et == "not_found":
        hint = ("Model IDs look like provider/model, e.g. openai/gpt-4o-mini - check OPENAI_MODEL and the model override "
                "in Settings (see openrouter.ai/models)." if or_ else "Check OPENAI_MODEL and the model override in Settings.")
        return ModelNotFoundError(f"{service}: model not found or unavailable ({tag or 'model_not_found'}). {hint}{rid}",
                                  fatal=True, blocking=True,
                                  spoken="The AI model isn't available. Please check the model name.", **common)
    # ---- 429
    if i.status == 429 or et == "rate_limit_exceeded" or "RateLimitError" in name:
        if not or_ and any(h in low for h in _BILLING_HINTS):           # OpenAI: insufficient_quota arrives as 429
            return BillingError(f"{service}: the account is out of quota/credit ({tag or 'insufficient_quota'}). This is a billing "
                                f"problem, not a temporary limit - add credits or a payment method at platform.openai.com > "
                                f"Settings > Billing, or use a key from a funded project. Not retrying.{rid}",
                                fatal=True, blocking=True,
                                spoken="My AI provider says the account is out of credit. Please check the billing page.", **common)
        if "per-day" in low or "per day" in low or "daily" in low:      # e.g. OpenRouter free-models-per-day: waiting won't help
            return RateLimitError(f"{service}: daily request limit reached ({tag}). Add credits, use a paid model, or wait for "
                                  f"the daily reset. Not retrying.{rid}", fatal=True, blocking=True,
                                  spoken="The daily limit for this model has been reached.", **common)
        return RateLimitError(f"{service}: temporary rate limit ({tag or 'rate_limit_exceeded'}) - too many requests or tokens "
                              f"per minute.{rid}", retryable=True,
                              spoken="The AI service is busy right now. Please try again in a moment.", **common)
    # ---- 408 / 5xx / provider availability
    if i.status in (408, 502, 503, 504, 529) or (i.status and i.status >= 500) or "InternalServerError" in name \
            or et in ("provider_overloaded", "provider_unavailable", "timeout", "server"):
        what = "timed out" if i.status in (408, 504) or et == "timeout" else "had a problem or has no available provider"
        return ServerError(f"{service}: {vname} {what} ({tag or 'server error'}).{rid}", retryable=True,
                           spoken="The AI service is having problems. Please try again shortly.", **common)
    # ---- network
    if "APIConnectionError" in name or "APITimeoutError" in name or isinstance(exc, (socket.gaierror, ConnectionError, TimeoutError)) \
            or "Connect" in name or "Timeout" in name:
        return NetworkError(f"{service}: cannot reach the server - check your internet connection, proxy or VPN.", retryable=True,
                            spoken="I can't reach the AI service. Please check your internet.", **common)
    if isinstance(exc, OSError) and not i.status:
        return NetworkError(f"{service}: cannot reach the server - check your internet connection.", retryable=True, **common)
    # ---- 400 and everything else
    if et == "context_length_exceeded" or "context length" in low or "context_length" in low:
        return LufyError(f"{service}: the conversation is too long for this model ({tag}). Clear the chat history in Settings.{rid}",
                         spoken="That conversation got too long for me. Try clearing the history.", **common)
    if i.status and i.status >= 400:
        return LufyError(f"{service}: request rejected ({tag}): {i.message or name}.{rid}", **common)
    return LufyError(f"{service}: unexpected error ({name}: {redact(exc)})")
