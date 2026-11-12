"""Gnani Prisma realtime STT client (WebSocket) - protocol per https://docs.gnani.ai/api/STT/stt-websocket.md

* Auth + config are WebSocket upgrade headers (x-api-key-id, lang_code, x-sample-rate, VAD headers).
* Audio: binary frames of exactly 1024 bytes (512 samples PCM s16le mono) at real-time cadence.
* Server JSON: connected / processing / transcript / error / settings_updated.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import websockets

from config import ENV, STT_FRAME_BYTES, STT_SAMPLE_RATE
from core.errors import AuthError, LufyError, NetworkError, classify

log = logging.getLogger("lufy.stt")


class GnaniSTTClient:
    def __init__(self, *, language: str = "en-IN", vad_threshold: float = 0.6, min_silence_ms: int = 600,
                 endpoint: Optional[str] = None, api_key: Optional[str] = None):
        self.endpoint = endpoint or ENV.stt_endpoint
        self.api_key = api_key if api_key is not None else ENV.gnani_api_key
        self.language = language
        self.vad_threshold = vad_threshold
        self.min_silence_ms = min_silence_ms
        # callbacks (called on the asyncio loop thread)
        self.on_transcript: Callable[[str, dict], None] = lambda text, raw: None
        self.on_processing: Callable[[], None] = lambda: None
        self.on_status: Callable[[str, str], None] = lambda state, detail: None   # state: connecting|connected|error|closed
        self.on_error: Callable[[str], None] = lambda msg: None
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=200)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ---- public API (call from the loop thread) ----
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not self.api_key:
            self.on_status("error", "no key")
            self.on_error("Gnani API key missing. Add GNANI_API_KEY to your .env file.")
            return
        self._stop.clear()
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def restart(self, **changes) -> None:
        for k, v in changes.items():
            setattr(self, k, v)
        await self.stop()
        self.start()

    @property
    def active(self) -> bool:
        return bool(self._task and not self._task.done())

    def feed(self, frame: bytes) -> None:
        """Queue one 1024-byte frame. Drops the oldest audio instead of bursting if we fall behind."""
        if len(frame) != STT_FRAME_BYTES:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(frame)

    def headers(self) -> dict[str, str]:
        return {
            "x-api-key-id": self.api_key,
            "lang_code": self.language,
            "x-sample-rate": str(STT_SAMPLE_RATE),
            "x-format": "verbatim",
            "x-vad-threshold": str(self.vad_threshold),
            "x-min-silence-ms": str(self.min_silence_ms),
            "x-min-speech-ms": "150",
        }

    # ---- internals ----
    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self.on_status("connecting", "")
            try:
                async with websockets.connect(self.endpoint, additional_headers=self.headers(),
                                              open_timeout=10, ping_interval=20, max_size=2 ** 20) as ws:
                    backoff = 1.0
                    self._drain()
                    sender = asyncio.ensure_future(self._send_loop(ws))
                    try:
                        await self._recv_loop(ws)
                    finally:
                        sender.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                err = classify(e, "Speech recognition")
                self.on_status("error", err.user_message)
                self.on_error(err.user_message)
                if isinstance(err, AuthError) or err.fatal:
                    return  # retrying with a bad key just burns requests
                log.warning("STT connection lost: %r", e)
            if self._stop.is_set():
                break
            self.on_status("connecting", f"retrying in {backoff:.0f}s")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 15.0)
        self.on_status("closed", "")

    def _drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    async def _send_loop(self, ws) -> None:
        while True:
            frame = await self._queue.get()
            await ws.send(frame)

    async def _recv_loop(self, ws) -> None:
        import json
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "connected":
                self.on_status("connected", msg.get("message", ""))
            elif kind == "processing":
                self.on_processing()
            elif kind == "transcript":
                text = (msg.get("text") or "").strip()
                if text:
                    self.on_transcript(text, msg)
            elif kind == "error":
                m = msg.get("message", "unknown error")
                low = m.lower()
                if "auth" in low or "api key" in low or "unauthor" in low:
                    raise AuthError("Speech recognition: authentication failed - check GNANI_API_KEY.", fatal=True)
                self.on_error(f"Speech recognition error: {m}")
