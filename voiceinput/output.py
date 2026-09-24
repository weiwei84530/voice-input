"""Insert text into the focused app: put it on the clipboard, send paste, restore clipboard."""
import sys
import time

from pynput import keyboard
from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication

_kb = keyboard.Controller()
_PASTE_MOD = keyboard.Key.cmd if sys.platform == "darwin" else keyboard.Key.ctrl


def paste_text(text: str) -> None:
    """Must be called on the Qt main thread."""
    if not text:
        return
    cb = QGuiApplication.clipboard()
    previous = cb.text()
    cb.setText(text)
    time.sleep(0.03)  # let the clipboard owner settle before pasting
    with _kb.pressed(_PASTE_MOD):
        _kb.press("v")
        _kb.release("v")
    # Restore after the target app has had time to read the clipboard
    QTimer.singleShot(400, lambda: cb.setText(previous))
