"""Insert text into the focused app: put it on the clipboard, send paste, restore clipboard. Plus the few keys
voice commands need (Backspace to rewrite what was just dictated, Enter to send, Shift+Enter for a new line)."""
import ctypes
import sys
import time

from pynput import keyboard
from PySide6.QtCore import QMimeData, QTimer
from PySide6.QtGui import QGuiApplication

_kb = keyboard.Controller()
_PASTE_MOD = keyboard.Key.cmd if sys.platform == "darwin" else keyboard.Key.ctrl


def _snapshot(cb) -> QMimeData | None:
    """A copy of every format on the clipboard, so an image or rich text copied before dictating survives
    (only the plain text used to be restored)."""
    md = cb.mimeData()
    if md is None:
        return None
    copy = QMimeData()
    for fmt in md.formats():
        copy.setData(fmt, md.data(fmt))
    if md.hasImage():
        copy.setImageData(md.imageData())
    return copy


def paste_text(text: str) -> None:
    """Must be called on the Qt main thread."""
    if not text:
        return
    cb = QGuiApplication.clipboard()
    try:
        previous = _snapshot(cb)
    except Exception:
        previous = None
    previous_text = cb.text()
    cb.setText(text)
    time.sleep(0.03)  # let the clipboard owner settle before pasting
    with _kb.pressed(_PASTE_MOD):
        _kb.press("v")
        _kb.release("v")

    def restore():
        if previous is not None and previous.formats():
            cb.setMimeData(previous)
        else:
            cb.setText(previous_text)
    # Restore after the target app has had time to read the clipboard
    QTimer.singleShot(400, restore)


def press_delete() -> None:
    """Delete the target app's current selection."""
    _kb.press(keyboard.Key.delete)
    _kb.release(keyboard.Key.delete)


def press_enter() -> None:
    _kb.press(keyboard.Key.enter)
    _kb.release(keyboard.Key.enter)


def press_newline() -> None:
    """Shift+Enter: a new line in chat inputs and Claude Code, a plain line break in editors."""
    with _kb.pressed(keyboard.Key.shift):
        _kb.press(keyboard.Key.enter)
        _kb.release(keyboard.Key.enter)


def backspace(n: int) -> None:
    """Press Backspace n times. On Windows all presses go out in one SendInput call so the app receives them
    as one burst, before the paste that usually follows."""
    if n <= 0:
        return
    if sys.platform != "win32":
        for _ in range(n):
            _kb.press(keyboard.Key.backspace)
            _kb.release(keyboard.Key.backspace)
        return
    events = (_INPUT * (2 * n))()
    for i in range(2 * n):
        events[i].type = 1   # INPUT_KEYBOARD
        events[i].ki = _KEYBDINPUT(0x08, 0, 2 if i % 2 else 0, 0, None)   # VK_BACK, KEYEVENTF_KEYUP on odd
    ctypes.windll.user32.SendInput(len(events), events, ctypes.sizeof(_INPUT))


if sys.platform == "win32":
    import ctypes.wintypes as wt

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _UNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", _UNION)]
