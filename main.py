"""LUFY - desktop voice assistant entry point."""
import logging
import sys

from PySide6.QtWidgets import QApplication

from core.assistant import Assistant
from core.errors import RedactingFormatter
from ui.main_window import MainWindow


def main() -> int:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(name)s %(levelname)s %(message)s"))   # scrubs API keys everywhere
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)       # their DEBUG output can include request headers
    app = QApplication(sys.argv)
    app.setApplicationName("LUFY")
    assistant = Assistant()
    win = MainWindow(assistant)
    win.show()
    assistant.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
