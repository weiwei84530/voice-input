r"""End-to-end test without a microphone: run the real App, feed TTS audio instead of the microphone, and type into a
fresh Notepad or Windows Terminal window. It types into real windows and may learn hotwords: back up
%LOCALAPPDATA%\VoiceInput\hotwords.json first.

  powershell -File dev\tts.ps1 -out <dir>\wav\claude.wav -text "我想請你用 Claude Code 來執行任務"
  .venv\Scripts\python.exe dev\e2e.py <dir> notepad|terminal claude,undo,sel:城市,...

Each step is a wav name in <dir>/wav (spoken), or sel:WORD (select WORD through UI Automation). The log goes to
<dir>/e2e.log.
"""
import ctypes
import logging
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voiceinput import app as appmod, paths, selection  # noqa: E402
from voiceinput.session import foreground  # noqa: E402

S = sys.argv[1]
TARGET = sys.argv[2] if len(sys.argv) > 2 else "notepad"
user32 = ctypes.windll.user32
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(S + "/e2e.log", encoding="utf-8", mode="w")])
qapp = QApplication(sys.argv)
qapp.setQuitOnLastWindowClosed(False)
A = appmod.App(qapp)


def pump(sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


def load(name):
    w = wave.open(f"{S}/wav/{name}.wav")
    return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def target_text():
    uia, U = selection._uia()
    el = uia.GetFocusedElement()
    p = el.GetCurrentPattern(U.UIA_TextPatternId)
    if not p:
        return None
    p = p.QueryInterface(U.IUIAutomationTextPattern)
    if TARGET == "notepad":
        return p.DocumentRange.GetText(-1).replace("\r", "\n")
    r = p.GetVisibleRanges()
    lines = "".join(r.GetElement(i).GetText(-1) for i in range(r.Length)).rstrip().splitlines()
    return lines[-1] if lines else ""


def say(name, wait=6.0):
    if foreground() != HWND:
        print("!! focus lost, aborting")
        sys.exit(1)
    audio = load(name)
    probe = selection.ContextProbe()
    A.worker.submit(A._transcribe_job, A.recognizer, audio, probe, HWND)
    pump(wait)
    menu = A.picker.title.text() if A.picker.isVisible() else ""
    print(f"[{name}] -> {target_text()!r}" + (f"   MENU: {menu} {A.picker.candidates}" if menu else ""))


# wait for everything to load
t0 = time.monotonic()
while time.monotonic() - t0 < 180:
    pump(0.5)
    if A.recognizer and A.second:
        break
print(f"loaded in {time.monotonic() - t0:.1f}s: asr={bool(A.recognizer)} second={bool(A.second)}")

def windows(cls):
    found = []
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, _):
        b = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(h, b, 128)
        if b.value == cls and user32.IsWindowVisible(h):
            found.append(h)
        return True
    user32.EnumWindows(PROC(cb), 0)
    return found


cls = "Notepad" if TARGET == "notepad" else "CASCADIA_HOSTING_WINDOW_CLASS"
before = set(windows(cls))
if TARGET == "notepad":
    proc = subprocess.Popen(["notepad.exe"])
else:
    proc = subprocess.Popen(["wt.exe", "-w", "new", "cmd.exe", "/k", "prompt $G"])
pump(3)
new = [h for h in windows(cls) if h not in before]
if new:
    h = new[0]
    user32.keybd_event(0x12, 0, 0, 0)       # Alt: lets SetForegroundWindow through the focus-steal guard
    user32.SetForegroundWindow(h)
    user32.keybd_event(0x12, 0, 2, 0)
    pump(1)
HWND = foreground()
buf = ctypes.create_unicode_buffer(256)
user32.GetWindowTextW(HWND, buf, 256)
print("target window:", buf.value)
if TARGET == "notepad" and "Notepad" not in buf.value and "記事本" not in buf.value:
    print("!! notepad not in front")
    sys.exit(1)
if TARGET == "notepad":
    # Win11 Notepad may restore old tabs: open a fresh one
    from pynput import keyboard
    kb = keyboard.Controller()
    with kb.pressed(keyboard.Key.ctrl):
        kb.press("n")
        kb.release("n")
    pump(1.5)

def select(word):
    uia, U = selection._uia()
    el = uia.GetFocusedElement()
    p = el.GetCurrentPattern(U.UIA_TextPatternId).QueryInterface(U.IUIAutomationTextPattern)
    r = p.DocumentRange.FindText(word, True, False)
    r.Select()
    A.session.dirty = True      # a real selection is made with a click or keys
    pump(0.5)
    print(f"[select {word}]")


for step in sys.argv[3].split(","):
    if step.startswith("sel:"):
        select(step[4:])
    else:
        say(step)

import os  # noqa: E402
out = subprocess.run(["powershell", "-NoProfile", "-Command",
                      "(Get-Process -Id %d).WorkingSet64/1MB" % os.getpid()],
                     capture_output=True, text=True)
print("memory MB:", out.stdout.split())
A.quit()
