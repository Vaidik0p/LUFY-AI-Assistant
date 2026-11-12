"""Text-to-speech: Gnani Timbre realtime WebSocket (primary) + pyttsx3/SAPI (offline fallback).

Gnani protocol (https://docs.gnani.ai/api/TTS/tts-websocket.md): connect with header X-API-Key-ID, send one JSON
request {text, voice, model, language, speed, audio_config}, receive JSON {type:"audio", data:{audio:<b64>, is_final}}
chunks followed by {type:"complete"}. We request container=raw / linear_pcm so chunks are plain PCM we can play
immediately and cut instantly on interruption.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import tempfile
import threading
import wave
from typing import Callable, Optional

import numpy as np
from websockets.sync.client import connect as ws_connect

from config import ENV, TTS_VOICES
from core.errors import AuthError, classify

log = logging.getLogger("lufy.tts")
TTS_SAMPLE_RATE = 24000


def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " code omitted. ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#>~|]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"https?://\S+", "link", text)
    return re.sub(r"\s+", " ", text).strip()


class Speaker:
    """Queue of sentences -> audio. Thread-based; `stop()` cuts audio within ~100 ms."""

    def __init__(self, settings: Callable[[str], str]):
        self._get = settings
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stop_evt = threading.Event()
        self._gen = 0
        self._lock = threading.Lock()
        self._out = None
        self._ws = None
        self._busy = threading.Event()
        self._thread = threading.Thread(target=self._worker, name="lufy-tts", daemon=True)
        self._thread.start()
        self._spoken_recent = ""          # used by the assistant to ignore its own echo
        self.on_start: Callable[[str], None] = lambda text: None       # sentence about to be spoken (subtitle)
        self.on_idle: Callable[[], None] = lambda: None
        self.on_error: Callable[[str], None] = lambda msg: None
        self.on_engine: Callable[[str], None] = lambda name: None      # "gnani" | "fallback"
        self._gnani_disabled_reason: Optional[str] = None

    # ---- public ----
    @property
    def speaking(self) -> bool:
        return self._busy.is_set() or not self._q.empty()

    @property
    def recent_text(self) -> str:
        return self._spoken_recent

    def say(self, text: str) -> None:
        text = clean_for_speech(text)
        if not text or self._get("tts_enabled") != "1":
            return
        for part in self._split(text):
            self._q.put((self._gen, part))

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self._stop_evt.set()
            while not self._q.empty():
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        out = self._out
        if out is not None:
            try:
                out.abort()
            except Exception:
                pass

    def shutdown(self) -> None:
        self.stop()
        self._q.put(None)

    # ---- internals ----
    @staticmethod
    def _split(text: str, limit: int = 220) -> list[str]:
        sentences = re.split(r"(?<=[.!?\u0964])\s+", text)
        out, cur = [], ""
        for s in sentences:
            if len(cur) + len(s) + 1 <= limit:
                cur = f"{cur} {s}".strip()
            else:
                if cur:
                    out.append(cur)
                cur = s
        if cur:
            out.append(cur)
        return out

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            gen, text = item
            if gen != self._gen:
                continue
            self._stop_evt.clear()
            self._busy.set()
            try:
                self._spoken_recent = (self._spoken_recent + " " + text)[-400:]
                self.on_start(text)
                pcm_iter = self._synthesize(text, gen)
                self._play(pcm_iter, gen)
            except Exception as e:  # never kill the worker
                if gen == self._gen:     # errors caused by an intentional interrupt are expected
                    log.exception("TTS failure")
                    self.on_error(f"Voice output failed: {e}")
            finally:
                self._busy.clear()
                if self._q.empty():
                    self.on_idle()

    def _synthesize(self, text: str, gen: int):
        """Yield (pcm_bytes, sample_rate) chunks; falls back to the local engine if Gnani is unavailable."""
        if ENV.gnani_api_key and not self._gnani_disabled_reason:
            try:
                got_audio = False
                for chunk in self._gnani_stream(text, gen):
                    got_audio = True
                    yield chunk, TTS_SAMPLE_RATE
                if got_audio or gen != self._gen:
                    self.on_engine("gnani")
                    return
            except AuthError as e:
                self._gnani_disabled_reason = e.user_message
                self.on_error(e.user_message + " Using the offline voice instead.")
            except Exception as e:
                if gen != self._gen:
                    return
                err = classify(e, "Voice output")
                self.on_error(err.user_message + " Using the offline voice for now.")
        if gen != self._gen:
            return
        self.on_engine("fallback")
        yield from self._fallback_stream(text)

    def _gnani_stream(self, text: str, gen: int):
        model = self._get("tts_model")
        voice = self._get("tts_voice")
        if voice not in TTS_VOICES.get(model, []):      # voices are not shared between models (docs)
            voice = TTS_VOICES[model][0]
        speed = max(0.85, min(1.15, float(self._get("tts_speed") or 1.0)))
        req = {
            "text": text, "voice": voice, "model": model,
            "language": self._get("tts_language") or "auto", "speed": speed,
            "audio_config": {"sample_rate": TTS_SAMPLE_RATE, "num_channels": 1, "sample_width": 2,
                             "encoding": "linear_pcm", "container": "raw"},
        }
        try:
            ws = ws_connect(ENV.tts_endpoint, additional_headers={"X-API-Key-ID": ENV.gnani_api_key},
                            open_timeout=8, max_size=2 ** 22)
        except Exception as e:
            err = classify(e, "Voice output")
            raise err
        self._ws = ws
        try:
            ws.send(json.dumps(req))
            leftover = b""
            for raw in ws:
                if gen != self._gen:
                    return
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "audio":
                    data = msg.get("data", {})
                    b = base64.b64decode(data.get("audio") or "")
                    if b:
                        b = leftover + b
                        if len(b) % 2:
                            leftover, b = b[-1:], b[:-1]
                        else:
                            leftover = b""
                        if b:
                            yield b
                    if data.get("is_final"):
                        return
                elif kind == "complete":
                    return
                elif kind == "error":
                    m = msg.get("message", "synthesis failed")
                    if "auth" in m.lower() or "api key" in m.lower():
                        raise AuthError("Voice output: authentication failed - check GNANI_API_KEY.", fatal=True)
                    raise RuntimeError(m)
        finally:
            self._ws = None
            try:
                ws.close()
            except Exception:
                pass

    def _fallback_stream(self, text: str):
        try:
            import pyttsx3
        except ImportError:
            self.on_error("Offline voice needs 'pyttsx3' (pip install pyttsx3).")
            return
        path = os.path.join(tempfile.gettempdir(), f"lufy_tts_{os.getpid()}.wav")
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", int(190 * float(self._get("tts_speed") or 1.0)))
            engine.save_to_file(text, path)
            engine.runAndWait()
            with wave.open(path, "rb") as w:
                rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
                data = w.readframes(w.getnframes())
            if width != 2:
                return
            if ch == 2:
                data = np.frombuffer(data, dtype=np.int16).reshape(-1, 2).mean(axis=1).astype(np.int16).tobytes()
            step = rate  # ~0.5 s chunks (rate samples * 2 bytes)
            for i in range(0, len(data), step):
                yield data[i:i + step], rate
        except Exception as e:
            self.on_error(f"Offline voice failed: {e}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _play(self, chunks, gen: int) -> None:
        import sounddevice as sd
        vol = max(0.0, min(1.0, float(self._get("tts_volume") or 1.0)))
        stream, rate = None, None
        try:
            for pcm, sr in chunks:
                if gen != self._gen:
                    break
                if stream is None or sr != rate:
                    if stream is not None:
                        stream.stop(); stream.close()
                    rate = sr
                    stream = sd.RawOutputStream(samplerate=sr, channels=1, dtype="int16")
                    self._out = stream
                    stream.start()
                if vol < 0.999:
                    pcm = (np.frombuffer(pcm, dtype=np.int16) * vol).astype(np.int16).tobytes()
                # write in small slices so stop() can interrupt quickly
                step = sr // 10 * 2
                for i in range(0, len(pcm), step):
                    if gen != self._gen:
                        return
                    stream.write(pcm[i:i + step])
        finally:
            if stream is not None:
                try:
                    if gen == self._gen:
                        stream.stop()   # drain what was written
                    stream.close()
                except Exception:
                    pass
            self._out = None
