"""LUFY orchestrator: microphone -> STT -> wake word -> router / LLM -> TTS, plus state for the UI.

Threads: Qt main thread (UI) | PortAudio callback (mic) | asyncio loop thread (STT websocket + LLM) |
TTS worker thread (audio out) | executor threads (blocking OS automation).
All communication to the UI is through Qt signals (thread-safe, queued).
"""
from __future__ import annotations

import asyncio
import collections
import difflib
import logging
import re
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal

from config import DB_PATH, ENV
from core import command_router
from core.ai_brain import AIBrain
from core.command_router import CommandRouter
from core.errors import LufyError, classify
from core.memory import Memory
from database.database import Database
from speech import wake_word
from speech.microphone import Microphone
from speech.stt import GnaniSTTClient
from speech.tts import Speaker

log = logging.getLogger("lufy.assistant")

IDLE, LISTENING, THINKING, SPEAKING, ERROR = "idle", "listening", "thinking", "speaking", "error"
SENTENCE_END = re.compile(r"(?<=[.!?\u0964])\s+")


def _n(s: str) -> str:
    return re.sub(r"[^\w\u0900-\u097F ]", "", s.lower()).strip()


class Assistant(QObject):
    state_changed = Signal(str)
    level = Signal(float)
    heard = Signal(str)                 # every recognized segment (live caption)
    chat_message = Signal(str, str)     # role ("user"/"assistant"), text   (assistant text may grow: see assistant_delta)
    assistant_delta = Signal(str)       # streaming text of the current reply
    assistant_done = Signal(str)
    subtitle = Signal(str)
    status = Signal(str, str, str)      # component (stt|llm|tts|mic), state (ok|busy|error|off), detail
    error = Signal(str)
    confirm_request = Signal(str)       # prompt shown with Yes/No buttons
    confirm_cleared = Signal()
    muted_changed = Signal(bool)
    memory_changed = Signal()

    def __init__(self, db: Optional[Database] = None):
        super().__init__()
        self.db = db or Database(DB_PATH)
        self.memory = Memory(self.db)
        self.router = CommandRouter(self.memory)
        self.brain = AIBrain(self.memory)
        self.brain.on_retry = lambda err, delay, n, total: self.status.emit(
            "llm", "busy", f"{err.kind.replace('_', ' ')} - retry {n}/{total} in {delay:.0f}s")
        self._turn_key = ""
        self._turn_started = 0.0
        self._last_submit: tuple[str, float] = ("", 0.0)
        self.speaker = Speaker(self.memory.get)
        self.speaker.on_start = self._on_speak_start
        self.speaker.on_idle = self._on_speak_idle
        self.speaker.on_error = lambda m: (self.error.emit(m), self.status.emit("tts", "error", m))
        self.speaker.on_engine = lambda e: self.status.emit("tts", "ok", "Gnani Timbre" if e == "gnani" else "offline voice")

        self.state = IDLE
        self.muted = False
        self._ptt_until = 0.0
        self._awake_until = 0.0
        self._last_speak_end = 0.0
        self._generating = False
        self._turn: Optional[asyncio.Task] = None
        self._recent: collections.deque = collections.deque(maxlen=20)   # (norm text, time, segment_id)
        self._preroll: collections.deque = collections.deque(maxlen=16)
        self._gate_until = 0.0
        self._loud_frames = 0

        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="lufy-loop", daemon=True)
        self._thread.start()
        self.stt: Optional[GnaniSTTClient] = None
        self.mic = Microphone(self._on_frame, self._mic_device())

    # ------------------------------------------------------------------ lifecycle
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _call(self, fn, *a) -> None:
        self.loop.call_soon_threadsafe(fn, *a)

    def start(self) -> None:
        self._call(self._start_stt)
        try:
            self.mic.start()
            self.status.emit("mic", "ok", "listening")
        except LufyError as e:
            self.status.emit("mic", "error", e.user_message)
            self.error.emit(e.user_message)
            self._set_state(ERROR)
        self._emit_llm_status()
        if not ENV.gnani_api_key:
            self.status.emit("tts", "off", "no Gnani key - offline voice")

    def shutdown(self) -> None:
        self.mic.stop()
        self.speaker.shutdown()
        if self.stt:
            fut = asyncio.run_coroutine_threadsafe(self.stt.stop(), self.loop)
            try:
                fut.result(timeout=2)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.db.close()

    def _start_stt(self) -> None:
        self.stt = GnaniSTTClient(language=self.memory.get("language"),
                                  vad_threshold=self.memory.get_float("vad_threshold", 0.6),
                                  min_silence_ms=int(self.memory.get_float("min_silence_ms", 600)))
        self.stt.on_transcript = self._on_transcript
        self.stt.on_processing = lambda: None
        self.stt.on_status = lambda s, d: self.status.emit(
            "stt", {"connected": "ok", "connecting": "busy", "error": "error", "closed": "off"}.get(s, "off"), d)
        self.stt.on_error = lambda m: self._report_error(m)
        self.stt.start()

    def apply_settings(self) -> None:
        """Call after the settings dialog saves."""
        self.brain.reset()
        self.mic.device = self._mic_device()
        if self.mic.running:
            self.mic.stop()
            try:
                self.mic.start()
                self.status.emit("mic", "ok", "listening")
            except LufyError as e:
                self.status.emit("mic", "error", e.user_message)
                self._report_error(e.user_message)
        if self.stt:
            asyncio.run_coroutine_threadsafe(self.stt.restart(
                language=self.memory.get("language"),
                vad_threshold=self.memory.get_float("vad_threshold", 0.6),
                min_silence_ms=int(self.memory.get_float("min_silence_ms", 600))), self.loop)
        self._emit_llm_status()

    def _emit_llm_status(self) -> None:
        ok = self.brain.configured()
        self.status.emit("llm", "ok" if ok else "error", self.brain.describe() if ok else "no API key (OPENAI_API_KEY in .env)")

    def _mic_device(self) -> Optional[int]:
        v = self.memory.get("mic_device")
        return int(v) if v.strip().isdigit() else None

    # ------------------------------------------------------------------ state helpers
    def _set_state(self, s: str) -> None:
        if s != self.state:
            self.state = s
            self.state_changed.emit(s)

    def _report_error(self, msg: str) -> None:
        self.error.emit(msg)

    # ------------------------------------------------------------------ mic path (audio thread)
    def _on_frame(self, frame: bytes, level: float) -> None:
        self.level.emit(level)
        now = time.time()
        # Voice barge-in: sustained loud input while LUFY is talking cuts speech immediately.
        if self.speaker.speaking and self.memory.get_bool("barge_in"):
            self._loud_frames = self._loud_frames + 1 if level > self.memory.get_float("barge_in_level", 0.10) else 0
            if self._loud_frames >= 8:       # ~256 ms
                self._loud_frames = 0
                self.speaker.stop()
        else:
            self._loud_frames = 0
        if self.memory.get("mode") == "push_to_talk":
            if now > self._ptt_until:
                return
        elif self.memory.get_bool("stream_gate"):
            if level > 0.03:
                if now > self._gate_until:          # gate opening: send pre-roll so first word isn't clipped
                    for f in self._preroll:
                        self._call(self._feed, f)
                    self._preroll.clear()
                self._gate_until = now + 1.5
            elif now > self._gate_until:
                self._preroll.append(frame)
                return
        self._call(self._feed, frame)

    def _feed(self, frame: bytes) -> None:
        if self.stt:
            self.stt.feed(frame)

    # ------------------------------------------------------------------ STT path (loop thread)
    def _is_echo(self, text: str) -> bool:
        if not (self.speaker.speaking or time.time() - self._last_speak_end < 2.0):
            return False
        a, b = _n(text), _n(self.speaker.recent_text)
        if not a:
            return True
        return a in b or difflib.SequenceMatcher(None, a, b[-len(a) * 2:]).ratio() > 0.6

    def _on_transcript(self, text: str, raw: dict) -> None:
        seg = raw.get("segment_id")
        key = _n(text)
        now = time.time()
        if not key:
            return
        for k, t, s in self._recent:                                  # avoid processing the same command twice
            if (seg and s == seg) or (k == key and now - t < 3.0):
                return
        self._recent.append((key, now, seg))
        if self._is_echo(text):
            return
        self.heard.emit(text)
        if self.memory.get("mode") == "push_to_talk":
            if now <= self._ptt_until:
                self._ptt_until = 0.0
                self.status.emit("mic", "ok", "listening")
                self._begin_turn(text)
            return
        if self.router.has_pending():          # answering a confirmation never needs the wake word
            self._begin_turn(text)
            return
        wk = wake_word.detect(text)
        if wk.matched:
            if wk.command:
                self._begin_turn(wk.command)
            else:
                self._awake_until = now + max(6.0, self.memory.get_float("conversation_window_s", 8))
                self.speaker.stop()
                self._set_state(LISTENING)
                self.speaker.say("Yes?")
            return
        if not self.memory.get_bool("wake_word_required") or now < self._awake_until:
            self._begin_turn(text)

    # ------------------------------------------------------------------ UI entry points (any thread)
    def submit_text(self, text: str) -> None:
        text = text.strip()
        if text:
            self._call(self._begin_turn, text)

    def listen_now(self) -> None:
        """Mic button: next utterance is a command, no wake word needed."""
        self.speaker.stop()
        self._ptt_until = time.time() + 10
        self._awake_until = time.time() + 10
        self.status.emit("mic", "ok", "listening")
        self._set_state(LISTENING)

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        self.mic.muted = muted
        self.muted_changed.emit(muted)
        self.status.emit("mic", "off" if muted else "ok", "muted" if muted else "listening")
        if muted:
            self._set_state(IDLE)

    def interrupt(self) -> None:
        self.speaker.stop()
        self._call(self._cancel_turn)
        self._set_state(IDLE)

    def confirm(self, yes: bool) -> None:
        self.submit_text("yes" if yes else "no")

    # ------------------------------------------------------------------ turn handling (loop thread)
    def _cancel_turn(self) -> None:
        if self._turn and not self._turn.done():
            self._turn.cancel()

    def _begin_turn(self, text: str) -> None:
        key, now = _n(text), time.time()
        # Never send the same request twice: ignore an identical command that is still running,
        # or was submitted moments ago (double Enter, STT + typed duplicate, ...).
        if key and key == self._turn_key and ((self._turn and not self._turn.done()) or now - self._last_submit[1] < 3.0):
            return
        self._turn_key, self._last_submit = key, (key, now)
        self._cancel_turn()
        self.speaker.stop()                      # speaking over LUFY always interrupts it
        self._turn = self.loop.create_task(self._handle(text))

    async def _handle(self, text: str) -> None:
        self._generating = True
        self._set_state(THINKING)
        try:
            pending = self.router.resolve_pending(text)
            if pending is not None:
                self.confirm_cleared.emit()
                await self._reply_local(text, pending)
                return
            route = await self.loop.run_in_executor(None, self.router.route, text)
            if route.interrupt:
                self._set_state(IDLE)
                return
            if route.handled:
                await self._reply_local(text, route)
                if route.action.startswith("remember") or route.action == "confirmed":
                    self.memory_changed.emit()
                return
            await self._reply_llm(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("turn failed")
            self._report_error(f"Something went wrong: {e}")
            self._set_state(ERROR)
        finally:
            self._generating = False
            if not self.speaker.speaking and self.state in (THINKING,):
                self._set_state(IDLE)

    async def _reply_local(self, user_text: str, route) -> None:
        self.memory.add_message("user", user_text)
        self.chat_message.emit("user", user_text)
        reply = route.reply
        if route.pending:
            self.confirm_request.emit(route.pending.prompt)
        if reply:
            self.memory.add_message("assistant", reply)
            self.chat_message.emit("assistant", reply)
            self.assistant_done.emit(reply)
            self.speaker.say(reply)
        if not self.speaker.speaking:
            self._set_state(IDLE)

    async def _reply_llm(self, user_text: str) -> None:
        self.memory.add_message("user", user_text)
        self.chat_message.emit("user", user_text)
        self.chat_message.emit("assistant", "")
        full, buf = "", ""
        try:
            async for delta in self.brain.reply_stream(user_text):
                full += delta
                buf += delta
                self.assistant_delta.emit(delta)
                parts = SENTENCE_END.split(buf)
                if len(parts) > 1:
                    for sentence in parts[:-1]:
                        self.speaker.say(sentence)
                    buf = parts[-1]
            if buf.strip():
                self.speaker.say(buf)
            self.status.emit("llm", "ok", self.brain.describe())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err = classify(e, "AI")
            self.status.emit("llm", "error", err.user_message)
            self._report_error(err.user_message)
            if not full.strip():
                full = err.spoken or "Sorry, I couldn't get an answer just now."
                self.assistant_delta.emit(full)
                self.speaker.say(full)
        if full.strip():
            self.memory.add_message("assistant", full)
        self.assistant_done.emit(full)

    # ------------------------------------------------------------------ TTS callbacks (tts thread)
    def _on_speak_start(self, text: str) -> None:
        self._set_state(SPEAKING)
        if self.memory.get_bool("show_subtitles"):
            self.subtitle.emit(text)

    def _on_speak_idle(self) -> None:
        self._last_speak_end = time.time()
        self._awake_until = self._last_speak_end + self.memory.get_float("conversation_window_s", 8)
        self.subtitle.emit("")
        if not self._generating:
            self._set_state(IDLE)
