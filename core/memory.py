"""Memory manager: history, user-approved memories, frequent commands, settings."""
from __future__ import annotations

import re
from typing import Optional

from config import DEFAULT_SETTINGS
from database.database import Database

# Things LUFY must not store without the user explicitly confirming.
SENSITIVE_RE = re.compile(
    r"\b(password|passcode|pin|otp|cvv|card number|credit card|debit card|aadhaar|aadhar|"
    r"pan number|ssn|social security|bank account|account number|api key|secret|token|passport)\b",
    re.I,
)


def looks_sensitive(text: str) -> bool:
    return bool(SENSITIVE_RE.search(text)) or bool(re.search(r"\b\d{12,19}\b", text))


class Memory:
    def __init__(self, db: Database):
        self.db = db

    # ---- conversation history ----
    def add_message(self, role: str, content: str) -> None:
        if content and content.strip():
            self.db.execute("INSERT INTO messages(role, content) VALUES(?,?)", (role, content.strip()))

    def recent_messages(self, limit: int = 12) -> list[dict]:
        rows = self.db.query("SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def history(self, limit: int = 200) -> list[dict]:
        rows = self.db.query("SELECT id, role, content, created_at FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    def clear_history(self) -> None:
        self.db.execute("DELETE FROM messages")

    # ---- user-approved memories ----
    def add_memory(self, content: str) -> int:
        return self.db.execute("INSERT INTO memories(content) VALUES(?)", (content.strip(),))

    def list_memories(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT id, content, created_at, updated_at FROM memories ORDER BY id")]

    def update_memory(self, mem_id: int, content: str) -> None:
        self.db.execute("UPDATE memories SET content=?, updated_at=datetime('now','localtime') WHERE id=?",
                        (content.strip(), mem_id))

    def delete_memory(self, mem_id: int) -> None:
        self.db.execute("DELETE FROM memories WHERE id=?", (mem_id,))

    def find_memories(self, needle: str) -> list[dict]:
        needle = needle.lower().strip()
        return [m for m in self.list_memories() if needle and needle in m["content"].lower()]

    # ---- frequent commands ----
    def record_command(self, key: str) -> None:
        self.db.execute(
            "INSERT INTO command_usage(command,count) VALUES(?,1) "
            "ON CONFLICT(command) DO UPDATE SET count=count+1, last_used=datetime('now','localtime')", (key,))

    def frequent_commands(self, limit: int = 8) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT command, count FROM command_usage ORDER BY count DESC, last_used DESC LIMIT ?", (limit,))]

    # ---- settings ----
    def get(self, key: str, default: Optional[str] = None) -> str:
        rows = self.db.query("SELECT value FROM settings WHERE key=?", (key,))
        if rows:
            return rows[0]["value"]
        return DEFAULT_SETTINGS.get(key, default if default is not None else "")

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key))
        except ValueError:
            return default

    def get_bool(self, key: str) -> bool:
        return self.get(key) in ("1", "true", "True", "yes")
