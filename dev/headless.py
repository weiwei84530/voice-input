r"""Headless end-to-end test: the real App (models, commands, hotwords, menus, undo) with a simulated text box in
place of the focused app, so nothing is typed into real windows. Audio comes from TTS wav files.

  .venv\Scripts\python.exe dev\headless.py <dir> step,step,...

Each step is a wav name in <dir>/wav (spoken), sel:WORD (select the last WORD in the box), click (the user clicks:
ends what the app knows about the text) or key:BACK (the user presses Backspace n times: key:BACK*3).
hotwords.json is backed up and restored.
"""
import shutil
import sys
import time
import wave
from pathlib import Path

import numpy as np
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voiceinput import app as appmod, output, paths, selection, session  # noqa: E402

S = sys.argv[1]
HWND = 4242


class Box:
    """A text box with a caret and an optional selection."""

    def __init__(self):
        self.text, self.sel, self.caret = "", None, 0      # sel: (start, end)

    def paste(self, s):
        if self.sel:
            self.delete()
        self.text = self.text[:self.caret] + s + self.text[self.caret:]
        self.caret += len(s)

    def back(self, n):
        n = min(n, self.caret)
        self.text = self.text[:self.caret - n] + self.text[self.caret:]
        self.caret -= n

    def delete(self):
        if self.sel:
            a, b = self.sel
            self.text, self.sel, self.caret = self.text[:a] + self.text[b:], None, a

    def selection(self):
        if not self.sel:
            return None
        a, b = self.sel
        line_start = self.text.rfind("\n", 0, a) + 1
        line_end = self.text.find("\n", b)
        line_end = len(self.text) if line_end < 0 else line_end
        return selection.Selection(self.text[a:b], self.text[line_start:a], self.text[b:line_end], "fake.exe", None)


box = Box()
for mod in (appmod, output):
    mod.paste_text = box.paste
appmod.backspace = box.back
appmod.press_delete = box.delete
appmod.press_enter = lambda: box.paste("⏎")
appmod.press_newline = lambda: box.paste("\n")
appmod.foreground = session.foreground = lambda: HWND
appmod.caret_rect = lambda: None
appmod.PushToTalk.start = lambda self: None        # no real hotkey / keyboard hook
appmod.App._watch_mouse = lambda self: None       # the user's real clicks must not end the simulated session


class Probe:
    def __init__(self, *_):
        self._ctx = selection.Context(box.selection(), [], "fake.exe")

    def result(self, timeout=2.0):
        return self._ctx


appmod.ContextProbe = Probe
backup = paths.HOTWORDS_PATH.read_bytes() if paths.HOTWORDS_PATH.exists() else None
qapp = QApplication(sys.argv)
qapp.setQuitOnLastWindowClosed(False)
A = appmod.App(qapp)
A.tray.showMessage = lambda title, text, *a: print(f"      [通知] {title}: {text}")


def pump(sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


def load(name):
    w = wave.open(f"{S}/wav/{name}.wav")
    return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


t0 = time.monotonic()
while time.monotonic() - t0 < 120 and not (A.recognizer and A.second and A.preview):
    pump(0.3)
pump(2)
try:
    for step in sys.argv[2].split(","):
        if step.startswith("sel:"):
            w = step[4:]
            i = box.text.rfind(w)
            box.sel, box.caret = (i, i + len(w)), i + len(w)
            A.session.on_click(0, 0)
            print(f"[選取 {w}]")
            continue
        if step == "click":
            A.session.on_click(0, 0)
            print("[點滑鼠]")
            continue
        if step.startswith("key:BACK"):
            n = int(step.split("*")[1]) if "*" in step else 1
            box.back(n)
            for _ in range(n):
                A.session.on_key(0x08)
            print(f"[按 Backspace ×{n}]")
            continue
        menu_open = A.picker.isVisible()
        A.worker.submit(A._transcribe_job, A.recognizer, load(step), Probe(), menu_open, HWND)
        pump(2.5)
        menu = f"   選單：{A.picker.title.text()} {A.picker.candidates}" if A.picker.isVisible() else ""
        shown = box.text[:box.caret] + "|" + box.text[box.caret:]
        print(f"[{step}] {A._records[-1]['fmt'] if A._records else ''!r:32} → 文字框：{shown!r}{menu}")
finally:
    A.quit()
    if backup is not None:
        paths.HOTWORDS_PATH.write_bytes(backup)
    shutil.rmtree(Path(S) / "__pycache__", ignore_errors=True)
