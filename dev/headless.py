r"""Headless end-to-end test: the real App (models, selection edits, hotwords, menus, undo) with a simulated text box in
place of the focused app, so nothing is typed into real windows. Audio comes from TTS wav files.

  .venv\Scripts\python.exe dev\headless.py <dir> step,step,...

Each step is a wav name in <dir>/wav (spoken), sel:WORD (select the last WORD in the box), click (the user clicks:
ends what the app knows about the text), tick (click ✓ in the add-hotword box), pick:N (click row N of an open
menu, 1-based), keep (click 保留「X」 in a context hotword question), undo (double tap of the hotkey), type:TEXT
(the user replaces the last dictation's copy of its first differing word by hand: type:城市=程式, then the idle
check runs at once) or key:BACK (the user presses Backspace n times: key:BACK*3).
It works on a copy of hotwords.json (<dir>/hotwords.test.json), never on the real file.
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
appmod.foreground = session.foreground = lambda: HWND
appmod.caret_rect = lambda: None
appmod.visible_text = lambda: box.text
appmod.PushToTalk.start = lambda self: None        # no real hotkey / keyboard hook
appmod.App._watch_mouse = lambda self: None       # the user's real clicks must not end the simulated session


class Probe:
    def __init__(self, *_):
        self._ctx = selection.Context(box.selection(), [])

    def result(self, timeout=2.0):
        return self._ctx


appmod.ContextProbe = Probe
# A private copy of hotwords.json: the real one may be in use by a running VoiceInput at the same time
_hotwords = Path(S) / "hotwords.test.json"
_hotwords.write_bytes(paths.HOTWORDS_PATH.read_bytes() if paths.HOTWORDS_PATH.exists() else b"[]")
_Store = appmod.HotwordStore
appmod.HotwordStore = lambda: _Store(_hotwords)
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
while time.monotonic() - t0 < 120 and not (A.recognizer and A.second):
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
        if step == "tick":
            shown = A.picker.isVisible() and A._menu is not None and A._menu[0] == "learn"
            if shown:
                A.picker._confirm()
            pump(0.3)
            print(f"[點 ✓] {'加入' if shown else '（沒有方塊）'}  熱詞：{[(h.key, h.value) for h in A.hotwords.items]}")
            continue
        if step.startswith("pick:") or step == "keep":
            shown = A.picker.isVisible()
            if shown:
                A.picker._pick(int(step[5:]) - 1) if step != "keep" else A.picker._decline()
            pump(0.3)
            shown_text = box.text[:box.caret] + "|" + box.text[box.caret:]
            print(f"[點 {step}] {'' if shown else '（沒有選單）'} → 文字框：{shown_text!r}")
            continue
        if step == "undo":
            A.undo()
            pump(0.3)
            print(f"[快按兩下] → 文字框：{box.text[:box.caret] + '|' + box.text[box.caret:]!r}")
            continue
        if step.startswith("type:"):
            old, new = step[5:].split("=")
            i = box.text.rfind(old)
            box.text = box.text[:i] + new + box.text[i + len(old):]
            box.caret = len(box.text)
            A.session.on_key(0x41)
            A._check_typed_fix()
            pump(0.5)
            offer = f"   ✓ 框：{A.picker.title.text()} {A._menu[1:3]}" if A._menu and A._menu[0] == "learn" else ""
            print(f"[手動改 {old} → {new}] → 文字框：{box.text!r}{offer}")
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
        A.worker.submit(A._transcribe_job, A.recognizer, load(step), Probe(), HWND)
        pump(2.5)
        menu = f"   選單：{A.picker.title.text()} {A.picker.candidates}" if A.picker.isVisible() else ""
        shown = box.text[:box.caret] + "|" + box.text[box.caret:]
        print(f"[{step}] {A._records[-1]['fmt'] if A._records else ''!r:32} → 文字框：{shown!r}{menu}")
finally:
    A.quit()
    shutil.rmtree(Path(S) / "__pycache__", ignore_errors=True)
