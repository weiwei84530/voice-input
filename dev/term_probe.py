"""Probe: can a mouse drag in a terminal (herdr / Claude Code in Windows Terminal) be mapped to a text position?

Listens to the left mouse button globally. After each drag it prints, via UI Automation RangeFromPoint, the
character and line under the press and release points (with the column inside that line), what Windows
Terminal reports as its own selection, and the clipboard (herdr copies on select). Does not touch VoiceInput.

    .venv\\Scripts\\python.exe dev\\term_probe.py [log file] [--click]

--click: 2s after each drag, click just right of the selection's last character (a click only, no typing) to
see where the terminal app puts its cursor.
"""
import ctypes
import ctypes.wintypes as wt
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voiceinput.selection import _uia, _proc_name  # noqa: E402

ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # physical pixels, like UIA
_CHAR, _LINE, _START, _END = 0, 3, 0, 1
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
out = open(sys.argv[1], "a", encoding="utf-8") if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
events: queue.Queue = queue.Queue()
CLICK = "--click" in sys.argv


def say(*parts):
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    if out:
        out.write(line + "\n")
        out.flush()


def clipboard() -> str:
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalLock.restype = ctypes.c_void_p
    u.GetClipboardData.restype = ctypes.c_void_p
    if not u.OpenClipboard(None):
        return "<busy>"
    try:
        h = u.GetClipboardData(13)  # CF_UNICODETEXT
        if not h:
            return ""
        p = k.GlobalLock(ctypes.c_void_p(h))
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(ctypes.c_void_p(h))
    finally:
        u.CloseClipboard()


def at_point(uia, U, x, y):
    """(char, column, line text) under a screen point, or an error string."""
    pt = wt.POINT(x, y)
    el = uia.ElementFromPoint(pt)
    pat = el.GetCurrentPattern(U.UIA_TextPatternId)
    if not pat:
        return f"<no TextPattern: {el.CurrentClassName} / {_proc_name(el.CurrentProcessId)}>"
    pat = pat.QueryInterface(U.IUIAutomationTextPattern)
    r = pat.RangeFromPoint(pt)
    ch = r.Clone()
    ch.ExpandToEnclosingUnit(_CHAR)
    line = r.Clone()
    line.ExpandToEnclosingUnit(_LINE)
    before = line.Clone()
    before.MoveEndpointByRange(_END, ch, _START)
    return ch.GetText(-1), len(before.GetText(-1)), line.GetText(-1).rstrip("\r\n")


def wt_selection(uia, U):
    try:
        pat = uia.GetFocusedElement().GetCurrentPattern(U.UIA_TextPatternId)
        if not pat:
            return "<no TextPattern>"
        ranges = pat.QueryInterface(U.IUIAutomationTextPattern).GetSelection()
        return [ranges.GetElement(i).GetText(-1) for i in range(ranges.Length)]
    except Exception as e:
        return f"<{e}>"


def worker():
    uia, U = _uia()
    n = 0
    while True:
        down, up, clip_before = events.get()
        time.sleep(0.25)  # let the terminal copy the selection
        n += 1
        drag = abs(down[0] - up[0]) + abs(down[1] - up[1]) > 4
        say(f"\n#{n} {'drag' if drag else 'click'} {down} -> {up}")
        for name, (x, y) in (("down", down), ("up", up)):
            try:
                res = at_point(uia, U, x, y)
                if isinstance(res, str):
                    say(f"  {name}: {res}")
                else:
                    ch, col, line = res
                    say(f"  {name}: char={ch!r} col={col}")
                    say(f"        line={line!r}")
                    say(f"        before={line[:col]!r}")
            except Exception as e:
                say(f"  {name}: <error {e}>")
        try:
            say(f"  colors: {colors(uia, U, *up)}")
        except Exception as e:
            say(f"  colors: <error {e}>")
        say(f"  WT selection: {wt_selection(uia, U)!r}")
        clip = clipboard()
        say(f"  clipboard: {clip!r}{'' if clip != clip_before else '  (unchanged)'}")
        if CLICK and drag:
            try:
                click_after(uia, U, down, up)
            except Exception as e:
                say(f"  click: <error {e}>")


