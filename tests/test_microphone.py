import numpy as np
from speech.microphone import Microphone


def test_frames_are_exactly_1024_bytes_regardless_of_block_size():
    frames = []
    m = Microphone(lambda f, lvl: frames.append((f, lvl)))
    rng = np.random.default_rng(0)
    total = 0
    for n in (300, 512, 777, 1500, 1):                         # odd block sizes from the driver
        data = (rng.standard_normal(n) * 3000).astype(np.int16).tobytes()
        total += len(data)
        m._callback(data, n, None, None)
    assert all(len(f) == 1024 for f, _ in frames)
    assert len(frames) == total // 1024
    assert all(0.0 <= lvl <= 1.0 for _, lvl in frames)


def test_muted_mic_emits_nothing():
    frames = []
    m = Microphone(lambda f, l: frames.append(f)); m.muted = True
    m._callback(b"\x00\x00" * 2048, 2048, None, None)
    assert frames == []


def test_silence_level_is_low_speech_is_higher():
    out = []
    m = Microphone(lambda f, l: out.append(l))
    m._callback(b"\x00\x00" * 512, 512, None, None)
    t = np.arange(512); loud = (np.sin(t / 5) * 12000).astype(np.int16).tobytes()
    m._callback(loud, 512, None, None)
    assert out[0] < 0.01 < 0.1 < out[1]
