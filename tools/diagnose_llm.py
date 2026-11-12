"""Diagnose LUFY's AI connection (OpenRouter by default).   Run:  python tools/diagnose_llm.py

1. Shows how the key / base URL / model are loaded from .env (the key itself is NEVER printed).
2. Checks the model ID exists in OpenRouter's catalogue (free, no key needed).
3. Checks your key and credit status via GET /key (free).
4. Sends one tiny STREAMING chat request (~10 tokens, a fraction of a cent) and prints the real HTTP status / error code.
Nothing in your configuration is modified.
"""
from __future__ import annotations

import asyncio
import difflib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402

import config  # noqa: E402
from core.ai_brain import _OPENAI_KEY_PREFIXES, normalize_model  # noqa: E402
from core.errors import LufyError, api_error_info, classify, redact  # noqa: E402

OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"
FIXES = {
    "auth": "The key was rejected. Create a key at openrouter.ai/keys, put it in OPENAI_API_KEY in .env (it must start with sk-or-), restart.",
    "billing": "Insufficient credits (HTTP 402). This is billing, not a rate limit: add credits at openrouter.ai/credits.",
    "budget": "A spending limit on the key/workspace is used up. Raise it in OpenRouter settings or wait for the reset time shown.",
    "model": "The model ID isn't available. Use provider/model format, e.g. openai/gpt-4o-mini (see openrouter.ai/models).",
    "rate_limit": "Rate limited (HTTP 429). LUFY retries temporary limits with backoff; a daily free-model limit needs credits or a reset.",
    "server": "The upstream provider is down/overloaded or timed out (502/503/408). Retry later or choose another model.",
    "network": "Can't reach OpenRouter: check internet, firewall, proxy/VPN, or OPENAI_BASE_URL.",
    "policy": "The request was declined by moderation or a guardrail. Try different wording.",
    "empty": "The model returned an empty response. Retry, or pick another model.",
}


def mask(key: str) -> str:
    return f"{key[:6]}... ({len(key)} characters)" if key else "(empty)"


class _Resp:
    def __init__(self, status: int, headers: dict, raw: bytes):
        self.status_code, self.headers, self._raw = status, {k.lower(): v for k, v in headers.items()}, raw

    def json(self):
        import json
        return json.loads(self._raw or b"{}")


