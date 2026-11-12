"""OpenRouter migration tests: real OpenAI SDK + LUFY's brain against a mock that follows OpenRouter's documented API."""
import logging, os, subprocess, sys, textwrap
from pathlib import Path

import pytest

import config
import core.ai_brain as ai
from core.ai_brain import AIBrain, normalize_model
from core.errors import (AuthError, BillingError, BudgetError, LufyError, ModelNotFoundError, PolicyError, RateLimitError,
                         ServerError, RedactingFormatter, redact)
from core.memory import Memory
from database.database import Database
from tests.openrouter_mock import GOOD_KEY, OpenRouterMock, or_err

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    sleeps = []

    async def fake_sleep(d): sleeps.append(d)
    monkeypatch.setattr(ai, "_sleep", fake_sleep)
    mem = Memory(Database(tmp_path / "x.db")); mem.set("llm_provider", "openai")
    brain, servers = AIBrain(mem), []

    def make(script=(), key=GOOD_KEY, model="openai/gpt-4o-mini"):
        srv = OpenRouterMock(script, GOOD_KEY); servers.append(srv)
        monkeypatch.setattr("config.ENV.openai_api_key", key)
        monkeypatch.setattr("config.ENV.openai_base_url", srv.url)
        monkeypatch.setattr("config.ENV.openai_model", model)
        monkeypatch.setattr("config.ENV.llm_vendor", "openrouter")        # the mock is on 127.0.0.1, so force the vendor
        brain.reset()
        return srv
    yield brain, make, sleeps, mem
    for s in servers: s.shutdown()


async def ask(brain, text="hi"):
    return "".join([d async for d in brain.reply_stream(text)])


# ------------------------------------------------------------------ configuration / model format
def test_defaults_are_openrouter_and_model_format():
    assert config.OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1" and config.OPENROUTER_DEFAULT_MODEL == "openai/gpt-4o-mini"


def test_env_file_is_read_and_wins_over_stale_os_variable(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("OPENAI_API_KEY=sk-or-v1-FROMFILE\nOPENAI_BASE_URL=https://openrouter.ai/api/v1\nOPENAI_MODEL=openai/gpt-4o-mini\nGNANI_API_KEY=gk-file\n")
    code = ("import config;E=config.ENV;print(E.openai_api_key,E.openai_base_url,E.openai_model,config.vendor(),config.default_model(),"
            "E.gnani_api_key,config.env_overrides())")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "LUFY_ENV_FILE": str(envf), "OPENAI_API_KEY": "sk-proj-STALE-SYSTEM-VALUE", "OPENAI_MODEL": "gpt-4o"})
    parts = out.stdout.split()
    assert parts[:6] == ["sk-or-v1-FROMFILE", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "openrouter",
                         "openai/gpt-4o-mini", "gk-file"], out.stdout + out.stderr      # .env values won
    assert "OPENAI_API_KEY" in out.stdout and "OPENAI_MODEL" in out.stdout              # ...and the ignored OS overrides are reported by NAME
    assert "STALE" not in out.stdout                                                    # ...never by value


