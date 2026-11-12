"""Maps spoken text (English / Hindi / Hinglish) to an allowlist of local actions.

The router NEVER executes text from an LLM. Every action is a fixed Python function;
destructive or sensitive ones return a PendingAction that needs a spoken/clicked "yes".
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from automation import applications, browser, file_manager, spotify, system
from core.memory import Memory, looks_sensitive

CONFIRM_TTL_S = 30
YES = {"yes", "yeah", "yep", "confirm", "confirmed", "sure", "ok", "okay", "do it", "go ahead", "haan", "han", "ha",
       "haa", "kar do", "theek hai", "हाँ", "हां", "ठीक है", "कर दो", "जी"}
NO = {"no", "nope", "cancel", "stop", "don't", "dont", "nahi", "nahin", "mat karo", "rehne do", "नहीं", "मत करो", "रहने दो"}


@dataclass
class PendingAction:
    prompt: str
    run: Callable[[], str]
    created: float = 0.0


@dataclass
class RouteResult:
    handled: bool
    reply: str = ""
    pending: Optional[PendingAction] = None
    action: str = ""          # key used for "frequent commands"
    interrupt: bool = False   # user asked LUFY to stop talking


def _norm(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[^\w\s\u0900-\u097F.:/'-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _strip_fillers(t: str) -> str:
    t = re.sub(r"^(?:hey\s+)?(please|can you|could you|will you|would you|zara|lufy|kya tum|kya aap)\s+", "", t)
    return re.sub(r"\s+(please|kar do|karo|do)$", "", t) if False else t


OPEN_VERBS = r"(?:open|launch|start|run|kholo|khol do|kholna|khol de|chalu karo|chalao|shuru karo|खोलो|खोल दो|चालू करो)"
CLOSE_VERBS = r"(?:close|quit|exit|kill|band karo|band kar do|bandh karo|बंद करो|बंद कर दो)"
MINIMIZE_VERBS = r"(?:minimize|minimise|hide|chhota karo|chhota kar|neeche karo|नीचे करो|छोटा करो|मिनिमाइज़ करो)"
MAXIMIZE_VERBS = r"(?:maximize|maximise|full screen|bada karo|bada kar|पूरा करो|मैक्सिमाइज़ करो)"
RESTORE_VERBS = r"(?:restore|unminimize|show|wapas lao|wapas kholo|वापस लाओ|वापस खोलो)"
FOCUS_VERBS = r"(?:focus|switch to|bring to front|foreground|saamne lao|aage lao|सामने लाओ)"


class CommandRouter:
    def __init__(self, memory: Memory):
        self.memory = memory
        self.pending: Optional[PendingAction] = None
        self.last_found: list[Path] = []

    # ------------------------------------------------------------------ confirmation
    def has_pending(self) -> bool:
        if self.pending and time.time() - self.pending.created > CONFIRM_TTL_S:
            self.pending = None
        return self.pending is not None

    def resolve_pending(self, text: str) -> Optional[RouteResult]:
        """If a confirmation is waiting, interpret `text` as yes/no. Return None if it is neither."""
        if not self.has_pending():
            return None
        t = _norm(text)
        words = set(t.split())
        if t in YES or (words & {"yes", "yeah", "confirm", "haan", "han", "हाँ", "हां"} and not words & NO):
            action, self.pending = self.pending, None
            try:
                return RouteResult(True, action.run(), action="confirmed")
            except Exception as e:  # pragma: no cover - defensive
                return RouteResult(True, f"That didn't work: {e}")
        if t in NO or words & {"no", "cancel", "nahi", "nahin", "नहीं"}:
            self.pending = None
            return RouteResult(True, "Okay, cancelled.", action="cancelled")
        return None

    def cancel_pending(self) -> None:
        self.pending = None

    def _ask(self, prompt: str, run: Callable[[], str]) -> RouteResult:
        self.pending = PendingAction(prompt, run, time.time())
        return RouteResult(True, prompt + " Say yes to confirm or no to cancel.", pending=self.pending)

    # ------------------------------------------------------------------ main entry
    def route(self, text: str) -> RouteResult:
        t = _strip_fillers(_norm(text))
        if not t:
            return RouteResult(False)
        try:
            for handler in (self._control, self._memory_cmds, self._info, self._volume, self._spotify, self._web,
                            self._files, self._window_cmds, self._apps):
                res = handler(t, text)
                if res is not None:
                    if res.handled and res.action:
                        self.memory.record_command(res.action)
                    return res
        except file_manager.FileError as e:
            return RouteResult(True, str(e))
        except Exception as e:
            return RouteResult(True, f"Sorry, that action failed: {e}")
        return RouteResult(False)

    # ------------------------------------------------------------------ handlers
    def _control(self, t: str, raw: str) -> Optional[RouteResult]:
        if t in {"stop", "stop talking", "be quiet", "quiet", "shut up", "ruko", "chup", "chup ho ja", "bas", "bas karo",
                 "ruk jao", "चुप", "रुको", "बस"}:
            return RouteResult(True, "", interrupt=True)
        return None

    def _memory_cmds(self, t: str, raw: str) -> Optional[RouteResult]:
        m = re.match(r"^(?:please )?(?:remember(?: that)?|yaad rakho|yaad rakhna|note that|याद रखो)\s+(.+)$", t)
        if m:
            raw_m = re.match(r"^\W*(?:please\s+)?(?:remember(?:\s+that)?|yaad\s+rakho|yaad\s+rakhna|note\s+that|याद\s+रखो)\s+(.+?)\W*$",
                             raw.strip(), re.I | re.S)
            fact_text = raw_m.group(1).strip() if raw_m else m.group(1)      # keep the user's own casing
            if looks_sensitive(fact_text):
                return self._ask("That looks sensitive, like a password, PIN or ID number. Do you really want me to store it?",
                                 lambda: (self.memory.add_memory(fact_text), "Okay, saved.")[1])
            self.memory.add_memory(fact_text)
            return RouteResult(True, "Got it, I'll remember that.", action="remember")
        m = re.match(r"^(?:forget|bhool ja|bhul jao|भूल जाओ)\s+(?:about |that |ki )?(.+)$", t)
        if m:
            hits = self.memory.find_memories(m.group(1))
            if not hits:
                return RouteResult(True, "I don't have anything saved about that.")
            ids = [h["id"] for h in hits]
            return self._ask(f"I found {len(ids)} saved memory about that. Delete it?" if len(ids) == 1
                             else f"I found {len(ids)} saved memories about that. Delete them all?",
                             lambda: ([self.memory.delete_memory(i) for i in ids], "Done, I've forgotten it.")[1])
        if re.search(r"\b(what do you remember|what do you know about me|my memories|kya yaad hai)\b", t):
            mems = self.memory.list_memories()
            if not mems:
                return RouteResult(True, "I haven't saved anything about you yet.", action="memories")
            return RouteResult(True, "Here's what I remember: " + "; ".join(m["content"] for m in mems[:8]) + ".", action="memories")
        return None

    def _info(self, t: str, raw: str) -> Optional[RouteResult]:
        if re.search(r"\b(time|samay|kitne baje|baje hain|समय|टाइम)\b", t) and not re.search(r"\b(timer|times)\b", t) \
                and re.search(r"\b(what|current|tell|batao|bata|kya|kitne|now|abhi|बताओ|क्या)\b", t):
            return RouteResult(True, system.current_time(), action="time")
        if re.search(r"\b(date|tarikh|taareekh|aaj kya din|what day|which day|today's date|तारीख|आज की तारीख)\b", t):
            return RouteResult(True, system.current_date(), action="date")
        if re.search(r"\b(battery|बैटरी)\b", t):
            return RouteResult(True, system.battery_status(), action="battery")
        if re.search(r"\b(cpu|ram|memory usage|system status|system info|performance|processor)\b", t):
            return RouteResult(True, system.cpu_ram_status(), action="cpu_ram")
        if re.search(r"\b(screenshot|screen shot|screen capture|capture (the )?screen)\b", t):
            return RouteResult(True, system.take_screenshot(), action="screenshot")
        if re.search(r"\block (the |my )?(computer|laptop|pc|screen|system)\b", t) or "laptop lock" in t:
            return RouteResult(True, system.lock_computer(), action="lock")
        return None

    def _volume(self, t: str, raw: str) -> Optional[RouteResult]:
        if not re.search(r"\b(volume|awaaz|aawaz|sound|आवाज़|आवाज)\b", t):
            return None
        m = re.search(r"\b(\d{1,3})\b", t)
        if m and re.search(r"\b(set|to|at|kar do|karo|rakho|percent)\b", t):
            return RouteResult(True, system.set_volume(int(m.group(1))), action="volume")
        if re.search(r"\b(unmute)\b", t):
            return RouteResult(True, system.mute_volume(False), action="volume")
        if re.search(r"\b(mute|silent)\b", t):
            return RouteResult(True, system.mute_volume(True), action="volume")
        if re.search(r"\b(up|increase|raise|louder|badha|badhao|zyada|बढ़ाओ)\b", t):
            return RouteResult(True, system.change_volume(+10), action="volume")
        if re.search(r"\b(down|decrease|lower|reduce|softer|kam|ghata|कम)\b", t):
            return RouteResult(True, system.change_volume(-10), action="volume")
        return None

    def _spotify(self, t: str, raw: str) -> Optional[RouteResult]:
        # Search/play requests are explicit Spotify intents, not generic web searches.
        m = re.match(r"^(?:play|search|find)\s+(.+?)\s+(?:on|in)\s+spotify$", t)
        if not m:
            m = re.match(r"^spotify\s+(?:pe|par|mein|me)\s+(.+?)(?:\s+(?:play|search|chalao|bajao|dhundo)(?:\s+karo)?)?$", t)
        if m:
            query = m.group(1).strip()
            return RouteResult(True, spotify.search_spotify(query), action="spotify_search")
        if re.search(r"\b(pause|stop)\s+(?:the\s+)?(?:spotify|music|song)\b", t) or t in {"pause music", "music pause kar", "gaana rok do"}:
            return RouteResult(True, spotify.toggle_playback(), action="spotify_toggle")
        if re.search(r"\b(resume|continue|play)\s+(?:the\s+)?(?:spotify|music|song)\b", t) or t in {"resume music", "music chalao", "gaana chalao"}:
            return RouteResult(True, spotify.toggle_playback(), action="spotify_toggle")
        if re.search(r"\b(next|skip)\s+(?:song|track)\b", t) or t in {"next song", "agla gaana"}:
            return RouteResult(True, spotify.next_track(), action="spotify_next")
        if re.search(r"\b(previous|back)\s+(?:song|track)\b", t) or t in {"previous song", "pichla gaana"}:
            return RouteResult(True, spotify.previous_track(), action="spotify_previous")
        return None

    def _window_cmds(self, t: str, raw: str) -> Optional[RouteResult]:
        """Deterministic window-management routing; handled before the LLM fallback."""
        target_suffix = r"(?:\s+(?:window|app|application))?$"
        current_words = {"current", "current window", "this window", "this app", "current app", "active window", "active app"}

        # Natural phrasing where the verb surrounds the target: "bring Chrome to front".
        front = re.match(r"^(?:please )?bring\s+(.+?)\s+to (?:the )?front$", t)
        if front:
            target = front.group(1).strip()
            if target in {"this", "current", "this window", "current window"}:
                target = "current window"
            if target != "current window" and not applications.resolve_known(target):
                return RouteResult(True, f"I don't recognize {target} as a supported open application.")
            return RouteResult(True, applications.focus_window(target), action="window_focus")

        patterns = [
            ("minimize", MINIMIZE_VERBS, applications.minimize_window),
            ("maximize", MAXIMIZE_VERBS, applications.maximize_window),
            ("restore", RESTORE_VERBS, applications.restore_window),
            ("focus", FOCUS_VERBS, applications.focus_window),
        ]
        for action, verbs, function in patterns:
            # Verb first: "minimize Chrome", "minimize the current window".
            m = re.match(rf"^{verbs}\s+(?:the\s+)?(.+?)" + target_suffix, t)
            # App first: "Chrome minimize", "Chrome ko minimize kar do".
            if not m:
                m = re.match(rf"^(.+?)\s+(?:ko\s+)?{verbs}(?:\s+(?:kar|karo|kar do|please))?" + target_suffix, t)
            if not m:
                continue
            target = m.group(1).strip()
            target = re.sub(r"^(?:the|my)\s+", "", target)
            target = re.sub(r"\s+(?:ko|kar|karo|kar do|please)$", "", target).strip()
            if target in {"it", "this", "the current", "current", "this app", "this window", "current window", "active window"}:
                target = "current window"
            elif target in {"window", "app", "application"}:
                target = "current window"
            # Avoid sending unsupported targets into the LLM and giving a fake success.
            if target != "current window" and not applications.resolve_known(target):
                return RouteResult(True, f"I don't recognize {target} as a supported open application.")
            reply = function(target)
            return RouteResult(True, reply, action=f"window_{action}")
        return None

    def _web(self, t: str, raw: str) -> Optional[RouteResult]:
        m = re.match(r"^(?:search|play|find)\s+(.+?)\s+(?:on|in)\s+youtube$", t) or \
            re.match(r"^youtube\s+(?:pe|par|mein|me|पर)\s+(.+?)(?:\s+(?:search|play|chalao|dhundo)(?:\s+karo)?)?$", t) or \
            re.match(r"^(?:search )?youtube (?:for )?(.+)$", t) or re.match(r"^play\s+(.+?)\s+youtube$", t)
        if m:
            return RouteResult(True, browser.search_youtube(m.group(1)), action="youtube")
        m = re.match(r"^(?:search|google|find|look up)\s+(?:on google |google for |for )?(.+?)(?:\s+(?:on|in) google)?$", t) or \
            re.match(r"^google\s+(?:pe|par|mein|me|पर)\s+(.+?)(?:\s+search(?:\s+karo)?)?$", t) or \
            re.match(r"^(.+?)\s+(?:google (?:pe|par|karo)|search karo|dhundo|dhoondo)$", t)
        if m and not re.search(r"\bfiles?\b|\bfolders?\b", t):
            q = re.sub(r"^(?:for|about)\s+", "", m.group(1)).strip()
            if q and q not in ("google", "youtube"):
                return RouteResult(True, browser.search_google(q), action="google")
        m = re.match(rf"^(?:open|go to|visit|browse|{OPEN_VERBS})\s+(?:the )?(?:website |site )?(.+)$", t)
        if m:
            reply = browser.open_website(m.group(1))
            if reply:
                return RouteResult(True, reply, action="website")
        m = re.match(r"^(.+?)\s+(?:website\s+)?(?:kholo|khol do|open karo)$", t)
        if m and "." in m.group(1):
            reply = browser.open_website(m.group(1))
            if reply:
                return RouteResult(True, reply, action="website")
        return None

    def _files(self, t: str, raw: str) -> Optional[RouteResult]:
        m = re.match(r"^(?:create|make|new|banao)\s+(?:a |an |ek )?(?:new )?folder(?:\s+(?:called|named|name))?\s+(.+)$", t)
        if m:
            return RouteResult(True, file_manager.create_folder(m.group(1)), action="create_folder")
        m = re.match(r"^(?:create|make|new|banao)\s+(?:a |an |ek )?(?:new )?(?:text |txt )?file(?:\s+(?:called|named|name))?\s+(.+)$", t)
        if m:
            return RouteResult(True, file_manager.create_text_file(m.group(1).replace(" dot ", ".")), action="create_file")
        m = re.match(r"^(?:organize|organise|clean up|sort)\s+(?:my |the )?(.+?)(?: folder)?$", t)
        if m and file_manager.known_folder(m.group(1)):
            folder = file_manager.known_folder(m.group(1))
            plan = file_manager.plan_organize(folder)
            if not plan:
                return RouteResult(True, f"There's nothing to organize in {folder.name}.")
            return self._ask(f"I'll sort {len(plan)} files in {folder.name} into folders by type.",
                             lambda: file_manager.apply_organize(plan))
        m = re.match(r"^(?:rename)\s+(?:the )?(?:file |folder )?(.+?)\s+to\s+(.+)$", t)
        if m:
            hits = file_manager.search_files(m.group(1), limit=3)
            if not hits:
                return RouteResult(True, f"I couldn't find {m.group(1)}.")
            self.last_found = hits
            if len(hits) > 1 and hits[0].stem.lower() != m.group(1).strip():
                return RouteResult(True, "I found several matches: " + ", ".join(h.name for h in hits) + ". Please be more specific.")
            new = m.group(2)
            return self._ask(f"Rename {hits[0].name} to {new}?", lambda: file_manager.rename(hits[0], new))
        m = re.match(r"^(?:delete|remove|trash|hatao|मिटाओ)\s+(?:the )?(?:file |folder )?(.+)$", t)
        if m:
            hits = file_manager.search_files(m.group(1), limit=3)
            if not hits:
                return RouteResult(True, f"I couldn't find {m.group(1)}.")
            if len(hits) > 1 and hits[0].stem.lower() != m.group(1).strip():
                return RouteResult(True, "I found several matches: " + ", ".join(h.name for h in hits) + ". Please be more specific.")
            return self._ask(f"Move {hits[0].name} to the Recycle Bin?", lambda: file_manager.delete_to_trash(hits[0]))
        m = re.match(r"^(?:find|search(?: for)?|locate|dhundo)\s+(?:my |the )?(?:file|folder|document)s?\s+(?:called |named )?(.+)$", t)
        if m:
            hits = file_manager.search_files(m.group(1))
            self.last_found = hits
            if not hits:
                return RouteResult(True, f"I couldn't find any file matching {m.group(1)}.", action="find_file")
            names = ", ".join(h.name for h in hits[:3])
            more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""
            return RouteResult(True, f"I found {len(hits)}: {names}{more}. Say 'open the first one' to open it.", action="find_file")
        if re.match(r"^(?:open|kholo)\s+(?:the |it|that)\s*(first|1st|top)?\s*(?:one|file|result)?$", t) and self.last_found:
            return RouteResult(True, file_manager.open_path(self.last_found[0]), action="open_file")
        m = re.match(r"^(?:open|kholo)\s+(?:the )?(?:file|document)\s+(?:called |named )?(.+)$", t)
        if m:
            hits = file_manager.search_files(m.group(1), limit=5)
            self.last_found = hits
            if not hits:
                return RouteResult(True, f"I couldn't find a file called {m.group(1)}.")
            if len(hits) == 1 or hits[0].stem.lower() == m.group(1).strip():
                return RouteResult(True, file_manager.open_path(hits[0]), action="open_file")
            return RouteResult(True, "I found several: " + ", ".join(h.name for h in hits[:4]) + ". Say 'open the first one' or be more specific.")
        return None

    def _apps(self, t: str, raw: str) -> Optional[RouteResult]:
        m = re.match(rf"^{CLOSE_VERBS}\s+(?:the )?(.+)$", t) or re.match(rf"^(.+?)\s+{CLOSE_VERBS}$", t)
        if m:
            name = m.group(1)
            key = applications.can_close(name)
            if not key:
                return RouteResult(True, f"I can only close apps from my supported list, and {name} isn't one of them.")
            return self._ask(f"Close {key}? Unsaved work may be lost.", lambda: applications.close_app(name))
        m = re.match(rf"^{OPEN_VERBS}\s+(?:the |my |an? )?(.+?)(?:\s+(?:app|application))?$", t) or \
            re.match(rf"^(.+?)\s+(?:app\s+)?{OPEN_VERBS}$", t)
        if m:
            return RouteResult(True, applications.open_app(m.group(1)), action="open_app:" + m.group(1)[:30])
        return None
