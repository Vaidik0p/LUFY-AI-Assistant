"""Allow-listed Windows application launch and window management.

LLM output is never executed here. Window operations target known applications
or the currently active window and use normal Windows window messages/APIs.
"""
from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# name -> (aliases, launch candidates, process image name for closing/window matching)
KNOWN_APPS = {
    "chrome": (["chrome", "google chrome", "browser"], ["chrome.exe"], "chrome.exe"),
    "vscode": (["vs code", "vscode", "visual studio code", "code"], ["code"], "Code.exe"),
    "notepad": (["notepad", "note pad"], ["notepad.exe"], "notepad.exe"),
    "calculator": (["calculator", "calc"], ["calc.exe"], "CalculatorApp.exe"),
    "file explorer": (["file explorer", "explorer", "files", "my computer", "this pc"], ["explorer.exe"], "explorer.exe"),
    "spotify": (["spotify", "music"], ["spotify:"], "Spotify.exe"),
    "edge": (["edge", "microsoft edge"], ["msedge.exe"], "msedge.exe"),
    "word": (["word", "ms word", "microsoft word"], ["winword.exe"], "WINWORD.EXE"),
    "excel": (["excel", "ms excel"], ["excel.exe"], "EXCEL.EXE"),
    "powerpoint": (["powerpoint", "ppt"], ["powerpnt.exe"], "POWERPNT.EXE"),
    "paint": (["paint", "mspaint"], ["mspaint.exe"], "mspaint.exe"),
    "settings": (["settings", "windows settings"], ["ms-settings:"], None),
    "task manager": (["task manager"], ["taskmgr.exe"], "Taskmgr.exe"),
    "command prompt": (["command prompt", "cmd"], ["cmd.exe"], "cmd.exe"),
}
_EXTRA_PATHS = {
    "code": [r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe", r"%ProgramFiles%\Microsoft VS Code\Code.exe"],
    "chrome.exe": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                   r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                   r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
    "spotify:": [r"%APPDATA%\Spotify\Spotify.exe"],
}

_start_menu_cache: dict[str, Path] | None = None


def _alias_map() -> dict[str, str]:
    return {alias: key for key, (aliases, _, _) in KNOWN_APPS.items() for alias in aliases}


def resolve_known(name: str) -> str | None:
    name = name.lower().strip()
    aliases = _alias_map()
    if name in KNOWN_APPS:
        return name
    if name in aliases:
        return aliases[name]
    m = difflib.get_close_matches(name, aliases.keys(), n=1, cutoff=0.82)
    return aliases[m[0]] if m else None


def _start_menu_apps() -> dict[str, Path]:
    global _start_menu_cache
    if _start_menu_cache is not None:
        return _start_menu_cache
    apps: dict[str, Path] = {}
    if IS_WINDOWS:
        roots = [Path(os.environ.get("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs",
                 Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs"]
        for root in roots:
            if root.exists():
                for lnk in root.rglob("*.lnk"):
                    if "uninstall" not in lnk.stem.lower():
                        apps[lnk.stem.lower()] = lnk
    _start_menu_cache = apps
    return apps


def refresh_installed_apps() -> int:
    global _start_menu_cache
    _start_menu_cache = None
    return len(_start_menu_apps())


def _start(target: str) -> None:
    """Launch via the Windows shell (resolves App Paths names, URIs and .lnk files)."""
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", target])


def _launch_candidate(cand: str) -> bool:
    for p in _EXTRA_PATHS.get(cand, []):
        p = os.path.expandvars(p)
        if os.path.exists(p):
            subprocess.Popen([p])
            return True
    if cand.endswith(":"):
        try:
            _start(cand)
            return True
        except OSError:
            return False
    exe = shutil.which(cand)
    if exe:
        subprocess.Popen([exe])
        return True
    if IS_WINDOWS:
        try:
            _start(cand)
            return True
        except OSError:
            return False
    return False


def open_app(name: str) -> str:
    key = resolve_known(name)
    if key:
        for cand in KNOWN_APPS[key][1]:
            try:
                if _launch_candidate(cand):
                    return f"Opening {key}."
            except Exception:
                continue
        return f"I couldn't find {key} on this computer. Is it installed?"
    apps = _start_menu_apps()
    match = difflib.get_close_matches(name.lower().strip(), apps.keys(), n=1, cutoff=0.6)
    if not match:
        sub = [k for k in apps if name.lower().strip() in k]
        match = sub[:1]
    if match:
        try:
            _start(str(apps[match[0]]))
            return f"Opening {match[0]}."
        except Exception as e:
            return f"I found {match[0]} but couldn't start it ({e})."
    return f"I couldn't find an app called {name}."


def can_close(name: str) -> str | None:
    key = resolve_known(name)
    return key if key and KNOWN_APPS[key][2] else None


def _windows_for_app(name: str) -> list[dict[str, Any]]:
    """Return visible top-level windows belonging to a supported app (Windows only)."""
    if not IS_WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes
    import psutil

    key = resolve_known(name)
    if not key:
        return []
    process_image = KNOWN_APPS[key][2]
    if not process_image:
        return []
    wanted = process_image.casefold()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    records: list[dict[str, Any]] = []

    enum_windows = user32.EnumWindows
    enum_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    enum_windows.argtypes = [enum_callback_type, wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    get_text_length = user32.GetWindowTextLengthW
    get_text_length.argtypes = [wintypes.HWND]
    get_text_length.restype = ctypes.c_int
    get_text = user32.GetWindowTextW
    get_text.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    get_text.restype = ctypes.c_int
    is_visible = user32.IsWindowVisible
    is_visible.argtypes = [wintypes.HWND]
    is_visible.restype = wintypes.BOOL
    get_pid = user32.GetWindowThreadProcessId
    get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_pid.restype = wintypes.DWORD

    @enum_callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        if not is_visible(hwnd):
            return True
        length = get_text_length(hwnd)
        title_buf = ctypes.create_unicode_buffer(max(length + 1, 2))
        get_text(hwnd, title_buf, len(title_buf))
        title = title_buf.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        get_pid(hwnd, ctypes.byref(pid))
        try:
            proc_name = psutil.Process(pid.value).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return True
        if proc_name.casefold() == wanted:
            records.append({"hwnd": int(hwnd), "title": title, "pid": int(pid.value), "process": proc_name})
        return True

    enum_windows(callback, 0)
    return records


def _current_window() -> dict[str, Any] | None:
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes
    import psutil

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(max(length + 1, 2))
    user32.GetWindowTextW(hwnd, buf, len(buf))
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        process_name = psutil.Process(pid.value).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        process_name = "unknown"
    return {"hwnd": int(hwnd), "title": buf.value.strip(), "pid": int(pid.value), "process": process_name}


def _resolve_window_targets(name: str) -> tuple[list[dict[str, Any]], str]:
    current_aliases = {"current", "current window", "this window", "this app", "current app", "active window", "active app"}
    cleaned = name.strip().lower()
    if cleaned in current_aliases:
        current = _current_window()
        return ([current] if current else []), "current window"
    key = resolve_known(cleaned)
    if not key:
        return [], cleaned
    return _windows_for_app(key), key


def _show_window(name: str, command: int, verb: str) -> str:
    if not IS_WINDOWS:
        return "Window management is only available on Windows."
    import ctypes
    targets, label = _resolve_window_targets(name)
    if not targets:
        return f"I couldn't find an open window for {label}."
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.wintypes.BOOL
    user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindow.restype = ctypes.wintypes.BOOL
    successes = 0
    for item in targets:
        hwnd = item["hwnd"]
        user32.ShowWindow(hwnd, command)
        # ShowWindow's return value reports previous visibility, not success.
        if user32.IsWindow(hwnd):
            successes += 1
    if not successes:
        return f"I couldn't {verb.lower()} {label}."
    return f"{label.title()} {verb.lower()}ed." if verb in {"Minimiz", "Maximiz", "Restor"} else f"{label.title()} {verb.lower()}ed."


def minimize_window(name: str) -> str:
    return _show_window(name, 6, "Minimiz")


def maximize_window(name: str) -> str:
    return _show_window(name, 3, "Maximiz")


def restore_window(name: str) -> str:
    return _show_window(name, 9, "Restor")


def focus_window(name: str) -> str:
    if not IS_WINDOWS:
        return "Window focus is only available on Windows."
    import ctypes
    targets, label = _resolve_window_targets(name)
    if not targets:
        return f"I couldn't find an open window for {label}."
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
    user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
    item = targets[0]
    user32.ShowWindow(item["hwnd"], 9)  # restore if minimized
    ok = bool(user32.SetForegroundWindow(item["hwnd"]))
    return f"Brought {label} to the front." if ok else f"I found {label}, but Windows did not allow it to take focus."


def list_open_windows() -> list[dict[str, Any]]:
    if not IS_WINDOWS:
        return []
    result: list[dict[str, Any]] = []
    for key in KNOWN_APPS:
        result.extend(_windows_for_app(key))
    return result


def close_app(name: str) -> str:
    key = can_close(name)
    if not key:
        return f"I can't close {name}; I only close apps from my supported list."
    if not IS_WINDOWS:
        return "Closing apps is only supported on Windows."
    windows = _windows_for_app(key)
    if not windows:
        return f"{key.title()} doesn't seem to have an open window."
    import ctypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
    user32.PostMessageW.restype = ctypes.wintypes.BOOL
    WM_CLOSE = 0x0010
    sent = 0
    for item in windows:
        if user32.PostMessageW(item["hwnd"], WM_CLOSE, 0, 0):
            sent += 1
    return f"Sent a normal close request to {key}. Save any work if prompted." if sent else f"I couldn't send a close request to {key}."