def test_missing_base_url_and_model_default_to_openrouter(tmp_path):
    envf = tmp_path / ".env"; envf.write_text("OPENAI_API_KEY=sk-or-v1-x\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "LLM_"))} | {"LUFY_ENV_FILE": str(envf)}
    out = subprocess.run([sys.executable, "-c", "import config;print(config.vendor(),config.ENV.openai_base_url,config.default_model())"],
                         cwd=ROOT, capture_output=True, text=True, env=env)
    assert out.stdout.split() == ["openrouter", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"]


@pytest.mark.parametrize("given,vend,expect", [
    ("openai/gpt-4o-mini", "openrouter", "openai/gpt-4o-mini"),
    ("gpt-4o-mini", "openrouter", "openai/gpt-4o-mini"),                       # bare OpenAI name is auto-prefixed
    ("o3-mini", "openrouter", "openai/o3-mini"),
    ("anthropic/claude-haiku-5.5:online", "openrouter", "anthropic/claude-haiku-5.5:online"),
    ("openrouter/auto", "openrouter", "openrouter/auto"),
    ("gpt-4o-mini", "openai", "gpt-4o-mini"), ("llama3", "generic", "llama3"),   # other vendors untouched
])
def test_model_normalisation(given, vend, expect):
    assert normalize_model(given, vend) == expect


def test_unknown_bare_model_is_rejected_for_openrouter():
    with pytest.raises(ModelNotFoundError) as ei:
        normalize_model("llama3", "openrouter")
    assert "provider/model" in ei.value.user_message


# ------------------------------------------------------------------ real conversation through the stack
@pytest.mark.asyncio
async def test_request_shape_streaming_and_history_preserved(env):
    brain, make, sleeps, mem = env
    srv = make()
    mem.add_message("user", "My favourite colour is teal.")
    assert await ask(brain, "My favourite colour is teal.") == "Hello there. I am a mock OpenRouter model."
    mem.add_message("assistant", "Hello there. I am a mock OpenRouter model.")
    mem.add_message("user", "What is my favourite colour?")
    chunks = [d async for d in brain.reply_stream("What is my favourite colour?")]
    assert "".join(chunks) == "Your favourite colour is teal." and len(chunks) > 1          # streamed in pieces
    r1, r2 = srv.chat_requests
    assert r1["path"] == "/api/v1/chat/completions"
    assert r1["headers"]["authorization"] == f"Bearer {GOOD_KEY}"
    assert r1["body"]["model"] == "openai/gpt-4o-mini" and r1["body"]["stream"] is True
    assert r1["body"]["messages"][0]["role"] == "system" and "LUFY" in r1["body"]["messages"][0]["content"]
    roles = [m["role"] for m in r2["body"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]                                  # history preserved, in order
    assert "teal" in r2["body"]["messages"][1]["content"]
    assert not any(k.lower() == "x-api-key-id" for k in r1["headers"])                       # Gnani credentials never sent here
    assert len(srv.requests) == 2 and not sleeps


@pytest.mark.asyncio
async def test_bare_model_name_in_env_is_fixed_on_the_wire(env):
    brain, make, *_ = env
    srv = make(model="gpt-4o-mini")
    assert await ask(brain)
    assert srv.chat_requests[0]["body"]["model"] == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_keepalive_comments_and_usage_chunk_are_ignored(env):
    brain, make, *_ = env
    make()
    assert (await ask(brain, "joke in hinglish")).startswith("Ek aadmi")


# ------------------------------------------------------------------ separate handling of each failure
@pytest.mark.asyncio
async def test_openai_key_with_openrouter_fails_locally_with_no_request(env):
    brain, make, sleeps, _ = env
    srv = make(key="sk-proj-ThisIsAnOpenAIKey1234567890")
    with pytest.raises(AuthError) as ei:
        await ask(brain)
    assert srv.requests == [] and "sk-or-v1" in ei.value.user_message and "sk-proj-This" not in ei.value.user_message


@pytest.mark.asyncio
async def test_401_wrong_openrouter_key(env):
    brain, make, sleeps, _ = env
    srv = make(key="sk-or-v1-wrongwrongwrongwrong")
    with pytest.raises(AuthError) as ei:
        await ask(brain)
    e = ei.value
    assert len(srv.chat_requests) == 1 and not sleeps and e.status == 401 and "OpenRouter keys start with 'sk-or-'" in e.user_message
    assert "wrongwrong" not in e.user_message


@pytest.mark.asyncio
async def test_402_insufficient_credits_not_retried_and_blocked(env):
    brain, make, sleeps, _ = env
    srv = make([or_err(402, "Insufficient credits. Add more using https://openrouter.ai/credits")])
    with pytest.raises(BillingError) as ei:
        await ask(brain)
    e = ei.value
    assert e.status == 402 and "insufficient credits" in e.user_message.lower() and "openrouter.ai/credits" in e.user_message
    assert "Not retrying" in e.user_message and not sleeps and len(srv.chat_requests) == 1
    for _ in range(3):                                         # further questions must not hit the API
        with pytest.raises(BillingError):
            await ask(brain, "again")
    assert len(srv.chat_requests) == 1
    brain.reset(); assert await ask(brain, "after adding credits") and len(srv.chat_requests) == 2


@pytest.mark.asyncio
async def test_402_with_retry_after_is_a_temporary_budget_limit_and_retries(env):
    brain, make, sleeps, _ = env
    srv = make([("status", 402, {"Retry-After": "3"}, {"error": {"code": 402, "message": "In-flight budget", "metadata": {"limit_source": "openrouter_in_flight_budget"}}}), "ok"])
    assert await ask(brain)
    assert len(srv.chat_requests) == 2 and sleeps and sleeps[0] >= 3


@pytest.mark.asyncio
async def test_404_model_unavailable(env):
    brain, make, sleeps, _ = env
    srv = make(model="openai/not-a-real-model")
    with pytest.raises(ModelNotFoundError) as ei:
        await ask(brain)
    e = ei.value
    assert e.status == 404 and "provider/model" in e.user_message and "openai/not-a-real-model" in e.user_message
    assert len(srv.chat_requests) == 1 and not sleeps


@pytest.mark.asyncio
async def test_429_rate_limit_retries_with_backoff_then_succeeds(env):
    brain, make, sleeps, _ = env
    notes = []
    brain.on_retry = lambda err, d, n, t: notes.append((err.kind, n, t))
    rl = lambda: ("status", 429, {"Retry-After": "2"}, {"error": {"code": 429, "message": "Rate limit exceeded", "metadata": {"error_type": "rate_limit_exceeded"}}})
    srv = make([rl(), rl(), "ok"])
    assert await ask(brain) and len(srv.chat_requests) == 3 and len(sleeps) == 2 and sleeps[0] >= 2
    assert notes == [("rate_limit", 1, 3), ("rate_limit", 2, 3)]


@pytest.mark.asyncio
async def test_429_persistent_gives_up_but_does_not_lock_out(env):
    brain, make, sleeps, _ = env
    srv = make([or_err(429, "Rate limit exceeded")] * 9)
    with pytest.raises(RateLimitError) as ei:
        await ask(brain)
    assert len(srv.chat_requests) == 4 and "Gave up after 4 attempts" in ei.value.user_message
    srv.script = []; assert await ask(brain, "later")


@pytest.mark.asyncio
async def test_429_daily_free_limit_is_not_retried(env):
    brain, make, sleeps, _ = env
    srv = make([or_err(429, "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day")])
    with pytest.raises(RateLimitError) as ei:
        await ask(brain)
    assert len(srv.chat_requests) == 1 and not sleeps and "daily" in ei.value.user_message.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [502, 503, 408])
async def test_provider_down_or_timeout_retries_then_succeeds(env, code):
    brain, make, sleeps, _ = env
    srv = make([or_err(code, "Provider returned error"), or_err(code, "Provider returned error"), "ok"])
    assert await ask(brain) and len(srv.chat_requests) == 3 and len(sleeps) == 2


@pytest.mark.asyncio
async def test_403_moderation_affects_only_that_request(env):
    brain, make, sleeps, _ = env
    srv = make([or_err(403, "Input flagged by moderation", {"reasons": ["violence"], "flagged_input": "..."})])
    with pytest.raises(PolicyError) as ei:
        await ask(brain, "something flagged")
    assert "violence" in ei.value.user_message and not sleeps
    assert await ask(brain, "a normal question") and len(srv.chat_requests) == 2          # NOT blocked afterwards


@pytest.mark.asyncio
async def test_403_budget_limit_is_blocking(env):
    brain, make, sleeps, _ = env
    srv = make([or_err(403, "Budget limit exceeded (weekly limit).", {"limit_source": "openrouter_key_limit", "resets_at": "2026-12-01T00:00:00.000Z"})])
    with pytest.raises(BudgetError) as ei:
        await ask(brain)
    assert "2026-12-01" in ei.value.user_message
    with pytest.raises(BudgetError):
        await ask(brain, "again")
    assert len(srv.chat_requests) == 1


@pytest.mark.asyncio
async def test_midstream_error_after_tokens_is_not_retried(env):
    brain, make, sleeps, _ = env
    srv = make([("midstream", 3, {"code": 502, "message": "Provider disconnected", "metadata": {"error_type": "provider_unavailable"}})])
    got = []
    with pytest.raises(ServerError):
        async for d in brain.reply_stream("hi"):
            got.append(d)
    assert "".join(got).startswith("Hello there.") and len(srv.chat_requests) == 1 and not sleeps    # never repeats half an answer


@pytest.mark.asyncio
async def test_midstream_error_before_any_token_is_retried(env):
    brain, make, sleeps, _ = env
    srv = make([("midstream", 0, {"code": 429, "message": "Rate limit exceeded", "metadata": {"error_type": "rate_limit_exceeded"}}), "ok"])
    assert await ask(brain) and len(srv.chat_requests) == 2 and len(sleeps) == 1


@pytest.mark.asyncio
async def test_empty_200_response_is_retried_once(env):
    brain, make, sleeps, _ = env
    srv = make([("empty",), "ok"])
    assert await ask(brain) and len(srv.chat_requests) == 2
    srv2 = make([("empty",), ("empty",), "ok"])
    with pytest.raises(LufyError) as ei:
        await ask(brain)
    assert "empty" in ei.value.user_message.lower() and len(srv2.chat_requests) == 2


# ------------------------------------------------------------------ secrets
@pytest.mark.asyncio
async def test_key_never_in_logs_errors_or_ui(env, caplog):
    brain, make, sleeps, _ = env
    secret = "sk-or-v1-SUPERSECRETSUPERSECRET123456"
    make(key=secret)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AuthError) as ei:
        await ask(brain)
    fmt = RedactingFormatter("%(name)s %(message)s")
    text = "\n".join(fmt.format(r) for r in caplog.records) + ei.value.user_message + str(ei.value)
    assert "SUPERSECRET" not in text and "status=401" in text
    assert "SUPERSECRET" not in redact(f"Authorization: Bearer {secret} and {secret}")
    assert "SUPERSECRET" not in repr(brain) and "SUPERSECRET" not in brain.describe()
    assert brain.describe() == "OpenRouter · openai/gpt-4o-mini"


def test_settings_dialog_and_status_never_show_keys(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QLabel
    from core.assistant import Assistant
    from ui.main_window import SettingsDialog
    secret = "sk-or-v1-UISECRETUISECRETUISECRET99"
    monkeypatch.setattr("config.ENV.openai_api_key", secret); monkeypatch.setattr("config.ENV.gnani_api_key", "gnani-UISECRET-1234567890")
    QApplication.instance() or QApplication([])
    a = Assistant(Database(tmp_path / "ui.db"))
    seen = []
    a.status.connect(lambda *x: seen.append(" ".join(map(str, x))))
    a._emit_llm_status()
    dlg = SettingsDialog(a)
    texts = " ".join(l.text() for l in dlg.findChildren(QLabel)) + " ".join(seen)
    a.shutdown()
    assert "UISECRET" not in texts and "found" in texts and "OpenRouter" in texts.replace("openrouter", "OpenRouter")


# ------------------------------------------------------------------ Gnani unaffected
@pytest.mark.asyncio
async def test_gnani_stt_headers_independent_of_llm_settings(monkeypatch):
    from speech.stt import GnaniSTTClient
    monkeypatch.setattr("config.ENV.openai_api_key", "sk-or-v1-LLMKEY0000000000")
    monkeypatch.setattr("config.ENV.openai_base_url", "https://openrouter.ai/api/v1")
    h = GnaniSTTClient(api_key="gnani-key-123", language="en-IN").headers()
    assert h["x-api-key-id"] == "gnani-key-123" and "sk-or" not in " ".join(h.values())
    assert GnaniSTTClient(api_key="gnani-key-123").endpoint.startswith("wss://api.vachana.ai")


def test_gnani_tts_uses_only_gnani_key(monkeypatch):
    import json, threading
    from websockets.sync.server import serve
    got = {}

    def handler(ws):
        got["h"] = dict(ws.request.headers); got["req"] = json.loads(ws.recv())
        ws.send(json.dumps({"type": "complete", "data": {"audio": "", "is_final": True}}))
    with serve(handler, "127.0.0.1", 0) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setattr("speech.tts.ENV.tts_endpoint", f"ws://127.0.0.1:{srv.socket.getsockname()[1]}")
        monkeypatch.setattr("speech.tts.ENV.gnani_api_key", "gnani-key-123")
        monkeypatch.setattr("config.ENV.openai_api_key", "sk-or-v1-LLMKEY0000000000")
        from speech.tts import Speaker
        sp = Speaker({"tts_enabled": "1", "tts_model": "timbre-v3.0", "tts_voice": "Shivani", "tts_language": "auto", "tts_speed": "1", "tts_volume": "1"}.get)
        list(sp._gnani_stream("hello", sp._gen)); srv.shutdown(); sp.shutdown()
    assert got["h"]["x-api-key-id"] == "gnani-key-123" and "sk-or" not in json.dumps(got)


# ------------------------------------------------------------------ the shipped live-test tools, run against the mock
def _run_tool(script, srv, key=GOOD_KEY, extra_env=None):
    import tempfile
    env = {**os.environ, "OPENAI_API_KEY": key, "OPENAI_BASE_URL": srv.url, "OPENAI_MODEL": "openai/gpt-4o-mini", "LLM_VENDOR": "openrouter",
           "LUFY_DATA_DIR": tempfile.mkdtemp(), "LUFY_ENV_FILE": str(Path(tempfile.mkdtemp()) / "none.env"), **(extra_env or {})}
    return subprocess.run([sys.executable, str(ROOT / "tools" / script)], env=env, capture_output=True, text=True, timeout=90, cwd=ROOT)


def test_live_conversation_tool_passes_against_mock():
    srv = OpenRouterMock()
    out = _run_tool("test_openrouter_conversation.py", srv); srv.shutdown()
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Your favourite colour is teal." in out.stdout and "Real conversation works" in out.stdout
    assert GOOD_KEY not in out.stdout + out.stderr
    assert [r["body"]["messages"][-1]["role"] for r in srv.chat_requests] == ["user"] * 3          # 3 real requests, history grew each turn
    assert len(srv.chat_requests[2]["body"]["messages"]) == 6                                    # system + 2 full exchanges + new user turn


def test_live_conversation_tool_reports_credit_failure_clearly():
    srv = OpenRouterMock([or_err(402, "Insufficient credits")])
    out = _run_tool("test_openrouter_conversation.py", srv); srv.shutdown()
    assert out.returncode == 1 and "insufficient credits" in out.stdout.lower() and GOOD_KEY not in out.stdout + out.stderr


def test_diagnose_tool_healthy_path():
    srv = OpenRouterMock()
    out = _run_tool("diagnose_llm.py", srv); srv.shutdown()
    text = out.stdout
    assert out.returncode == 0, text + out.stderr
    assert "found (4 models listed)" in text and "key accepted" in text and "streaming works" in text and "healthy" in text
    assert GOOD_KEY not in text + out.stderr


def test_diagnose_tool_pinpoints_each_cause():
    cases = [("sk-proj-OpenAIKeyUsedByMistake1234567", [], "OpenAI-style key"),
             ("sk-or-v1-doesnotexistdoesnotexist00", [], "AUTH"),
             (GOOD_KEY, [or_err(402, "Insufficient credits")], "BILLING")]
    for key, script, expect in cases:
        srv = OpenRouterMock(script)
        out = _run_tool("diagnose_llm.py", srv, key=key); srv.shutdown()
        assert expect in out.stdout and out.returncode != 0, (expect, out.stdout)
        assert key not in out.stdout + out.stderr
    srv = OpenRouterMock()
    out = _run_tool("diagnose_llm.py", srv, extra_env={"OPENAI_MODEL": "openai/gpt-4o-minii"}); srv.shutdown()
    assert "NOT in the catalogue" in out.stdout and "openai/gpt-4o-mini" in out.stdout
