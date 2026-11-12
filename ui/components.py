"""Reusable widgets: glass cards, chat bubbles, status chips, round mic button."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ui.styles import AMBER, BLUE, CYAN, GREEN, MUTED, RED


class GlassCard(QFrame):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("glass")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(8)
        if title:
            t = QLabel(title.upper() if False else title)
            t.setObjectName("title")
            self.body.addWidget(t)


class StatusChip(QWidget):
    COLORS = {"ok": GREEN, "busy": AMBER, "error": RED, "off": MUTED}

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.dot = QLabel("●")
        self.name = QLabel(name)
        self.detail = QLabel("")
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)
        self.detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.dot)
        lay.addWidget(self.name)
        lay.addWidget(self.detail, 1)
        self.set("off", "")

    def set(self, state: str, detail: str) -> None:
        self.dot.setStyleSheet(f"color: {self.COLORS.get(state, MUTED)}; font-size: 11px;")
        self.detail.setText(detail)
        self.setToolTip(detail)


class Bubble(QFrame):
    def __init__(self, role: str, text: str):
        super().__init__()
        user = role == "user"
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bg = "rgba(59,130,246,120)" if user else "rgba(34,211,238,38)"
        self.setObjectName("bubble")
        self.setStyleSheet(f"QFrame#bubble {{ background: {bg}; border-radius: 14px; }} QLabel {{ background: transparent; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.addWidget(self.label)

    def append(self, s: str) -> None:
        self.label.setText(self.label.text() + s)


class ChatView(QScrollArea):
    MAX = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self.viewport().setAutoFillBackground(False)
        self.lay = QVBoxLayout(inner)
        self.lay.setContentsMargins(0, 0, 6, 0)
        self.lay.setSpacing(8)
        self.lay.addStretch(1)
        self.setWidget(inner)
        self._last_assistant: Bubble | None = None

    def add(self, role: str, text: str) -> Bubble:
        b = Bubble(role, text)
        row = QHBoxLayout()
        if role == "user":
            row.addStretch(1); row.addWidget(b, 0)
        else:
            row.addWidget(b, 0); row.addStretch(1)
        b.setMaximumWidth(340)
        self.lay.insertLayout(self.lay.count() - 1, row)
        if role == "assistant":
            self._last_assistant = b
        while self.lay.count() > self.MAX:
            item = self.lay.takeAt(0)
            if item.layout():
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w:
                        w.deleteLater()
        QTimer.singleShot(30, self.scroll_bottom)
        return b

    def stream(self, delta: str) -> None:
        if self._last_assistant is None:
            self.add("assistant", "")
        self._last_assistant.append(delta)
        self.scroll_bottom()

    def finish(self, full: str) -> None:
        if self._last_assistant is not None and full:
            self._last_assistant.label.setText(full)
        self._last_assistant = None

    def scroll_bottom(self) -> None:
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear(self) -> None:
        while self.lay.count() > 1:
            item = self.lay.takeAt(0)
            if item.layout():
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w:
                        w.deleteLater()


class MicButton(QPushButton):
    """Round microphone button; pulses while listening."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(60, 60)
        self.setCursor(Qt.PointingHandCursor)
        self.muted = False
        self.active = False
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pulse)
        self._timer.start(50)
        self.setToolTip("Talk to LUFY (Ctrl+Space)")

    def _pulse(self):
        if self.active:
            self._phase = (self._phase + 1) % 20
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        col = QColor(RED if self.muted else CYAN if self.active else BLUE)
        if self.active:
            glow = QColor(col); glow.setAlpha(int(110 - self._phase * 5))
            p.setPen(Qt.NoPen); p.setBrush(glow)
            p.drawEllipse(r.adjusted(-self._phase / 2, -self._phase / 2, self._phase / 2, self._phase / 2))
        fill = QColor(col); fill.setAlpha(190 if not self.underMouse() else 240)
        p.setPen(Qt.NoPen); p.setBrush(fill); p.drawEllipse(r)
        pen = QPen(QColor("white"), 2.4); pen.setCapStyle(Qt.RoundCap); p.setPen(pen); p.setBrush(Qt.NoBrush)
        cx, cy = r.center().x(), r.center().y()
        p.drawRoundedRect(QRectF(cx - 5, cy - 13, 10, 18), 5, 5)
        p.drawArc(QRectF(cx - 10, cy - 8, 20, 18), 200 * 16, 140 * 16)
        p.drawLine(int(cx), int(cy + 10), int(cx), int(cy + 15))
        if self.muted:
            p.setPen(QPen(QColor("white"), 3)); p.drawLine(int(cx - 12), int(cy - 14), int(cx + 12), int(cy + 14))
