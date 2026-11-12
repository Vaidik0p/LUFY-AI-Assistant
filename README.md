# LUFY - Windows desktop AI voice assistant

Voice in (Gnani Prisma STT) -> intent routing / LLM -> voice out (Gnani Timbre TTS), with a PySide6 UI,
SQLite memory and an allow-listed Windows automation layer. English, Hindi and Hinglish.

## 1. Install (Windows 10/11)

1. Install Python 3.11+ from python.org (tick "Add python.exe to PATH").
2. Open PowerShell in this folder:
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   copy .env.example .env
   ```
3. Edit `.env` and paste your keys (see section 2). Never commit or share `.env`.
4. Run: `python main.py`

## 2. API configuration (`.env`)

| Variable | Purpose |
|---|---|
| `GNANI_API_KEY` | Gnani key, sent as header `x-api-key-id` (STT) / `X-API-Key-ID` (TTS), per docs.gnani.ai |
| `GNANI_STT_ENDPOINT` | default `wss://api.vachana.ai/stt/v3/stream` |
| `GNANI_TTS_ENDPOINT` | default `wss://api.vachana.ai/api/v1/tts` |
| `LLM_PROVIDER` | `openai` (= the OpenAI-compatible client: OpenRouter, OpenAI, Ollama...) or `anthropic` |
| `OPENAI_API_KEY` | **Your OpenRouter key** (`sk-or-v1-...`, from openrouter.ai/keys). The variable keeps its `OPENAI_` name because LUFY uses the OpenAI-compatible client. An OpenAI key (`sk-proj-...`) is rejected by OpenRouter - LUFY detects this and says so before sending anything |
| `OPENAI_BASE_URL` | `https://openrouter.ai/api/v1` (default). Direct OpenAI: `https://api.openai.com/v1`. Local: `http://localhost:11434/v1` |
| `OPENAI_MODEL` | `openai/gpt-4o-mini` - OpenRouter IDs are `provider/model`. A bare `gpt-4o-mini` is auto-fixed to `openai/gpt-4o-mini` |

