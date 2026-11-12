"""Text-based wake word detection on STT transcripts.

Wake words: "Lufy" or "Loofy" (an optional "hey/hi/ok" before it is accepted but not needed).
Speech recognisers spell a made-up name many ways, so we accept common variants, names the STT
splits in two ("loo fy"), and a fuzzy match. Only the first few words are checked, so saying
"lucy" later in a normal sentence doesn't trigger it.

Wake-word detection runs on the transcript, so audio must be streamed to the STT service while
LUFY waits. See README "Privacy & cost" and the stream_gate setting.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

_GREETINGS = {"hey", "hi", "hello", "ok", "okay", "o", "hay", "he", "ae", "arre", "are", "yo", "there",
              "हे", "हाय", "ओके", "अरे", "हेलो", "सुनो", "suno"}
# Latin-script spellings an English/Hinglish recogniser tends to produce, plus Devanagari.
_NAMES = {"lufy", "lufi", "lufee", "lufey", "lufie", "loofy", "loofi", "loofie", "loofey", "loophy", "loophi",
          "lofy", "lofi", "lofie", "luffy", "luffi", "lucy", "lucie", "luvy", "lufe", "loofe", "lufty", "loopy", "lufis",
          "लुफी", "लूफी", "लुफ़ी", "लूफ़ी", "लुफि", "लूफि", "लुफ्फी", "लूफ़ि", "लुसी", "लॉफी", "लुफ्टी", "लूफ्य", "लूफी।"}
_TARGETS = ("lufy", "loofy")
_TOKEN_RE = re.compile(r"[\w\u0900-\u097F]+", re.UNICODE)
_MAX_LEAD = 3          # the name must be within the first few words


@dataclass
class WakeResult:
    matched: bool
    command: str = ""


def _is_name(tok: str) -> bool:
    tok = tok.lower()
    if tok in _NAMES:
        return True
    if not 4 <= len(tok) <= 8 or not tok.isascii():
        return False
    return max(difflib.SequenceMatcher(None, tok, t).ratio() for t in _TARGETS) >= 0.75


def detect(text: str) -> WakeResult:
    """Return (matched, remaining command)."""
    toks = list(_TOKEN_RE.finditer(text))
    words = [t.group(0) for t in toks]
    for i in range(min(_MAX_LEAD, len(words))):
        if i and not all(w.lower() in _GREETINGS for w in words[:i]):
            break                                    # something other than a greeting came first
        end = None
        if i + 1 < len(words) and words[i].isascii() and words[i + 1].isascii() \
                and _is_name(words[i] + words[i + 1]):          # "loo fy", "lu fi"
            end = toks[i + 1].end()
        elif _is_name(words[i]):
            end = toks[i].end()
        if end is not None:
            rest = text[end:].lstrip(" ,.!?;:-\u0964").strip()
            return WakeResult(True, rest)
    return WakeResult(False)
