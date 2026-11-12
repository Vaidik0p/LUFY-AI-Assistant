"""Colors and the global Qt stylesheet (dark navy + cyan/blue glow, glass cards)."""
BG_TOP, BG_BOTTOM = "#050914", "#0a1530"
TEXT, MUTED = "#dce8ff", "#7f93b8"
CYAN, BLUE, VIOLET, RED, GREEN, AMBER = "#22d3ee", "#3b82f6", "#8b7bff", "#ff4d6d", "#34d399", "#fbbf24"

STATE_COLORS = {"idle": BLUE, "listening": CYAN, "thinking": VIOLET, "speaking": "#60c8ff", "error": RED}
STATE_LABELS = {"idle": "Idle", "listening": "Listening", "thinking": "Thinking", "speaking": "Speaking", "error": "Error"}

QSS = f"""
* {{ font-family: "Segoe UI Variable", "Segoe UI", "Inter", sans-serif; color: {TEXT}; font-size: 13px; }}
QMainWindow, QDialog {{ background: qlineargradient(x1:0,y1:0,x2:0.6,y2:1, stop:0 {BG_TOP}, stop:1 {BG_BOTTOM}); }}
QWidget#root {{ background: transparent; }}
QFrame#glass {{ background: rgba(18, 32, 66, 150); border: 1px solid rgba(96, 165, 250, 55); border-radius: 18px; }}
QLabel {{ background: transparent; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#title {{ font-size: 15px; font-weight: 600; letter-spacing: 1px; }}
QLabel#clock {{ font-size: 30px; font-weight: 300; }}
QLabel#stateLabel {{ font-size: 20px; font-weight: 500; }}
QLabel#subtitle {{ font-size: 17px; color: #eaf2ff; }}
QLabel#caption {{ color: {MUTED}; font-style: italic; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: rgba(10, 20, 45, 190); border: 1px solid rgba(96, 165, 250, 70); border-radius: 12px; padding: 8px 12px;
    selection-background-color: {BLUE}; }}
QLineEdit:focus, QComboBox:focus {{ border: 1px solid {CYAN}; }}
QComboBox QAbstractItemView {{ background: #0d1a3a; border: 1px solid rgba(96,165,250,90); selection-background-color: {BLUE}; }}
QPushButton {{ background: rgba(59, 130, 246, 70); border: 1px solid rgba(96, 165, 250, 120); border-radius: 12px; padding: 8px 16px; }}
QPushButton:hover {{ background: rgba(34, 211, 238, 80); border-color: {CYAN}; }}
QPushButton:pressed {{ background: rgba(34, 211, 238, 130); }}
QPushButton:disabled {{ color: {MUTED}; background: rgba(40, 55, 90, 80); border-color: rgba(96,165,250,40); }}
QPushButton#danger {{ background: rgba(255, 77, 109, 70); border-color: rgba(255, 77, 109, 150); }}
QPushButton:focus {{ border: 1px solid {CYAN}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(96,165,250,90); border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QTabWidget::pane {{ border: 1px solid rgba(96,165,250,55); border-radius: 12px; top: -1px; }}
QTabBar::tab {{ background: transparent; padding: 9px 18px; color: {MUTED}; }}
QTabBar::tab:selected {{ color: {CYAN}; border-bottom: 2px solid {CYAN}; }}
QCheckBox {{ spacing: 8px; }}
QSlider::groove:horizontal {{ height: 4px; background: rgba(96,165,250,70); border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {CYAN}; width: 16px; margin: -6px 0; border-radius: 8px; }}
QListWidget, QTableWidget {{ background: rgba(10,20,45,150); border: 1px solid rgba(96,165,250,55); border-radius: 12px; gridline-color: rgba(96,165,250,30); }}
QHeaderView::section {{ background: rgba(18,32,66,200); border: none; padding: 6px; color: {MUTED}; }}
QToolTip {{ background: #0d1a3a; color: {TEXT}; border: 1px solid {CYAN}; }}
"""