`.env` always wins over Windows environment variables of the same name (a leftover system-wide `OPENAI_API_KEY` can't override it).
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | only if `LLM_PROVIDER=anthropic` |

Without a Gnani key LUFY still runs: typing works and the offline Windows voice (pyttsx3) speaks.
Without an LLM key, built-in commands still work; questions show a clear "no API key" message.
Add another LLM by subclassing `LLMProvider` in `core/ai_brain.py` and adding it to `PROVIDERS`.

## 3. Using LUFY

* Say **"Lufy ..."** or **"Loofy ..."** (e.g. "Lufy open Chrome"; "Hey Lufy" also works). Saying just "Lufy" opens an 8-second follow-up window.
  After each reply the same follow-up window applies, so you can chat without repeating the wake word.
* **Mic button / Ctrl+Space** = talk now (no wake word). **Mute mic** stops all capture. **Stop / Esc** silences LUFY.
  Speaking over LUFY interrupts it (see troubleshooting if it interrupts itself - use headphones).
* Settings: language (English+Hinglish `en-IN` or Hindi `hi-IN`), push-to-talk mode, microphone, voice/model/speed/volume,
  LLM provider, memory manager (view / add / edit / delete), frequent commands.

Example commands: open/close Chrome, VS Code, Notepad, Calculator, File Explorer, Spotify, any Start-Menu app; minimize/maximize/restore/focus Chrome or the current window; Spotify song/artist search opens Spotify search (select a result to start playback), with media-key next/previous/play-pause controls;
"search Google for ...", "search cats on YouTube", "open github.com"; "what's the time/date", "battery", "CPU and RAM",
"volume 40", "volume up", "mute", "lock the computer", "take a screenshot"; "create folder Projects",
"create file notes", "find file resume", "open the first one", "rename file notes to todo", "organize my downloads";
"remember that I prefer short answers", "what do you remember", "forget that". Hindi/Hinglish: "Chrome kholo",
"youtube pe lofi music chalao", "samay batao", "awaaz kam karo".

## 4. Safety design

* The LLM only **talks**. It can never trigger actions: every action is a fixed Python function matched by `core/command_router.py`.
  Apps launch only from a fixed list or Start-Menu shortcuts; URLs must be http/https.
* Confirmation ("say yes") is required to: close apps, delete files (goes to the Recycle Bin), rename files,
  reorganise a folder, delete memories, and save anything that looks sensitive (passwords, PINs, card/ID numbers).
* File operations are confined to your user folder; new files go to `Documents\LUFY`.
* Memories are saved only when you say "remember ..." or add them in Settings. Nothing is saved automatically.

## 5. Privacy and cost - please read

* Wake-word detection runs on the **transcript**, so in always-listening mode your microphone audio is streamed to Gnani
  continuously while LUFY is open. This can use quota and means room audio leaves your PC. Options: Settings > Listening >
  "Only stream audio while I'm speaking" (beta), push-to-talk mode, or Mute.
* Conversation text is sent to your chosen LLM provider; history and memories are stored locally in `%USERPROFILE%\.lufy\lufy.db`.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| "Microphone unavailable" | Windows Settings > Privacy > Microphone: allow desktop apps; pick the device in LUFY Settings |
| "Speech recognition: authentication failed" | Wrong/expired `GNANI_API_KEY`; fix `.env`, restart |
| "cannot reach the server" | Internet/firewall/VPN; wss on port 443 must be allowed |
| STT works but nothing happens | You must say "Lufy" or "Loofy" first (or press the mic); watch the grey caption under the orb |
| "Lufy" often missed | Try `en-IN` language; lower Noise filter slider; speak after a short pause; use the mic button |
| LUFY interrupts itself | Speakers feed back into the mic: use headphones, raise "Interrupt sensitivity" level, or disable interrupt |
| Hindi spoken in English accent / odd voice | Settings > Voice: model `timbre-v3.0` (all voices speak all languages) and speech language `auto` |
| Voice is robotic | Gnani TTS failed, so the offline voice is used; check the Voice chip in the Connections card |
| Volume commands do nothing | `pip install pycaw comtypes`; otherwise media-key fallback is used |
| "AI rate limit" / "Sorry, I couldn't get an answer" | Run `python tools/diagnose_llm.py` - it prints the real HTTP status and API error code and the fix. Common causes: `429 insufficient_quota` (no API credit - billing, not a rate limit), `401 invalid_api_key`, `404 model_not_found`, a stale Windows `OPENAI_API_KEY` environment variable overriding `.env` |
| Rate limit (temporary) | LUFY retries `429 rate_limit_exceeded` / 5xx / network errors with exponential backoff (up to 3 times). Billing, key and model errors are never retried and pause AI requests for 2 minutes (saving Settings clears the pause) |
| Install fails on `PySide6`/`pyttsx3` | Use 64-bit Python 3.11/3.12 and `pip install --upgrade pip` |

## 6a. OpenRouter: test it live

```
python tools/diagnose_llm.py                  # config + model + key/credits + one streaming request; pinpoints the cause of any failure
python tools/test_openrouter_conversation.py  # real 3-turn conversation: streaming, memory of turn 1, Hinglish (throwaway database)
```

## 6b. AI error handling

The UI shows the real cause (HTTP status + error code + request id) and LUFY speaks a short specific sentence. Handled separately:

| Situation | Behaviour |
|---|---|
| 401 bad/revoked key (or an OpenAI key used with OpenRouter) | clear message, never retried, AI paused 2 min |
| 402 insufficient credits | "add credits at openrouter.ai/credits", never retried, AI paused 2 min |
| 402 with `Retry-After` (temporary in-flight budget) | waits and retries |
| 403 spending limit / key limit | blocked until reset time shown |
| 403 moderation / guardrail | only that request declined; next question works |
| 404 / unknown model | shows the model ID and the `provider/model` format |
| 429 temporary | exponential backoff + jitter, honours `Retry-After`, 3 retries |
| 429 daily free-model limit | not retried |
| 408 / 502 / 503 / network / empty 200 | bounded retries (3 / 3 / 2 / 1) |
| error after text already streamed | never retried (no repeated half answers) |

Details: The SDK's hidden auto-retry is disabled so
requests are never silently repeated; identical requests within 3 seconds are sent once. Logs are scrubbed of API keys
(`sk-...`, `vach_...`, Bearer tokens) including tracebacks.

## 7. Layout

`main.py` entry - `config.py` settings/env - `core/` assistant orchestrator, LLM brain, router, memory, errors -
`speech/` mic, Gnani STT, Gnani TTS + fallback, wake word - `automation/` apps, browser, system, files -
`ui/` window, widgets, orb/waveform, styles - `database/` SQLite - `tests/` run with `python -m pytest`.

## 8. What has and has not been verified

Verified in a Linux sandbox: unit/integration tests (41+) for routing, confirmations, memory, file safety, wake word,
de-duplication, echo filtering, interruption, mic framing, and STT/TTS clients against a **mock** server built from the
Gnani docs; the UI constructs and renders offscreen.
**Not verified (needs your Windows machine and live keys):** real Gnani STT/TTS responses, real microphone/speaker audio,
Windows app launching, volume, lock, screenshots, wake-word accuracy in your room. First live run: watch the Connections card.
