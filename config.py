"""LUFY configuration. Secrets come ONLY from environment variables / .env (never hardcoded)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values, load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("LUFY_ENV_FILE", BASE_DIR / ".env"))
_MANAGED_VARS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "GNANI_API_KEY")
_OS_BEFORE = {k: os.environ.get(k) for k in _MANAGED_VARS}      # snapshot BEFORE .env is applied
# override=True: the .env file is the source of truth. Without it a stale system-wide OPENAI_API_KEY
# (very common) would silently win over the key you put in .env.
load_dotenv(ENV_FILE, override=True)

APP_NAME = "LUFY"
DATA_DIR = Path(os.getenv("LUFY_DATA_DIR", Path.home() / ".lufy"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "lufy.db"
WORKSPACE_DIR = Path(os.getenv("LUFY_WORKSPACE", Path.home() / "Documents" / "LUFY"))
SCREENSHOT_DIR = Path(os.getenv("LUFY_SCREENSHOTS", Path.home() / "Pictures" / "LUFY"))

# Gnani Vachana endpoints (from https://docs.gnani.ai/ - Realtime STT / Realtime TTS pages)
DEFAULT_STT_ENDPOINT = "wss://api.vachana.ai/stt/v3/stream"
DEFAULT_TTS_ENDPOINT = "wss://api.vachana.ai/api/v1/tts"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "openai/gpt-4o-mini"

# Audio constants dictated by the Gnani realtime STT protocol:
# raw PCM s16le mono, exactly 512 samples (1024 bytes) per frame, real-time cadence.
STT_SAMPLE_RATE = 16000
STT_FRAME_SAMPLES = 512
STT_FRAME_BYTES = STT_FRAME_SAMPLES * 2

TTS_VOICES = {
    "timbre-v3.0": ["Shivani", "Lakshmi", "Kalpana", "Chetana", "Vasudha", "Riyaa", "Tarika",
                    "Avinash", "Shubham", "Prakash", "Devansh", "Yashraj", "Harshad"],
    "timbre-v2.5": ["Nalini", "Kaveri", "Deepak", "Poorvi", "Bhavna", "Jwala", "Devika",
                    "Pranav", "Shlok", "Girish", "Trupti", "Vikrant"],
}


@dataclass
class Env:
    gnani_api_key: str = field(default_factory=lambda: os.getenv("GNANI_API_KEY", "").strip())
    stt_endpoint: str = field(default_factory=lambda: os.getenv("GNANI_STT_ENDPOINT", DEFAULT_STT_ENDPOINT).strip())
    tts_endpoint: str = field(default_factory=lambda: os.getenv("GNANI_TTS_ENDPOINT", DEFAULT_TTS_ENDPOINT).strip())
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "openai").strip().lower())
    # "OPENAI_*" names are kept because LUFY uses the OpenAI-compatible client; with OpenRouter the key
    # stored in OPENAI_API_KEY must be an OpenRouter key (sk-or-v1-...).
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", "").strip())
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", "").strip() or OPENROUTER_BASE_URL)
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "").strip() or "")
    llm_vendor: str = field(default_factory=lambda: os.getenv("LLM_VENDOR", "").strip().lower())   # optional override
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "").strip())
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip())


ENV = Env()


def vendor() -> str:
    """'openrouter' | 'openai' | 'generic' (any other OpenAI-compatible server, e.g. Ollama). Auto-detected from the base URL."""
    if ENV.llm_vendor:
        return ENV.llm_vendor
    host = urlparse(ENV.openai_base_url).netloc.lower()
    if host.endswith("openrouter.ai"):
        return "openrouter"
    return "openai" if host.endswith("api.openai.com") else "generic"


def default_model() -> str:
    return ENV.openai_model or (OPENROUTER_DEFAULT_MODEL if vendor() == "openrouter" else "gpt-4o-mini")


def env_overrides() -> list[str]:
    """Names of variables that are ALSO set in the OS environment with a different value than .env (values never exposed).
    .env wins, so these OS values are ignored - useful to tell the user why an old key 'came back'."""
    file_vals = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    return [k for k in _MANAGED_VARS if file_vals.get(k) and _OS_BEFORE.get(k) not in (None, file_vals[k].strip())]

# User-changeable settings (persisted in SQLite; these are the defaults).
DEFAULT_SETTINGS = {
    "language": "en-IN",          # STT language: en-IN (English + Hinglish) | hi-IN (Hindi)
    "mode": "continuous",         # continuous (wake word) | push_to_talk
    "wake_word_required": "1",
    "conversation_window_s": "8", # seconds LUFY keeps listening after a reply without the wake word
    "tts_enabled": "1",
    "tts_model": "timbre-v3.0",
    "tts_voice": "Shivani",
    "tts_language": "auto",
    "tts_speed": "1.0",           # Gnani allows 0.85 - 1.15
    "tts_volume": "1.0",          # 0.0 - 1.0 (applied locally)
    "barge_in": "1",
    "barge_in_level": "0.10",     # mic level (0-1) that interrupts speech; raise if speakers cause self-interrupts
    "stream_gate": "0",           # 1 = only stream mic audio to the cloud while you are speaking
    "mic_device": "",             # blank = system default
    "vad_threshold": "0.6",
    "min_silence_ms": "600",
    "llm_provider": ENV.llm_provider,
    "llm_model": "",              # blank = provider default from .env
    "show_subtitles": "1",
}
