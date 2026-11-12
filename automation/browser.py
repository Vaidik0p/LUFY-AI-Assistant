"""Web actions. Only http/https URLs are ever opened."""
from __future__ import annotations

import re
import webbrowser
from urllib.parse import quote_plus

_DOMAIN_RE = re.compile(r"^(?:https?://)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(/\S*)?$", re.I)
_SPOKEN_FIXES = [(r"\s+dot\s+", "."), (r"\s+", "")]
COMMON_SITES = {"youtube": "https://www.youtube.com", "google": "https://www.google.com",
                "gmail": "https://mail.google.com", "github": "https://github.com",
                "whatsapp": "https://web.whatsapp.com", "chatgpt": "https://chatgpt.com",
                "wikipedia": "https://www.wikipedia.org", "maps": "https://maps.google.com",
                "instagram": "https://www.instagram.com", "linkedin": "https://www.linkedin.com"}


def _open(url: str) -> bool:
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Blocked non-web URL")
    return webbrowser.open(url)


def search_google(query: str) -> str:
    _open(f"https://www.google.com/search?q={quote_plus(query)}")
    return f"Searching Google for {query}."


def search_youtube(query: str) -> str:
    _open(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
    return f"Searching YouTube for {query}."


def open_website(target: str) -> str | None:
    """Return a spoken reply, or None if `target` doesn't look like a website."""
    t = target.strip().lower().rstrip(".")
    if t in COMMON_SITES:
        _open(COMMON_SITES[t])
        return f"Opening {t}."
    t = re.sub(_SPOKEN_FIXES[0][0], _SPOKEN_FIXES[0][1], t)
    if " " in t:
        t = re.sub(_SPOKEN_FIXES[1][0], _SPOKEN_FIXES[1][1], t)
    m = _DOMAIN_RE.match(t)
    if not m:
        return None
    url = t if t.startswith("http") else f"https://{t}"
    _open(url)
    return f"Opening {m.group(1)}."
