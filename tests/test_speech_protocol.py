"""Protocol tests: run my STT/TTS clients against a local mock server that follows the Gnani docs
(headers, 1024-byte binary frames, JSON messages). This verifies OUR client logic - it does not prove the
real Gnani service accepts our requests; that needs a live key (see README)."""
import asyncio, base64, json, threading
import pytest
import websockets
from websockets.asyncio.server import serve
from websockets.sync.server import serve as sync_serve

from speech.stt import GnaniSTTClient
from speech.tts import Speaker, clean_for_speech


@pytest.mark.asyncio
async def test_stt_headers_framing_and_transcripts():
    seen = {"headers": None, "frames": []}

    async def handler(ws):
        seen["headers"] = dict(ws.request.headers)
        await ws.send(json.dumps({"type": "connected", "message": "ready", "config": {"sample_rate": 16000, "chunk_size": 512}}))
        async for msg in ws:
            seen["frames"].append(msg)
            if len(seen["frames"]) == 3:
                await ws.send(json.dumps({"type": "processing"}))
                await ws.send(json.dumps({"type": "transcript", "text": "hello lufy", "segment_id": "s1", "segment_index": 0, "latency": 10}))
                await ws.send(json.dumps({"type": "error", "message": "recoverable hiccup"}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        c = GnaniSTTClient(language="hi-IN", endpoint=f"ws://127.0.0.1:{port}", api_key="k123", vad_threshold=0.55, min_silence_ms=700)
        got, errs, statuses = [], [], []
        c.on_transcript = lambda t, raw: got.append((t, raw["segment_id"]))
        c.on_error = errs.append
        c.on_status = lambda s, d: statuses.append(s)
        c.start()
        for _ in range(100):                               # client drops audio queued before connect (by design)
            if "connected" in statuses: break
            await asyncio.sleep(0.02)
        for _ in range(3):
            c.feed(b"\x01\x00" * 512)
            await asyncio.sleep(0.03)
        c.feed(b"short")                                   # wrong size must be dropped
        for _ in range(50):
            if got: break
            await asyncio.sleep(0.05)
        await c.stop()
    h = seen["headers"]
    assert h["x-api-key-id"] == "k123" and h["lang_code"] == "hi-IN" and h["x-sample-rate"] == "16000"
    assert h["x-vad-threshold"] == "0.55" and h["x-min-silence-ms"] == "700"
    assert all(len(f) == 1024 for f in seen["frames"]) and len(seen["frames"]) == 3
    assert got == [("hello lufy", "s1")] and "connected" in statuses
    assert any("recoverable hiccup" in e for e in errs)


@pytest.mark.asyncio
async def test_stt_auth_failure_is_fatal_and_not_retried():
    attempts = []
    from websockets.exceptions import InvalidStatus  # noqa

    async def process_request(connection, request):
        attempts.append(1)
        return connection.respond(401, "unauthorized\n")

    async with serve(lambda ws: None, "127.0.0.1", 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        c = GnaniSTTClient(endpoint=f"ws://127.0.0.1:{port}", api_key="bad")
        errs = []
        c.on_error = errs.append
        c.start()
        await asyncio.sleep(1.5)
        await c.stop()
    assert len(attempts) == 1                       # no retry storm with a bad key
    assert errs and "key was rejected" in errs[0].lower() and "401" in errs[0]


@pytest.mark.asyncio
async def test_stt_missing_key_reports_clearly():
    c = GnaniSTTClient(api_key="")
    errs = []
    c.on_error = errs.append
    c.start()
    assert errs and "GNANI_API_KEY" in errs[0]


def test_tts_request_format_and_chunk_assembly(monkeypatch):
    got = {}

    def handler(ws):
        got["headers"] = dict(ws.request.headers)
        got["req"] = json.loads(ws.recv())
        ws.send(json.dumps({"type": "start", "request_id": "r"}))
        ws.send(json.dumps({"type": "audio", "data": {"chunk_index": 1, "audio": base64.b64encode(b"\x01\x00\x02").decode(), "is_final": False}}))
        ws.send(json.dumps({"type": "audio", "data": {"chunk_index": 2, "audio": base64.b64encode(b"\x00\x03\x00").decode(), "is_final": False}}))
        ws.send(json.dumps({"type": "complete", "data": {"chunk_index": 3, "audio": "", "is_final": True}}))

    with sync_serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.setattr("speech.tts.ENV.tts_endpoint", f"ws://127.0.0.1:{port}")
        monkeypatch.setattr("speech.tts.ENV.gnani_api_key", "k123")
        settings = {"tts_enabled": "1", "tts_model": "timbre-v3.0", "tts_voice": "Nalini",   # wrong-model voice on purpose
                    "tts_language": "auto", "tts_speed": "9", "tts_volume": "1"}
        sp = Speaker(settings.get)
        chunks = list(sp._gnani_stream("नमस्ते", sp._gen))
        server.shutdown()
    r = got["req"]
    assert got["headers"]["x-api-key-id"] == "k123"
    assert r["model"] == "timbre-v3.0" and r["voice"] == "Shivani"      # auto-corrected: v2.5 voice not valid on v3.0
    assert r["speed"] == 1.15                                          # clamped to documented range
    assert r["audio_config"]["container"] == "raw" and r["audio_config"]["encoding"] == "linear_pcm"
    assert b"".join(chunks) == b"\x01\x00\x02\x00\x03\x00"             # odd-byte chunk boundary re-aligned to int16
    sp.shutdown()


def test_clean_for_speech():
    t = clean_for_speech("**Hi** there! See [docs](https://x.y) and `code`.\n- item\n```py\nprint(1)\n```")
    assert "*" not in t and "http" not in t and "print" not in t and "docs" in t
