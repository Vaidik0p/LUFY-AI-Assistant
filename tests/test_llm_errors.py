"""Run the real OpenAI SDK + LUFY's brain against a local mock that returns genuine OpenAI error shapes."""
import json, logging, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import core.ai_brain as ai
from core.ai_brain import AIBrain
from core.errors import AuthError, BillingError, LufyError, ModelNotFoundError, RateLimitError, ServerError, redact
from core.memory import Memory
from database.database import Database

KEY = "sk-proj-SUPERSECRETKEY1234567890abcdef"


def err_body(msg, typ, code):
    return {"error": {"message": msg, "type": typ, "param": None, "code": code}}


E401 = (401, {}, err_body(f"Incorrect API key provided: {KEY}.", "invalid_request_error", "invalid_api_key"))
E404 = (404, {}, err_body("The model `gpt-4o-minii` does not exist or you do not have access to it.", "invalid_request_error", "model_not_found"))
E429Q = (429, {}, err_body("You exceeded your current quota, please check your plan and billing details.", "insufficient_quota", "insufficient_quota"))
E429R = (429, {"retry-after": "2", "x-request-id": "req_rl"}, err_body("Rate limit reached for gpt-4o-mini: Limit 3, Used 3, Requested 1.", "requests", "rate_limit_exceeded"))
E503 = (503, {}, err_body("The server is overloaded.", "server_error", None))
OKRESP = "ok"


class Mock(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, script):
        super().__init__(("127.0.0.1", 0), Handler)
        self.script, self.hits, self.auth = list(script), 0, []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, status, headers, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in headers.items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):                                   # /v1/models/<id>
        self.server.hits += 1
        item = self.server.script.pop(0) if self.server.script else (200, {}, {"id": "gpt-4o-mini", "object": "model", "created": 1, "owned_by": "openai"})
        self._send(*item) if item != OKRESP else self._send(200, {}, {"id": "gpt-4o-mini", "object": "model", "created": 1, "owned_by": "openai"})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.hits += 1
        self.server.auth.append(self.headers.get("Authorization"))
        item = self.server.script.pop(0) if self.server.script else OKRESP
        if item != OKRESP:
            return self._send(*item)
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for piece in ("Hello", " there."):
            chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o-mini",
                     "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    sleeps = []

    async def fake_sleep(d): sleeps.append(d)
    monkeypatch.setattr(ai, "_sleep", fake_sleep)
    mem = Memory(Database(tmp_path / "x.db"))
    mem.set("llm_provider", "openai")
    brain = AIBrain(mem)
    servers = []

    def make(script):
        srv = Mock(script); servers.append(srv)
        monkeypatch.setattr("config.ENV.openai_api_key", KEY)
        monkeypatch.setattr("config.ENV.openai_base_url", srv.url)
        brain.reset()
        return srv
    yield brain, make, sleeps
    for s in servers: s.shutdown()


async def collect(brain, text="hi"):
    return "".join([d async for d in brain.reply_stream(text)])


@pytest.mark.asyncio
async def test_success_path_streams(setup):
    brain, make, sleeps = setup
    srv = make([])
    assert await collect(brain) == "Hello there."
    assert srv.hits == 1 and srv.auth == [f"Bearer {KEY}"]


@pytest.mark.asyncio
async def test_401_invalid_key_single_request_clear_message(setup):
    brain, make, sleeps = setup
    srv = make([E401])
    with pytest.raises(AuthError) as ei:
        await collect(brain)
    e = ei.value
    assert srv.hits == 1 and not sleeps                       # no retry
    assert e.status == 401 and e.code == "invalid_api_key" and "key was rejected" in e.user_message
    assert KEY not in e.user_message and "SUPERSECRET" not in str(e)   # server echoed the key; we scrubbed it


@pytest.mark.asyncio
async def test_404_model_not_found(setup):
    brain, make, sleeps = setup
    srv = make([E404])
    with pytest.raises(ModelNotFoundError) as ei:
        await collect(brain)
    assert srv.hits == 1 and ei.value.code == "model_not_found" and "OPENAI_MODEL" in ei.value.user_message


@pytest.mark.asyncio
async def test_429_insufficient_quota_is_billing_not_rate_limit_and_not_retried(setup):
    brain, make, sleeps = setup
    srv = make([E429Q])
    with pytest.raises(BillingError) as ei:
        await collect(brain)
    e = ei.value
    assert srv.hits == 1 and not sleeps                       # SDK hidden retry is gone; we don't retry billing
    assert e.status == 429 and e.code == "insufficient_quota"
    assert "billing" in e.user_message.lower() and "not a temporary limit" in e.user_message
    # circuit breaker: the next questions make NO HTTP request at all
    for _ in range(3):
        with pytest.raises(BillingError):
            await collect(brain, "another question")
    assert srv.hits == 1
    # saving settings (after the user adds credit) clears the block
    brain.reset()
    srv.script = []
    assert await collect(brain, "now it works") == "Hello there." and srv.hits == 2


