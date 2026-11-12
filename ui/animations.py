"""Custom-painted animated widgets: the AI orb and the audio waveform."""
from __future__ import annotations

import collections
import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from ui.styles import STATE_COLORS


class _Animated(QWidget):
    def __init__(self, parent=None, fps: int = 40):
        super().__init__(parent)
        self.state = "idle"
        self.level = 0.0            # smoothed mic level 0..1
        self._target = 0.0
        self._t0 = time.time()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(int(1000 / fps))

    def set_state(self, state: str) -> None:
        self.state = state

    def push_level(self, v: float) -> None:
        self._target = max(self._target * 0.6, v)

    def _tick(self) -> None:
        self.level += (self._target - self.level) * 0.25
        self._target *= 0.88
        self.update()

    @property
    def t(self) -> float:
        return time.time() - self._t0

    def color(self) -> QColor:
        return QColor(STATE_COLORS.get(self.state, "#3b82f6"))


class OrbWidget(_Animated):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(260, 260)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        c = QPointF(w / 2, h / 2)
        base = min(w, h) * 0.5
        col = self.color()
        t = self.t
        # activity: how "alive" the orb looks in each state
        if self.state == "speaking":
            act = 0.45 + 0.25 * math.sin(t * 7) * math.sin(t * 2.3)
        elif self.state == "listening":
            act = 0.25 + self.level * 1.4
        elif self.state == "thinking":
            act = 0.3 + 0.1 * math.sin(t * 3)
        elif self.state == "error":
            act = 0.2 + 0.15 * (math.sin(t * 9) > 0)
        else:
            act = 0.12 + 0.06 * math.sin(t * 1.4)
        act = max(0.0, min(1.0, act))

        # outer glow
        glow = QRadialGradient(c, base)
        g0 = QColor(col); g0.setAlpha(int(90 + 90 * act))
        g1 = QColor(col); g1.setAlpha(0)
        glow.setColorAt(0.35, g0); glow.setColorAt(1.0, g1)
        p.setPen(Qt.NoPen); p.setBrush(QBrush(glow))
        p.drawEllipse(c, base, base)

        # rotating rings
        for i, (rad, speed, span) in enumerate(((0.80, 28, 110), (0.70, -45, 70), (0.60, 70, 150))):
            rc = QColor(col); rc.setAlpha(150 - i * 35)
            pen = QPen(rc, 2.0 if i != 1 else 1.2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            r = base * rad * (1 + 0.03 * act)
            rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
            ang = (t * speed * (2.2 if self.state == "thinking" else 1)) % 360
            for k in range(3 if i != 1 else 2):
                p.drawArc(rect, int((ang + k * 360 / (3 if i != 1 else 2)) * 16), int(span * 16 / (2 if i == 1 else 1.4)))

        # core
        core_r = base * (0.42 + 0.07 * act)
        core = QRadialGradient(QPointF(c.x() - core_r * 0.25, c.y() - core_r * 0.3), core_r * 1.25)
        light = QColor(col).lighter(170); light.setAlpha(255)
        mid = QColor(col); mid.setAlpha(235)
        dark = QColor(col).darker(300); dark.setAlpha(255)
        core.setColorAt(0.0, light); core.setColorAt(0.45, mid); core.setColorAt(1.0, dark)
        p.setPen(Qt.NoPen); p.setBrush(QBrush(core))
        p.drawEllipse(c, core_r, core_r)

        # thinking: orbiting dots
        if self.state == "thinking":
            p.setBrush(QColor("#ffffff"))
            for k in range(3):
                a = t * 3 + k * 2.094
                p.drawEllipse(QPointF(c.x() + math.cos(a) * core_r * 0.55, c.y() + math.sin(a) * core_r * 0.55), 4, 4)


class WaveformWidget(_Animated):
    BARS = 41

    def __init__(self, parent=None):
        super().__init__(parent, fps=30)
        self.setMinimumHeight(56)
        self._hist: collections.deque = collections.deque([0.0] * self.BARS, maxlen=self.BARS)

    def _tick(self):
        super()._tick()
        if self.state == "listening":
            self._hist.append(self.level)
        elif self.state == "speaking":
            t = self.t
            self._hist.append(0.25 + 0.2 * abs(math.sin(t * 9)) * abs(math.sin(t * 3.1)) + 0.08 * math.sin(t * 17))
        elif self.state == "thinking":
            self._hist.append(0.05)
        else:
            self._hist.append(self._hist[-1] * 0.8)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        n = self.BARS
        slot = w / n
        bw = max(2.0, slot * 0.55)
        col = self.color()
        mid = h / 2
        for i, v in enumerate(self._hist):
            edge = 1 - abs(i - n / 2) / (n / 2) * 0.55      # taper toward the sides
            bh = max(3.0, min(h, v * h * 1.6 * edge + 3))
            c = QColor(col); c.setAlpha(int(70 + 185 * min(1, v * 2)))
            p.setPen(Qt.NoPen); p.setBrush(c)
            p.drawRoundedRect(QRectF(i * slot + (slot - bw) / 2, mid - bh / 2, bw, bh), bw / 2, bw / 2)
