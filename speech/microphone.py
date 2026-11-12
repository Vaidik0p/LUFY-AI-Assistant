"""Microphone capture: 16 kHz mono int16 delivered as exact 1024-byte frames (Gnani STT requirement)."""
from __future__ import annotations

import threading
from typing import Callable, Optional

import numpy as np

from config import STT_FRAME_BYTES, STT_FRAME_SAMPLES, STT_SAMPLE_RATE
from core.errors import LufyError


def list_input_devices() -> list[tuple[int, str]]:
    try:
        import sounddevice as sd
        return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception:
        return []


class Microphone:
    """Calls `on_frame(bytes, level)` from the audio thread: 1024 bytes + RMS level in 0..1."""

    def __init__(self, on_frame: Callable[[bytes, float], None], device: Optional[int] = None):
        self.on_frame = on_frame
        self.device = device
        self.muted = False
        self._stream = None
        self._buf = bytearray()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
            self._stream = sd.RawInputStream(
                samplerate=STT_SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=STT_FRAME_SAMPLES, device=self.device, callback=self._callback)
            self._stream.start()
        except Exception as e:
            self._stream = None
            msg = str(e)
            if "PortAudio" in msg or "device" in msg.lower() or "Invalid" in msg:
                raise LufyError("Microphone unavailable. Check that one is connected and that Windows allows "
                                "desktop apps to use it (Settings > Privacy > Microphone).") from e
            raise LufyError(f"Microphone error: {e}") from e

    def stop(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        with self._lock:
            self._buf.clear()

    def _callback(self, indata, frames, time_info, status) -> None:  # audio thread
        if self.muted:
            return
        with self._lock:
            self._buf += bytes(indata)
            while len(self._buf) >= STT_FRAME_BYTES:
                frame = bytes(self._buf[:STT_FRAME_BYTES])
                del self._buf[:STT_FRAME_BYTES]
                samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
                level = float(np.sqrt(np.mean(samples ** 2))) * 4.0   # rough 0..1 scaling for speech
                try:
                    self.on_frame(frame, min(1.0, level))
                except Exception:
                    pass
