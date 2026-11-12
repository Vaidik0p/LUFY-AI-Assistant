"""LLM layer with swappable providers. Add a provider by subclassing LLMProvider and registering it."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import AsyncIterator, Callable, Optional

import config
from config import ENV
from core.errors import AuthError, EmptyResponseError, LufyError, ModelNotFoundError, api_error_info, classify, redact

log = logging.getLogger("lufy.llm")
_sleep = asyncio.sleep          # indirection so tests can run without real waiting

MAX_RETRIES = {"rate_limit": 3, "server": 3, "network": 2, "empty": 1}   # retries after the first attempt
BACKOFF_BASE_S, BACKOFF_CAP_S = 1.0, 20.0
BLOCK_TTL_S = 120               # after a billing/auth/model error, stop calling the API for this long

_OPENAI_MODEL_RE = re.compile(r"^(gpt-|chatgpt-|o\d|text-|davinci)", re.I)
_OPENAI_KEY_PREFIXES = ("sk-proj-", "sk-svcacct-", "sk-admin-")


def normalize_model(model: str, vend: str) -> str:
    """OpenRouter model IDs are 'provider/model' (e.g. openai/gpt-4o-mini).

    A bare OpenAI name such as 'gpt-4o-mini' is auto-prefixed with 'openai/'; anything else without a slash
    is rejected with a clear message instead of sending a request that is guaranteed to 404.
    """
    model = (model or "").strip()
    if vend != "openrouter":
        return model
    if "/" in model:
        return model
    if _OPENAI_MODEL_RE.match(model):
        fixed = f"openai/{model}"
        log.info("OpenRouter needs provider/model format; using %s instead of %s", fixed, model)
        return fixed
    raise ModelNotFoundError(f"AI (OpenRouter): '{model}' is not a valid OpenRouter model ID. Use provider/model format, e.g. "
                             f"openai/gpt-4o-mini (see openrouter.ai/models).", fatal=True, blocking=True,
                             spoken="The AI model name isn't valid for OpenRouter. Please check the settings.")


def check_key_matches_vendor(key: str, vend: str) -> None:
    """Fail fast (no HTTP request) when an OpenAI key is used with OpenRouter - it can only ever return 401."""
    if vend == "openrouter" and key.startswith(_OPENAI_KEY_PREFIXES):
        raise AuthError("AI (OpenRouter): OPENAI_API_KEY contains an OpenAI key (sk-proj-...). OpenRouter needs an OpenRouter key "
                        "(sk-or-v1-..., create one at openrouter.ai/keys). Put it in OPENAI_API_KEY in .env and restart.",
                        fatal=True, blocking=True, spoken="My AI key is an OpenAI key, but I'm connected to OpenRouter. "
                        "Please put an OpenRouter key in the settings file.")


SYSTEM_PROMPT = """You are LUFY, a friendly voice assistant running on the user's Windows laptop.
Your replies are spoken aloud, so:
- Keep answers short and natural (1-4 sentences) unless the user asks for detail or code.
- No markdown, bullet points, emojis or URLs. Say numbers and symbols the way a person would speak them.
- Reply in the user's language and style: English, Hindi, or Hinglish (Hindi in Roman script). Match their script.
- For code or debugging help, explain the idea in plain speech and give only the key line(s); say the user can ask for the full code in the chat window.
- If you don't know something or can't do it from here, say so plainly. Never invent facts.
- You cannot run commands yourself. Opening apps, files and system actions are handled by LUFY's built-in commands; if the user asks for one you couldn't recognise, suggest rephrasing it simply (for example "open Chrome").
Today's date: {today}.
{memories}"""


class LLMProvider:
    name = "base"

    def __init__(self, model: str = ""):
        self.model = model

    async def stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        raise NotImplementedError
        yield ""  # pragma: no cover

    def configured(self) -> bool:
        return True


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible client: OpenRouter (default), OpenAI, or any compatible server (Ollama, LM Studio...).

    Endpoint, key and model come from .env (OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL).
    """
    name = "openai"

    def __init__(self, model: str = ""):
        super().__init__(model or config.default_model())
        self._client = None
        self._client_for = None

    @property
    def vendor(self) -> str:
        return config.vendor()

    @property
    def label(self) -> str:
        return {"openrouter": "OpenRouter", "openai": "OpenAI"}.get(self.vendor, "OpenAI-compatible")

    def request_model(self) -> str:
        return normalize_model(self.model, self.vendor)

    def configured(self) -> bool:
        if ENV.openai_api_key:
            return True
        return self.vendor == "generic"           # local servers (Ollama...) don't need a key

    def _c(self):
        fingerprint = (ENV.openai_base_url, bool(ENV.openai_api_key))
        if self._client is None or self._client_for != fingerprint:
            from openai import AsyncOpenAI
            # max_retries=0: LUFY owns the retry policy (no hidden duplicate requests).
            self._client = AsyncOpenAI(api_key=ENV.openai_api_key or "not-needed", base_url=ENV.openai_base_url,
                                       timeout=30, max_retries=0)
            self._client_for = fingerprint
        return self._client

    async def stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        check_key_matches_vendor(ENV.openai_api_key, self.vendor)
        model = self.request_model()
        resp = await self._c().chat.completions.create(
            model=model, stream=True, max_tokens=500, temperature=0.6,
            messages=[{"role": "system", "content": system}, *messages])
        async for ev in resp:                      # (an SSE `error` event makes the SDK raise APIError - handled by the brain)
            if not ev.choices:                     # e.g. the final usage-only chunk
                continue
            ch = ev.choices[0]
            if ch.delta and ch.delta.content:
                yield ch.delta.content
            if getattr(ch, "finish_reason", None) == "error":
                raise LufyError(f"AI ({self.label}): the provider reported an error mid-response.", retryable=False)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str = ""):
        super().__init__(model or ENV.anthropic_model)
        self._client = None

    def configured(self) -> bool:
        return bool(ENV.anthropic_api_key)

    async def stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=ENV.anthropic_api_key, timeout=30, max_retries=0)
        async with self._client.messages.stream(model=self.model, max_tokens=500, system=system, messages=messages) as s:
            async for text in s.text_stream:
                yield text


