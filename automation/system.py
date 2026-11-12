"""System information and control. Windows-first; every call degrades gracefully elsewhere."""
from __future__ import annotations

import ctypes
import sys
import time
from datetime import datetime

import psutil

from config import SCREENSHOT_DIR

IS_WINDOWS = sys.platform == "win32"


def current_time() -> str:
    return datetime.now().strftime("It's %I:%M %p.").replace("It's 0", "It's ")


def current_date() -> str:
    return datetime.now().strftime("Today is %A, %d %B %Y.")


def battery_status() -> str:
    b = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if b is None:
        return "I couldn't find a battery - this looks like a desktop PC."
    state = "charging" if b.power_plugged else "on battery"
    msg = f"Battery is at {int(b.percent)} percent and {state}."
    if not b.power_plugged and b.secsleft and b.secsleft > 0:
        h, m = divmod(b.secsleft // 60, 60)
        msg += f" About {h} hours {m} minutes left." if h else f" About {m} minutes left."
    return msg


def cpu_ram_status() -> str:
    cpu = psutil.cpu_percent(interval=0.4)
    ram = psutil.virtual_memory()
    return f"CPU usage is {cpu:.0f} percent. RAM is {ram.percent:.0f} percent used, {ram.used / 2**30:.1f} of {ram.total / 2**30:.1f} gigabytes."


def snapshot() -> dict:
    """Cheap values for the UI status card."""
    b = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    return {"cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent,
            "battery": None if b is None else int(b.percent), "plugged": None if b is None else b.power_plugged}


# ---------------- volume ----------------
def _endpoint_volume():
    from pycaw.pycaw import AudioUtilities  # type: ignore
    dev = AudioUtilities.GetSpeakers()
    if hasattr(dev, "EndpointVolume"):          # pycaw >= 2024
        return dev.EndpointVolume
    from comtypes import CLSCTX_ALL  # type: ignore
    from pycaw.pycaw import IAudioEndpointVolume  # type: ignore
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return ctypes.cast(iface, ctypes.POINTER(IAudioEndpointVolume))


def _keys(key: str, times: int) -> None:
    import pyautogui
    pyautogui.press(key, presses=times, interval=0.01)


def set_volume(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    if IS_WINDOWS:
        try:
            _endpoint_volume().SetMasterVolumeLevelScalar(percent / 100.0, None)
            return f"Volume set to {percent} percent."
        except Exception:
            pass
    try:  # fallback: each media-key press moves volume by ~2%
        _keys("volumedown", 50)
        _keys("volumeup", round(percent / 2))
        return f"Volume set to about {percent} percent."
    except Exception as e:
        return f"I couldn't change the volume ({e})."


def change_volume(delta: int) -> str:
    if IS_WINDOWS:
        try:
            ev = _endpoint_volume()
            cur = ev.GetMasterVolumeLevelScalar() * 100
            return set_volume(int(cur + delta))
        except Exception:
            pass
    try:
        _keys("volumeup" if delta > 0 else "volumedown", max(1, abs(delta) // 2))
        return "Volume turned up." if delta > 0 else "Volume turned down."
    except Exception as e:
        return f"I couldn't change the volume ({e})."


def mute_volume(mute: bool = True) -> str:
    if IS_WINDOWS:
        try:
            _endpoint_volume().SetMute(1 if mute else 0, None)
            return "Muted." if mute else "Unmuted."
        except Exception:
            pass
    try:
        _keys("volumemute", 1)
        return "Toggled mute."
    except Exception as e:
        return f"I couldn't change mute ({e})."


# ---------------- lock / screenshot ----------------
def lock_computer() -> str:
    if not IS_WINDOWS:
        return "Locking the screen is only supported on Windows."
    ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
    return "Locking your computer."


def take_screenshot() -> str:
    try:
        import pyautogui
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
        pyautogui.screenshot().save(path)
        return f"Screenshot saved to {path}."
    except Exception as e:
        return f"I couldn't take a screenshot ({e})."
