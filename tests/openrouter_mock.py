"""Local stand-in for https://openrouter.ai/api/v1 modelled on OpenRouter's documented behaviour:
Bearer auth, provider/model IDs, integer error codes {"error":{"code":402,...}}, SSE keep-alive comments,
a usage-only final chunk, mid-stream errors delivered with HTTP 200, and context-aware replies."""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
MODELS = ["openai/gpt-4o-mini", "openai/gpt-4o", "anthropic/claude-haiku-5.5", "meta-llama/llama-3.1-8b-instruct:free"]


def or_err(code, message, metadata=None):
    body = {"error": {"code": code, "message": message}}
    if metadata: body["error"]["metadata"] = metadata
    return ("status", code, {}, body)


def reply_for(messages):
    last = messages[-1]["content"].lower()
    earlier = " ".join(m["content"].lower() for m in messages[:-1])
    if "favourite colour" in last and "teal" in earlier:
        return "Your favourite colour is teal."
    if "hinglish" in last or "joke" in last:
        return "Ek aadmi doctor ke paas gaya. Doctor ne kaha, aaram karo."
    return "Hello there. I am a mock OpenRouter model."


class OpenRouterMock(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, script=None, key=GOOD_KEY):
        super().__init__(("127.0.0.1", 0), _H)
        self.script, self.key, self.requests = list(script or []), key, []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/api/v1"

    @property
    def chat_requests(self):
        return [r for r in self.requests if r["path"].endswith("/chat/completions")]


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, status, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer {self.server.key}"

    def do_GET(self):
        self.server.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": None})
        if self.path.endswith("/models"):
            return self._json(200, {"data": [{"id": m} for m in MODELS]})
        if self.path.endswith("/key"):
            if not self._authed(): return self._json(401, {"error": {"code": 401, "message": "No auth credentials found"}})
            return self._json(200, {"data": {"label": f"{self.server.key[:12]}...{self.server.key[-3:]}", "usage": 0.42,
                                             "limit": None, "limit_remaining": None, "is_free_tier": False}})
        self._json(404, {"error": {"code": 404, "message": "not found"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.server.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if not self._authed():
            return self._json(401, {"error": {"code": 401, "message": "No auth credentials found"}})
        if body.get("model") not in MODELS:
            return self._json(404, {"error": {"code": 404, "message": f"No endpoints found for {body.get('model')}."}})
        item = self.server.script.pop(0) if self.server.script else "ok"
        if item != "ok" and item[0] == "status":
            _, code, headers, payload = item
            return self._json(code, payload, headers)
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        w = lambda obj: self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode()) or self.wfile.flush()
        chunk = lambda delta, fin=None, **kw: {"id": "gen-1", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                                              "provider": "OpenAI", "choices": [{"index": 0, "delta": delta, "finish_reason": fin}], **kw}
        self.wfile.write(b": OPENROUTER PROCESSING\n\n")                      # keep-alive comment
        if item != "ok" and item[0] == "empty":
            w(chunk({"role": "assistant", "content": ""})); w(chunk({}, "stop")); self.wfile.write(b"data: [DONE]\n\n"); return
        words = reply_for(body["messages"]).split(" ")
        w(chunk({"role": "assistant", "content": ""}))
        cut = item[1] if item != "ok" and item[0] == "midstream" else None
        for n, word in enumerate(words):
            if cut is not None and n == cut:
                w({"id": "gen-1", "object": "chat.completion.chunk", "created": 1, "model": body["model"], "provider": "OpenAI",
                   "error": item[2], "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}]})
                return
            w(chunk({"content": word + (" " if n < len(words) - 1 else "")}))
        w(chunk({}, "stop"))
        w({"id": "gen-1", "object": "chat.completion.chunk", "created": 1, "model": body["model"], "choices": [],
           "usage": {"prompt_tokens": 20, "completion_tokens": len(words), "total_tokens": 20 + len(words)}})
        self.wfile.write(b"data: [DONE]\n\n")
