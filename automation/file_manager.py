"""File operations confined to the user's home folder. Deletes go to the Recycle Bin."""
from __future__ import annotations

import difflib
import os
import re
import shutil
import sys
from pathlib import Path

from config import WORKSPACE_DIR

HOME = Path.home()
SEARCH_ROOTS = [HOME / d for d in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")]
CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".md"},
    "Videos": {".mp4", ".mkv", ".avi", ".mov", ".webm"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Installers": {".exe", ".msi"},
    "Code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".html", ".css", ".json", ".sql", ".asm"},
}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class FileError(Exception):
    pass


def _safe_name(name: str) -> str:
    name = _BAD_CHARS.sub("", name).strip().strip(".")
    if not name:
        raise FileError("That name isn't valid.")
    return name


def _inside_home(p: Path) -> Path:
    rp = p.resolve()
    if rp != HOME and HOME.resolve() not in rp.parents:
        raise FileError("I only work inside your user folder for safety.")
    return rp


def create_folder(name: str, parent: Path | None = None) -> str:
    target = _inside_home((parent or WORKSPACE_DIR) / _safe_name(name))
    target.mkdir(parents=True, exist_ok=True)
    return f"Created folder {target.name} in {target.parent}."


def create_text_file(name: str, content: str = "", parent: Path | None = None) -> str:
    name = _safe_name(name)
    if "." not in name:
        name += ".txt"
    target = _inside_home((parent or WORKSPACE_DIR) / name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileError(f"{target.name} already exists.")
    target.write_text(content, encoding="utf-8")
    return f"Created {target.name} in {target.parent}."


def search_files(query: str, limit: int = 8) -> list[Path]:
    q = query.lower().strip()
    tokens = [t for t in re.split(r"\W+", q) if t]
    scored: list[tuple[float, Path]] = []
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in ("node_modules", "__pycache__", "venv", ".git")]
            for fn in files + dirnames:
                low = fn.lower()
                stem = Path(fn).stem.lower()
                if q and q in low:
                    score = 1.0 + (0.2 if stem == q else 0)
                elif tokens and all(t in low for t in tokens):
                    score = 0.9
                else:
                    ratio = difflib.SequenceMatcher(None, q, stem).ratio()
                    score = ratio if ratio >= 0.8 else 0
                if score:
                    scored.append((score, Path(dirpath) / fn))
            if len(scored) > 400:
                break
    scored.sort(key=lambda s: (-s[0], len(str(s[1]))))
    return [p for _, p in scored[:limit]]


def open_path(path: Path) -> str:
    path = _inside_home(path)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        import subprocess
        subprocess.Popen(["xdg-open", str(path)])
    return f"Opening {path.name}."


def rename(path: Path, new_name: str) -> str:
    path = _inside_home(path)
    new = path.with_name(_safe_name(new_name) + (path.suffix if "." not in new_name else ""))
    _inside_home(new)
    if new.exists():
        raise FileError(f"{new.name} already exists.")
    path.rename(new)
    return f"Renamed {path.name} to {new.name}."


def delete_to_trash(path: Path) -> str:
    path = _inside_home(path)
    try:
        from send2trash import send2trash
        send2trash(str(path))
    except ImportError:
        raise FileError("Install 'send2trash' so deletes go to the Recycle Bin.")
    return f"Moved {path.name} to the Recycle Bin."


def plan_organize(folder: Path) -> dict[Path, Path]:
    """Return {source: destination} without touching anything."""
    folder = _inside_home(folder)
    if not folder.is_dir():
        raise FileError(f"{folder} isn't a folder.")
    plan: dict[Path, Path] = {}
    for f in folder.iterdir():
        if f.is_file() and not f.name.startswith("."):
            for cat, exts in CATEGORIES.items():
                if f.suffix.lower() in exts:
                    plan[f] = folder / cat / f.name
                    break
            else:
                plan[f] = folder / "Other" / f.name
    return plan


def apply_organize(plan: dict[Path, Path]) -> str:
    moved = 0
    for src, dst in plan.items():
        dst.parent.mkdir(exist_ok=True)
        final, n = dst, 1
        while final.exists():
            final = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
            n += 1
        shutil.move(str(src), str(final))
        moved += 1
    return f"Organized {moved} files into folders by type."


def known_folder(name: str) -> Path | None:
    name = name.lower().strip()
    mapping = {"downloads": "Downloads", "desktop": "Desktop", "documents": "Documents",
               "pictures": "Pictures", "videos": "Videos", "music": "Music"}
    for k, v in mapping.items():
        if k in name:
            return HOME / v
    return None
