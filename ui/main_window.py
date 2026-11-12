"""Main LUFY window + settings dialog."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QSlider, QTabWidget,
                               QTableWidget, QTableWidgetItem, QHeaderView, QVBoxLayout, QWidget, QMainWindow)

from automation import system
from config import ENV, TTS_VOICES
from core.assistant import Assistant
from speech.microphone import list_input_devices
from ui.animations import OrbWidget, WaveformWidget
from ui.components import ChatView, GlassCard, MicButton, StatusChip
from ui.styles import QSS, STATE_COLORS, STATE_LABELS

HINTS = {"idle": "Say “Lufy” or “Loofy”, press the mic, or type below", "listening": "I'm listening…",
         "thinking": "Working on it…", "speaking": "", "error": "Something needs attention"}


class SettingsDialog(QDialog):
    def __init__(self, assistant: Assistant, parent=None):
        super().__init__(parent)
        self.a, self.m = assistant, assistant.memory
        self.setWindowTitle("LUFY settings")
        self.resize(640, 560)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        tabs.addTab(self._listen_tab(), "Listening")
        tabs.addTab(self._voice_tab(), "Voice")
        tabs.addTab(self._ai_tab(), "AI")
        tabs.addTab(self._memory_tab(), "Memory")
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ---- tabs
    def _listen_tab(self):
        w = QWidget(); f = QFormLayout(w); f.setSpacing(12)
        self.language = QComboBox(); self.language.addItem("English + Hinglish (en-IN)", "en-IN"); self.language.addItem("Hindi (hi-IN)", "hi-IN")
        self.language.setCurrentIndex(max(0, self.language.findData(self.m.get("language"))))
        self.mode = QComboBox(); self.mode.addItem("Always listening (wake word)", "continuous"); self.mode.addItem("Push-to-talk only", "push_to_talk")
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.m.get("mode"))))
        self.wake_req = QCheckBox("Require the wake word “Lufy” / “Loofy” (turn off to treat everything I say as a command)")
        self.wake_req.setChecked(self.m.get_bool("wake_word_required"))
        self.window = QLineEdit(self.m.get("conversation_window_s"))
        self.mic = QComboBox(); self.mic.addItem("System default", "")
        for idx, name in list_input_devices():
            self.mic.addItem(f"{name} ({idx})", str(idx))
        self.mic.setCurrentIndex(max(0, self.mic.findData(self.m.get("mic_device"))))
        self.vad = QSlider(Qt.Horizontal); self.vad.setRange(30, 90); self.vad.setValue(int(self.m.get_float("vad_threshold", .6) * 100))
        self.vad.setToolTip("Higher = ignores more background noise but may miss quiet speech")
        self.silence = QSlider(Qt.Horizontal); self.silence.setRange(300, 1200); self.silence.setValue(int(self.m.get_float("min_silence_ms", 600)))
        self.silence.setToolTip("How long a pause ends your sentence. Lower = faster replies, may split sentences")
        self.gate = QCheckBox("Only stream audio to the cloud while I'm speaking (saves data/cost; beta)")
        self.gate.setChecked(self.m.get_bool("stream_gate"))
        self.barge = QCheckBox("Let me interrupt LUFY by speaking (use headphones to avoid echo)")
        self.barge.setChecked(self.m.get_bool("barge_in"))
        self.barge_level = QSlider(Qt.Horizontal); self.barge_level.setRange(3, 40); self.barge_level.setValue(int(self.m.get_float("barge_in_level", .1) * 100))
        for label, wid in (("Language", self.language), ("Mode", self.mode), ("", self.wake_req), ("Follow-up window (seconds)", self.window),
                           ("Microphone", self.mic), ("Noise filter (VAD)", self.vad), ("Pause length (ms)", self.silence),
                           ("", self.gate), ("", self.barge), ("Interrupt sensitivity (lower = easier)", self.barge_level)):
            f.addRow(label, wid)
        return w

    def _voice_tab(self):
        w = QWidget(); f = QFormLayout(w); f.setSpacing(12)
        self.tts_on = QCheckBox("Speak responses aloud"); self.tts_on.setChecked(self.m.get_bool("tts_enabled"))
        self.model = QComboBox(); self.model.addItems(list(TTS_VOICES)); self.model.setCurrentText(self.m.get("tts_model"))
        self.voice = QComboBox(); self.model.currentTextChanged.connect(self._fill_voices); self._fill_voices(self.model.currentText())
        self.voice.setCurrentText(self.m.get("tts_voice"))
        self.tts_lang = QComboBox(); self.tts_lang.addItems(["auto", "en-IN", "hi-IN", "hi-en"]); self.tts_lang.setCurrentText(self.m.get("tts_language"))
        self.speed = QSlider(Qt.Horizontal); self.speed.setRange(85, 115); self.speed.setValue(int(self.m.get_float("tts_speed", 1) * 100))
        self.volume = QSlider(Qt.Horizontal); self.volume.setRange(0, 100); self.volume.setValue(int(self.m.get_float("tts_volume", 1) * 100))
        self.subs = QCheckBox("Show subtitles while speaking"); self.subs.setChecked(self.m.get_bool("show_subtitles"))
        for label, wid in (("", self.tts_on), ("Gnani model", self.model), ("Voice", self.voice), ("Speech language", self.tts_lang),
                           ("Speed (0.85 - 1.15)", self.speed), ("Volume", self.volume), ("", self.subs)):
            f.addRow(label, wid)
        return w

    def _fill_voices(self, model: str):
        self.voice.clear(); self.voice.addItems(TTS_VOICES.get(model, []))

    def _ai_tab(self):
        w = QWidget(); f = QFormLayout(w); f.setSpacing(12)
        from core.ai_brain import PROVIDERS
        self.provider = QComboBox(); self.provider.addItems(list(PROVIDERS)); self.provider.setCurrentText(self.m.get("llm_provider"))
        self.llm_model = QLineEdit(self.m.get("llm_model")); self.llm_model.setPlaceholderText("blank = default from .env")
        import config
        keys = QLabel(f"Gnani key: {'found' if ENV.gnani_api_key else 'MISSING'}   |   LLM key (OPENAI_API_KEY): "
                      f"{'found' if ENV.openai_api_key else 'MISSING'}   |   Anthropic key: {'found' if ENV.anthropic_api_key else 'missing'}\n"
                      f"Endpoint: {config.vendor()} ({ENV.openai_base_url})   |   Default model: {config.default_model()}\n"
                      f"Keys are read from your .env file only (edit it and restart LUFY). They are never shown here.")
        keys.setObjectName("muted"); keys.setWordWrap(True)
        f.addRow("LLM provider", self.provider); f.addRow("Model override", self.llm_model); f.addRow(keys)
        return w

    def _memory_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(QLabel("Things LUFY remembers (only what you asked it to save):"))
        self.table = QTableWidget(0, 2); self.table.setHorizontalHeaderLabels(["Memory", "Saved"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows); self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, fn, name in (("Add", self._mem_add, ""), ("Edit", self._mem_edit, ""), ("Delete", self._mem_delete, "danger"),
                               ("Clear chat history", self._clear_history, "danger")):
            b = QPushButton(text); b.clicked.connect(fn)
            if name: b.setObjectName(name)
            row.addWidget(b)
        v.addLayout(row)
        v.addWidget(QLabel("Frequently used commands:"))
        self.freq = QListWidget(); self.freq.setMaximumHeight(110); v.addWidget(self.freq)
        self._reload_memory()
        return w

    def _reload_memory(self):
        mems = self.m.list_memories()
        self.table.setRowCount(len(mems))
        for r, mem in enumerate(mems):
            it = QTableWidgetItem(mem["content"]); it.setData(Qt.UserRole, mem["id"])
            self.table.setItem(r, 0, it); self.table.setItem(r, 1, QTableWidgetItem(mem["created_at"]))
        self.freq.clear()
        for c in self.m.frequent_commands():
            self.freq.addItem(f"{c['command']}  ×{c['count']}")

    def _sel_id(self):
        r = self.table.currentRow()
        return (self.table.item(r, 0).data(Qt.UserRole), self.table.item(r, 0).text()) if r >= 0 else (None, "")

    def _mem_add(self):
        text, ok = QInputDialog.getText(self, "Add memory", "What should LUFY remember?")
        if ok and text.strip():
            self.m.add_memory(text); self._reload_memory()

    def _mem_edit(self):
        mid, old = self._sel_id()
        if mid is None: return
        text, ok = QInputDialog.getText(self, "Edit memory", "Memory:", text=old)
        if ok and text.strip():
            self.m.update_memory(mid, text); self._reload_memory()

    def _mem_delete(self):
        mid, _ = self._sel_id()
        if mid is not None:
            self.m.delete_memory(mid); self._reload_memory()

    def _clear_history(self):
        if QMessageBox.question(self, "Clear history", "Delete the whole conversation history?") == QMessageBox.Yes:
            self.m.clear_history(); self.parent().chat.clear()

    # ---- save
    def _save(self):
        m = self.m
        m.set("language", self.language.currentData()); m.set("mode", self.mode.currentData())
        m.set("wake_word_required", int(self.wake_req.isChecked()))
        try: m.set("conversation_window_s", max(3, min(30, float(self.window.text()))))
        except ValueError: pass
        m.set("mic_device", self.mic.currentData()); m.set("vad_threshold", self.vad.value() / 100)
        m.set("min_silence_ms", self.silence.value()); m.set("stream_gate", int(self.gate.isChecked()))
        m.set("barge_in", int(self.barge.isChecked())); m.set("barge_in_level", self.barge_level.value() / 100)
        m.set("tts_enabled", int(self.tts_on.isChecked())); m.set("tts_model", self.model.currentText())
        m.set("tts_voice", self.voice.currentText()); m.set("tts_language", self.tts_lang.currentText())
        m.set("tts_speed", self.speed.value() / 100); m.set("tts_volume", self.volume.value() / 100)
        m.set("show_subtitles", int(self.subs.isChecked()))
        m.set("llm_provider", self.provider.currentText()); m.set("llm_model", self.llm_model.text().strip())
        self.a.apply_settings()
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, assistant: Assistant):
        super().__init__()
        self.a = assistant
        self.setWindowTitle("LUFY")
        self.resize(1180, 760)
        self.setMinimumSize(900, 620)
        self.setStyleSheet(QSS)
        root = QWidget(); root.setObjectName("root"); self.setCentralWidget(root)
        grid = QHBoxLayout(root); grid.setContentsMargins(18, 18, 18, 18); grid.setSpacing(16)

        # --- left: conversation
        left = GlassCard("Conversation"); left.setMinimumWidth(300); left.setMaximumWidth(400)
        self.chat = ChatView(); left.body.addWidget(self.chat, 1)
        for m in self.a.memory.history(30):
            self.chat.add(m["role"], m["content"])
        grid.addWidget(left, 3)

        # --- center
        center = QVBoxLayout(); center.setSpacing(10)
        self.orb = OrbWidget(); center.addWidget(self.orb, 5)
        self.state_label = QLabel("Idle"); self.state_label.setObjectName("stateLabel"); self.state_label.setAlignment(Qt.AlignCenter)
        self.hint = QLabel(HINTS["idle"]); self.hint.setObjectName("muted"); self.hint.setAlignment(Qt.AlignCenter)
        self.subtitle = QLabel(""); self.subtitle.setObjectName("subtitle"); self.subtitle.setAlignment(Qt.AlignCenter)
        self.subtitle.setWordWrap(True); self.subtitle.setMinimumHeight(48)
        self.caption = QLabel(""); self.caption.setObjectName("caption"); self.caption.setAlignment(Qt.AlignCenter); self.caption.setWordWrap(True)
        self.wave = WaveformWidget()
        for wdg in (self.state_label, self.hint, self.subtitle, self.caption, self.wave):
            center.addWidget(wdg)
        # confirmation bar (hidden until needed)
        self.confirm_bar = GlassCard(); self.confirm_bar.hide()
        crow = QHBoxLayout(); self.confirm_text = QLabel(""); self.confirm_text.setWordWrap(True)
        yes, no = QPushButton("Yes, do it"), QPushButton("Cancel"); no.setObjectName("danger")
        yes.clicked.connect(lambda: self.a.confirm(True)); no.clicked.connect(lambda: self.a.confirm(False))
        crow.addWidget(self.confirm_text, 1); crow.addWidget(yes); crow.addWidget(no); self.confirm_bar.body.addLayout(crow)
        center.addWidget(self.confirm_bar)
        # input row
        row = QHBoxLayout(); row.setSpacing(10)
        self.mic_btn = MicButton(); self.mic_btn.clicked.connect(self.a.listen_now)
        self.input = QLineEdit(); self.input.setPlaceholderText("Type a command or question and press Enter…"); self.input.returnPressed.connect(self._send)
        self.input.setMinimumHeight(44)
        self.mute_btn = QPushButton("Mute mic"); self.mute_btn.setCheckable(True); self.mute_btn.toggled.connect(self.a.set_muted)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.clicked.connect(self.a.interrupt)
        row.addWidget(self.mic_btn); row.addWidget(self.input, 1); row.addWidget(self.mute_btn); row.addWidget(self.stop_btn)
        center.addLayout(row)
        grid.addLayout(center, 5)

        # --- right: status
        right = QVBoxLayout(); right.setSpacing(14)
        clock = GlassCard(); self.clock = QLabel(); self.clock.setObjectName("clock"); self.date = QLabel(); self.date.setObjectName("muted")
        clock.body.addWidget(self.clock); clock.body.addWidget(self.date); right.addWidget(clock)
        sysc = GlassCard("System"); self.cpu = QLabel(); self.ram = QLabel(); self.bat = QLabel()
        for l in (self.cpu, self.ram, self.bat): sysc.body.addWidget(l)
        right.addWidget(sysc)
        api = GlassCard("Connections"); self.chips = {k: StatusChip(n) for k, n in (("mic", "Mic"), ("stt", "Speech-to-text"), ("llm", "AI brain"), ("tts", "Voice"))}
        for c in self.chips.values(): api.body.addWidget(c)
        right.addWidget(api)
        btns = GlassCard(); sb = QPushButton("Settings and memory"); sb.clicked.connect(self._settings); btns.body.addWidget(sb)
        right.addWidget(btns); right.addStretch(1)
        rw = QWidget(); rw.setLayout(right); rw.setFixedWidth(260); grid.addWidget(rw, 0)

        # --- wiring
        a = self.a
        a.state_changed.connect(self._state); a.level.connect(self._level); a.heard.connect(self._heard)
        a.chat_message.connect(self._chat); a.assistant_delta.connect(self.chat.stream); a.assistant_done.connect(self.chat.finish)
        a.subtitle.connect(self.subtitle.setText); a.error.connect(self._error); a.status.connect(self._status)
        a.confirm_request.connect(self._confirm); a.confirm_cleared.connect(self.confirm_bar.hide)
        a.muted_changed.connect(self._muted)
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self.a.listen_now)
        QShortcut(QKeySequence("Esc"), self, activated=self.a.interrupt)
        self._tick_clock(); t = QTimer(self); t.timeout.connect(self._tick_clock); t.start(1000)
        self._err_timer = QTimer(self); self._err_timer.setSingleShot(True); self._err_timer.timeout.connect(self._clear_error)

    # ---- slots
    def _send(self):
        t = self.input.text().strip()
        if t:
            self.input.clear(); self.a.submit_text(t)

    def _state(self, s: str):
        for w in (self.orb, self.wave): w.set_state(s)
        self.state_label.setText(STATE_LABELS.get(s, s)); self.state_label.setStyleSheet(f"color: {STATE_COLORS.get(s, '#fff')};")
        self.hint.setText(HINTS.get(s, "")); self.mic_btn.active = s == "listening"; self.mic_btn.update()
        if s != "listening": self.caption.setText("") if s == "idle" else None

    def _level(self, v: float):
        self.orb.push_level(v); self.wave.push_level(v)

    def _heard(self, text: str):
        self.caption.setText(f"“{text}”")

    def _chat(self, role: str, text: str):
        self.chat.add(role, text)

    def _error(self, msg: str):
        self.hint.setText(msg); self.hint.setStyleSheet("color: #ff8fa3;"); self._err_timer.start(9000)

    def _clear_error(self):
        self.hint.setStyleSheet(""); self.hint.setText(HINTS.get(self.a.state, ""))

    def _status(self, comp: str, state: str, detail: str):
        if comp in self.chips: self.chips[comp].set(state, detail)

    def _confirm(self, prompt: str):
        self.confirm_text.setText(prompt); self.confirm_bar.show()

    def _muted(self, muted: bool):
        self.mic_btn.muted = muted; self.mic_btn.update(); self.mute_btn.setText("Unmute mic" if muted else "Mute mic")
        if self.mute_btn.isChecked() != muted: self.mute_btn.setChecked(muted)

    def _tick_clock(self):
        now = datetime.now(); self.clock.setText(now.strftime("%I:%M %p").lstrip("0")); self.date.setText(now.strftime("%A, %d %B %Y"))
        s = system.snapshot()
        self.cpu.setText(f"CPU   {s['cpu']:.0f}%"); self.ram.setText(f"RAM   {s['ram']:.0f}%")
        self.bat.setText("Battery   n/a" if s["battery"] is None else f"Battery   {s['battery']}%{' ⚡' if s['plugged'] else ''}")

    def _settings(self):
        SettingsDialog(self.a, self).exec()
        self.hint.setText(HINTS.get(self.a.state, ""))

    def closeEvent(self, e):
        self.a.shutdown(); super().closeEvent(e)