def colors(uia, U, x, y):
    """Runs of (start column, text, background, foreground) on the line under a point: a TUI's own selection
    highlight shows up here."""
    pt = wt.POINT(x, y)
    pat = uia.ElementFromPoint(pt).GetCurrentPattern(U.UIA_TextPatternId)
    line = pat.QueryInterface(U.IUIAutomationTextPattern).RangeFromPoint(pt)
    line.ExpandToEnclosingUnit(_LINE)
    text = line.GetText(-1).rstrip("\r\n")
    runs = []
    for i, c in enumerate(text):
        r = line.Clone()
        r.MoveEndpointByUnit(_START, _CHAR, i)
        r.MoveEndpointByRange(_END, r, _START)
        r.MoveEndpointByUnit(_END, _CHAR, 1)
        key = (f"{r.GetAttributeValue(U.UIA_BackgroundColorAttributeId):06x}",
               f"{r.GetAttributeValue(U.UIA_ForegroundColorAttributeId):06x}")
        if runs and runs[-1][2:] == key:
            runs[-1][1] += c
        else:
            runs.append([i, c, *key])
    return [tuple(r) for r in runs if r[1].strip()]


def char_rect(uia, U, x, y):
    pt = wt.POINT(x, y)
    pat = uia.ElementFromPoint(pt).GetCurrentPattern(U.UIA_TextPatternId)
    ch = pat.QueryInterface(U.IUIAutomationTextPattern).RangeFromPoint(pt)
    ch.ExpandToEnclosingUnit(_CHAR)
    r = ch.GetBoundingRectangles()
    return tuple(int(v) for v in r[:4])


def click_after(uia, U, down, up):
    """Click just right of the selection's last character (the next cell), then put the mouse back."""
    for x, y in (down, up):
        if uia.ElementFromPoint(wt.POINT(x, y)).CurrentClassName != "TermControl":
            say("  click: skipped, not a terminal")
            return
    a, b = char_rect(uia, U, *down), char_rect(uia, U, *up)
    last = max(a, b, key=lambda r: (r[1] + r[3] // 2, r[0]))
    x, y = last[0] + last[2] + 3, last[1] + last[3] // 2
    say(f"  last char rect={last} -> clicking at {(x, y)} in 2s")
    time.sleep(2)
    u = ctypes.windll.user32
    old = wt.POINT()
    u.GetCursorPos(ctypes.byref(old))
    u.SetCursorPos(x, y)
    u.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    u.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    time.sleep(0.05)
    u.SetCursorPos(old.x, old.y)
    say("  clicked")


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
state = {}


@HOOKPROC
def hook(code, wparam, lparam):
    if code == 0:
        m = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if m.flags & 1:  # LLMHF_INJECTED: our own test click
            pass
        elif wparam == 0x201:  # WM_LBUTTONDOWN
            state["down"] = (m.pt.x, m.pt.y)
            state["clip"] = None
            threading.Thread(target=lambda: state.__setitem__("clip", clipboard()), daemon=True).start()
        elif wparam == 0x202 and "down" in state:  # WM_LBUTTONUP
            events.put((state.pop("down"), (m.pt.x, m.pt.y), state.get("clip")))
    return ctypes.windll.user32.CallNextHookEx(None, code, wparam, ctypes.c_void_p(lparam))


def main():
    threading.Thread(target=worker, daemon=True).start()
    u = ctypes.windll.user32
    u.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
    u.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, ctypes.c_void_p)
    h = u.SetWindowsHookExW(14, hook, None, 0)  # WH_MOUSE_LL
    if not h:
        raise SystemExit("hook failed")
    say(f"--- term_probe started {time.strftime('%H:%M:%S')}: drag to select text; Ctrl+C to stop")
    msg = wt.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(msg))
        u.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
