"""Safe Spotify desktop helpers.

Search opens Spotify's own search URI (or its web search page). Media transport
keys control the active media session. Search does not falsely claim playback.
For automatic track selection/playback, configure Spotify Web API OAuth separately.
"""
from __future__ import annotations

import os
import sys
import webbrowser
from urllib.parse import quote


def search_spotify(query: str) -> str:
    query = query.strip()
    if not query:
        return "Tell me the song or artist you want to search for on Spotify."
    encoded = quote(query, safe="")
    if sys.platform == "win32":
        try:
            os.startfile(f"spotify:search:{encoded}")  # type: ignore[attr-defined]
            return f"Opened Spotify search for {query}. Choose the correct result to start playing."
        except OSError:
            pass
    opened = webbrowser.open(f"https://open.spotify.com/search/{encoded}")
    if opened:
        return f"Opened Spotify web search for {query}. Choose a result to start playing."
    return "I couldn't open Spotify. Check that Spotify is installed or that your browser is available."


def _media_key(key: str, description: str) -> str:
    try:
        import pyautogui
        pyautogui.press(key)
        return description
    except Exception as exc:
        return f"I couldn't send the media control command: {exc}"


def toggle_playback() -> str:
    return _media_key("playpause", "Sent the play/pause command to the active media player.")


def next_track() -> str:
    return _media_key("nexttrack", "Skipped to the next track in the active media player.")


def previous_track() -> str:
    return _media_key("prevtrack", "Went to the previous track in the active media player.")