@pytest.mark.asyncio
async def test_429_rate_limit_retries_with_exponential_backoff_then_succeeds(setup):
    brain, make, sleeps = setup
    retries = []
    brain.on_retry = lambda err, d, n, t: retries.append((err.kind, n, t))
    srv = make([E429R, (429, {}, err_body("Rate limit", "requests", "rate_limit_exceeded")), OKRESP])
    assert await collect(brain) == "Hello there."
    assert srv.hits == 3 and len(sleeps) == 2
    assert sleeps[0] >= 2.0                                   # honoured Retry-After: 2
    assert sleeps[1] >= 2.0 and sleeps[1] > 1.0               # grows (1s -> 2s base)
    assert retries == [("rate_limit", 1, 3), ("rate_limit", 2, 3)]


@pytest.mark.asyncio
async def test_persistent_rate_limit_gives_up_after_bounded_retries(setup):
    brain, make, sleeps = setup
    srv = make([E429R] * 10)
    with pytest.raises(RateLimitError) as ei:
        await collect(brain)
    assert srv.hits == 4 and len(sleeps) == 3                 # 1 try + 3 retries, no more
    assert "Gave up after 4 attempts" in ei.value.user_message
    srv.script = []                                           # a transient limit must NOT lock the app out
    assert await collect(brain, "try again") == "Hello there."


@pytest.mark.asyncio
async def test_503_retries_then_succeeds(setup):
    brain, make, sleeps = setup
    srv = make([E503, E503, OKRESP])
    assert await collect(brain) == "Hello there." and srv.hits == 3 and len(sleeps) == 2


@pytest.mark.asyncio
async def test_persistent_500_is_server_error(setup):
    brain, make, sleeps = setup
    srv = make([(500, {}, err_body("boom", "server_error", None))] * 6)
    with pytest.raises(ServerError):
        await collect(brain)
    assert srv.hits == 4


@pytest.mark.asyncio
async def test_connection_refused_is_network_error(setup, monkeypatch):
    brain, make, sleeps = setup
    make([])
    monkeypatch.setattr("config.ENV.openai_base_url", "http://127.0.0.1:1/v1")
    brain.reset()
    with pytest.raises(LufyError) as ei:
        await collect(brain)
    assert ei.value.kind == "network" and len(sleeps) == 2    # network errors: 2 retries


@pytest.mark.asyncio
async def test_secrets_never_reach_logs(setup, caplog):
    brain, make, sleeps = setup
    from core.errors import RedactingFormatter
    make([E401])
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AuthError):
        await collect(brain)
    fmt = RedactingFormatter("%(name)s %(levelname)s %(message)s")
    text = "\n".join(fmt.format(r) for r in caplog.records)
    assert "LLM request failed" in text and "status=401" in text and "request_id" in text
    assert KEY not in text and "SUPERSECRET" not in text


def test_redact_patterns():
    s = redact("Authorization: Bearer abcdefghij1234 key sk-proj-abcdefghijk123 vach_ABCDEFGH12345 api_key=zzzzzzzzzz1111")
    assert "abcdefghij1234" not in s and "sk-proj" not in s and "vach_ABC" not in s and "zzzzzzzzzz1111" not in s


def test_diagnose_script_reports_real_cause_without_leaking_key(tmp_path):
    srv = Mock([E401, E429Q]) if False else Mock([(200, {}, {"id": "gpt-4o-mini", "object": "model", "created": 1, "owned_by": "x"}), E429Q])
    import os
    env = {**os.environ, "OPENAI_API_KEY": KEY, "OPENAI_BASE_URL": srv.url, "LUFY_DATA_DIR": str(tmp_path), "OPENAI_MODEL": "gpt-4o-mini"}
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, str(root / "tools" / "diagnose_llm.py")], env=env, capture_output=True, text=True, timeout=60)
    srv.shutdown()
    text = out.stdout + out.stderr
    assert "insufficient_quota" in text and "HTTP status : 429" in text and "BILLING" in text
    assert "OPENAI_API_KEY loaded" in text and KEY not in text and "SUPERSECRET" not in text
    assert out.returncode == 1