PROVIDERS = {"openai": OpenAIProvider, "anthropic": AnthropicProvider}


class AIBrain:
    def __init__(self, memory):
        self.memory = memory
        self._provider: Optional[LLMProvider] = None
        self._key = None
        self._blocked: Optional[tuple[float, LufyError]] = None
        # called as on_retry(error, delay_seconds, attempt_number, max_retries) before each backoff sleep
        self.on_retry: Callable[[LufyError, float, int, int], None] = lambda *a: None

    def reset(self) -> None:
        """Forget a cached billing/auth/model block (called when settings are saved)."""
        self._blocked = None
        self._provider = None

    def provider(self) -> LLMProvider:
        name = self.memory.get("llm_provider") or ENV.llm_provider
        model = self.memory.get("llm_model")
        if self._provider is None or self._key != (name, model):
            cls = PROVIDERS.get(name)
            if cls is None:
                raise LufyError(f"Unknown LLM provider '{name}'. Choose one of: {', '.join(PROVIDERS)}.")
            self._provider, self._key = cls(model), (name, model)
        return self._provider

    def configured(self) -> bool:
        try:
            return self.provider().configured()
        except LufyError:
            return False

    def describe(self) -> str:
        """Short, secret-free description for the status chip, e.g. 'OpenRouter · openai/gpt-4o-mini'."""
        try:
            prov = self.provider()
            label = getattr(prov, "label", prov.name)
            try:
                model = prov.request_model() if hasattr(prov, "request_model") else prov.model
            except LufyError:
                model = prov.model
            return f"{label} · {model}"
        except LufyError:
            return "unknown provider"

    def system_prompt(self) -> str:
        from datetime import date
        mems = self.memory.list_memories()
        block = ("Things the user asked you to remember:\n" + "\n".join(f"- {m['content']}" for m in mems)) if mems else ""
        return SYSTEM_PROMPT.format(today=date.today().strftime("%A, %d %B %Y"), memories=block)

    # ------------------------------------------------------------------ request with retry policy
    def _backoff(self, attempt: int, err: LufyError) -> float:
        delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** attempt)) + random.uniform(0, 0.4)
        if err.retry_after:                                   # server told us how long to wait
            delay = max(delay, min(err.retry_after, BACKOFF_CAP_S))
        return delay

    async def reply_stream(self, user_text: str) -> AsyncIterator[str]:
        """Yield text deltas. Context = stored history (last ~12 messages).

        * 401/403/404 and 429 insufficient_quota are NEVER retried; further requests are blocked for BLOCK_TTL_S
          so a broken key/billing state doesn't hammer the API on every question.
        * 429 rate_limit_exceeded, 5xx and network errors are retried with exponential backoff + jitter
          (only before any text was produced, so we never repeat half an answer).
        """
        prov = self.provider()
        if not prov.configured():
            raise LufyError(f"No API key for the '{prov.name}' provider. Add it to .env (see .env.example).", fatal=True)
        if self._blocked:
            until, err = self._blocked
            if time.time() < until:
                raise err                                      # no HTTP request is made
            self._blocked = None
        history = self.memory.recent_messages(12)
        if not history or history[-1]["content"] != user_text:
            history.append({"role": "user", "content": user_text})
        while history and history[0]["role"] != "user":        # providers need a user turn first
            history.pop(0)
        system = self.system_prompt()
        label = getattr(prov, "label", prov.name)

        attempt = 0
        while True:
            started = False
            try:
                async for delta in prov.stream(system, history):
                    started = True
                    yield delta
                if not started:                       # 200 OK but no text (cold start / provider hiccup) - worth one retry
                    raise EmptyResponseError(f"AI ({label}): the model returned an empty response.", retryable=True,
                                             spoken="I didn't get an answer. Please try again.")
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                err = classify(e, f"AI ({label})", vendor=getattr(prov, "vendor", prov.name))
                if err.kind == "model" and "Model:" not in err.user_message:
                    err.user_message += f" Model: {getattr(prov, 'model', '?')}."
                info = api_error_info(e)
                log.warning("LLM request failed: provider=%s model=%s status=%s code=%s type=%s request_id=%s attempt=%d kind=%s",
                            prov.name, prov.model, info.status, info.code, info.type, info.request_id, attempt + 1, err.kind)
                limit = MAX_RETRIES.get(err.kind, 0) if err.retryable else 0
                if started or attempt >= limit:
                    if err.blocking:
                        self._blocked = (time.time() + BLOCK_TTL_S, err)
                    if attempt and err.retryable:
                        err.user_message += f" Gave up after {attempt + 1} attempts."
                    raise err from None
                delay = self._backoff(attempt, err)
                attempt += 1
                log.info("retrying in %.1fs (retry %d/%d)", delay, attempt, limit)
                self.on_retry(err, delay, attempt, limit)
                await _sleep(delay)