def _get(url: str, key: str = "") -> _Resp:
    """Dependency-free GET (stdlib only) - works with any version of the openai package."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return _Resp(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return _Resp(e.code, dict(e.headers), e.read())


class _HttpErr(Exception):
    """Adapter so classify() can read a raw HTTP response like an SDK exception."""

    def __init__(self, resp):
        super().__init__(f"HTTP {resp.status_code}")
        self.response, self.status_code = resp, resp.status_code
        try:
            self.body = resp.json().get("error", resp.json())
        except Exception:
            self.body = None


def section_config():
    print("\n== 1. Configuration ==")
    env_path = config.ENV_FILE
    print(f"{OK if env_path.exists() else BAD} .env file: {env_path} {'found' if env_path.exists() else 'NOT FOUND (copy .env.example to .env)'}")
    vals = dotenv_values(env_path) if env_path.exists() else {}
    print(f"     variables defined in .env: {', '.join(sorted(k for k, v in vals.items() if v)) or '(none)'}")
    for name in config.env_overrides():
        print(f"{WARN} a system environment variable {name} is also set with a different value - it is IGNORED (.env wins).")
    key, vend = config.ENV.openai_api_key, config.vendor()
    if not key:
        print(f"{BAD} OPENAI_API_KEY is empty (put your OpenRouter key there)")
    else:
        print(f"{OK} OPENAI_API_KEY loaded: {mask(key)}")
        if vend == "openrouter" and key.startswith(_OPENAI_KEY_PREFIXES):
            print(f"{BAD} this is an OpenAI-style key (sk-proj-...). OpenRouter needs an OpenRouter key (sk-or-v1-...) - it will return 401.")
        elif vend == "openrouter" and not key.startswith("sk-or-"):
            print(f"{WARN} OpenRouter keys normally start with 'sk-or-'")
    print(f"{OK if vend == 'openrouter' else WARN} endpoint: {config.ENV.openai_base_url}  (vendor: {vend})")
    from core.memory import Memory
    from database.database import Database
    mem = Memory(Database(config.DB_PATH))
    override = mem.get("llm_model")
    raw_model = override or config.default_model()
    if override:
        print(f"{WARN} Settings has a saved model OVERRIDE: '{override}' (used instead of .env)")
    try:
        model = normalize_model(raw_model, vend)
        note = "" if model == raw_model else f"  (auto-fixed from '{raw_model}')"
        print(f"{OK} model: {model}{note}")
    except LufyError as e:
        print(f"{BAD} model: {e.user_message}")
        model = raw_model
    if mem.get("llm_provider") != "openai":
        print(f"{WARN} LLM provider in Settings is '{mem.get('llm_provider')}', not 'openai' - OpenRouter is not being used")
    return key, vend, model


async def section_requests(key: str, vend: str, model: str) -> str:
    from openai import AsyncOpenAI
    base = config.ENV.openai_base_url.rstrip("/")
    verdict = "ok"

    if vend == "openrouter":
        print(f"\n== 2. Is '{model}' in OpenRouter's catalogue? (public, free) ==")
        try:
            r = await asyncio.to_thread(_get, f"{base}/models")
            if r.status_code != 200:
                raise _HttpErr(r)
            ids = [m["id"] for m in r.json().get("data", [])]
            if model in ids:
                print(f"{OK} found ({len(ids)} models listed)")
            else:
                close = difflib.get_close_matches(model, ids, n=4, cutoff=0.5)
                print(f"{BAD} '{model}' is NOT in the catalogue. Did you mean: {', '.join(close) or '(no close match)'}")
                verdict = "model"
        except Exception as e:
            verdict = report(e, vend)

        print("\n== 3. Key and credit status: GET /key (free) ==")
        try:
            r = await asyncio.to_thread(_get, f"{base}/key", key)
            if r.status_code != 200:
                raise _HttpErr(r)
            d = r.json().get("data", {})
            print(f"{OK} HTTP 200 - key accepted")
            for k in ("label", "usage", "limit", "limit_remaining", "is_free_tier"):
                if k in d:
                    print(f"     {k}: {redact(d[k])}")
            if d.get("limit_remaining") is not None and d["limit_remaining"] <= 0:
                print(f"{BAD} this key's spending limit is used up")
                verdict = "budget"
        except Exception as e:
            v = report(e, vend)
            verdict = v if verdict == "ok" else verdict

    client = AsyncOpenAI(api_key=key or "missing", base_url=base, timeout=30, max_retries=0)   # max_retries=0 -> see the TRUE status
    if vend != "openrouter":                                  # direct OpenAI / other compatible servers
        print(f"\n== 2. Model lookup (free, uses no tokens): GET /models/{model} ==")
        try:
            await client.models.retrieve(model)
            print(f"{OK} HTTP 200 - key is valid and model '{model}' is visible to it")
        except Exception as e:
            verdict = report(e, vend)
    print("\n== 4. Tiny streaming chat request (max 20 tokens) ==")
    try:
        t0, first, text = time.time(), None, ""
        stream = await client.chat.completions.create(model=model, stream=True, max_tokens=20,
                                                      messages=[{"role": "user", "content": "Reply with: OK"}])
        async for ev in stream:
            if ev.choices and ev.choices[0].delta and ev.choices[0].delta.content:
                first = first or time.time() - t0
                text += ev.choices[0].delta.content
        if not text:
            print(f"{BAD} stream finished with no text")
            return "empty"
        print(f"{OK} streaming works - reply {text.strip()!r}, first token after {first:.2f}s")
    except Exception as e:
        verdict = report(e, vend)
    return verdict


def report(e: Exception, vend: str) -> str:
    info, err = api_error_info(e), classify(e, "AI", vendor=vend)
    print(f"{BAD} HTTP status : {info.status}")
    print(f"     error code  : {info.code}")
    if info.error_type:
        print(f"     error_type  : {info.error_type}")
    print(f"     message     : {redact(info.message) or redact(e)}")
    print(f"     request id  : {info.request_id}")
    if info.retry_after:
        print(f"     retry-after : {info.retry_after}s")
    print(f"     => classified as: {err.kind.upper()}")
    print(f"     => {FIXES.get(err.kind, err.user_message)}")
    return err.kind


def main() -> int:
    key, vend, model = section_config()
    if not key and vend != "generic":
        print("\nCannot test without a key.")
        return 2
    verdict = asyncio.run(section_requests(key, vend, model))
    print("\n== Result ==")
    print("Everything works: LUFY's AI connection is healthy." if verdict == "ok"
          else f"Cause: {verdict.upper()} -> {FIXES.get(verdict, 'see details above')}")
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
