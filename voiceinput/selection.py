"""Read the selected text and its surrounding line in the focused app via Windows UI Automation.

Tested (2026-09-27): Chrome/Edge inputs, Win11 Notepad and LINE expose a TextPattern with the selection and
its line; the first read in LINE takes ~1.2s, later reads 5-50ms.

Terminals (2026-09-30): a TUI such as herdr / Claude Code draws its own mouse selection, so Windows Terminal
reports none, and a paste goes to the app's cursor. A mouse drag there is watched instead (TermWatcher): herdr
copies the selection, and UIA RangeFromPoint maps the press and release points to the characters under them, so
the selection's exact place is known (a repeated word included). It is used only when it is on one line of
Claude Code's input box. To edit it, a click just right of its last character puts Claude Code's cursor there (a
drag does not move it) and Backspace removes it (Selection.click).
"""
import ctypes
import ctypes.wintypes as wt
import logging
import queue
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger("voiceinput")

_TERMINALS = {"windowsterminal.exe", "conhost.exe", "openconsole.exe", "wezterm-gui.exe", "alacritty.exe",
              "mintty.exe", "tabby.exe", "warp.exe"}
_CHAR, _LINE, _START, _END = 0, 3, 0, 1
_local = threading.local()


@dataclass
class Selection:
    text: str
    before: str       # rest of the line before the selection
    after: str        # rest of the line after the selection
    app: str          # process name, e.g. chrome.exe
    rect: tuple | None = None   # selection bounds (x, y, w, h) in physical screen pixels
    click: tuple | None = None  # terminal: screen point whose click puts the cursor right after the selection


def _proc_name(pid: int) -> str:
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wt.DWORD(260)
        k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        k32.CloseHandle(h)


def _uia():
    """One IUIAutomation per thread (COM objects are apartment-bound)."""
    if not hasattr(_local, "uia"):
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        _local.U = U
        _local.uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
    return _local.uia, _local.U


def read_selection() -> Selection | None:
    """The focused app's non-empty selection, or None (nothing selected, unsupported app, terminal)."""
    if sys.platform != "win32":
        return None
    try:
        uia, U = _uia()
        el = uia.GetFocusedElement()
        app = _proc_name(el.CurrentProcessId)
        if app in _TERMINALS or el.CurrentClassName == "TermControl":
            return term_watcher.selection()
        pattern = el.GetCurrentPattern(U.UIA_TextPatternId)
        if not pattern:
            return None
        pattern = pattern.QueryInterface(U.IUIAutomationTextPattern)
        ranges = pattern.GetSelection()
        if ranges.Length == 0:
            return None
        sel = ranges.GetElement(0)
        text = sel.GetText(-1)
        if not text.strip():
            return None
        line = sel.Clone()
        line.ExpandToEnclosingUnit(_LINE)
        before, after = line.Clone(), line.Clone()
        before.MoveEndpointByRange(_END, sel, _START)
        after.MoveEndpointByRange(_START, sel, _END)
        try:
            rects = sel.GetBoundingRectangles()
            rect = tuple(rects[:4]) if rects and len(rects) >= 4 else None
        except Exception:
            rect = None
        return Selection(text, before.GetText(-1), after.GetText(-1).rstrip("\r\n"), app, rect)
    except Exception:
        log.exception("reading the selection failed")
        return None


class TermWatcher:
    """Selections made by dragging the mouse in a terminal (see the module docstring). The mouse hook reports
    left button presses and releases; a drag is read on a worker thread and forgotten on the next key press or
    click, or when another window comes to the front."""
    _WAIT = 0.6        # seconds a drag may take to reach the clipboard
    _PROMPT = "❯"      # Claude Code's input line

    def __init__(self):
        self._lock = threading.Lock()
        self._gen = 0
        self._sel: Selection | None = None
        self._hwnd = 0
        self._ready = threading.Event()
        self._ready.set()
        self._down = None
        self._jobs: queue.Queue | None = None

    def forget(self):
        with self._lock:
            self._gen += 1
            self._sel = None
            self._ready.set()

    def mouse_down(self, x: int, y: int):
        self.forget()
        self._down = (x, y, ctypes.windll.user32.GetClipboardSequenceNumber())

    def mouse_up(self, x: int, y: int):
        down, self._down = self._down, None
        if down is None or abs(down[0] - x) + abs(down[1] - y) <= 4:
            return
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if _proc_name(_window_pid(hwnd)) not in _TERMINALS:
            return
        with self._lock:
            gen = self._gen
            self._ready.clear()
        if self._jobs is None:
            self._jobs = queue.Queue()
            threading.Thread(target=self._run, daemon=True).start()
        self._jobs.put((gen, hwnd, down[:2], (x, y), down[2]))

    def selection(self) -> Selection | None:
        """The current terminal selection; waits for a drag that is still being read."""
        self._ready.wait(self._WAIT + 0.5)
        with self._lock:
            if self._sel is not None and self._hwnd == ctypes.windll.user32.GetForegroundWindow():
                return self._sel
        return None

    def _run(self):
        while True:
            gen, hwnd, down, up, seq = self._jobs.get()
            sel = None
            try:
                sel = self._read(down, up, seq)
            except Exception:
                log.exception("reading the terminal selection failed")
            with self._lock:
                if gen == self._gen:
                    self._sel, self._hwnd = sel, hwnd
                    self._ready.set()

    def _read(self, down, up, seq) -> Selection | None:
        user32 = ctypes.windll.user32
        end = time.monotonic() + self._WAIT
        while user32.GetClipboardSequenceNumber() == seq:
            if time.monotonic() > end:
                return None       # nothing copied: no selection, or a terminal app that does not copy
            time.sleep(0.03)
        text = _clipboard_text()
        if not text or "\n" in text or "\r" in text:
            return None           # one line only (decided 2026-09-30)
        uia, U = _uia()
        ends = []
        for x, y in (down, up):
            pt = wt.POINT(x, y)
            el = uia.ElementFromPoint(pt)
            if el.CurrentClassName != "TermControl":
                return None
            pattern = el.GetCurrentPattern(U.UIA_TextPatternId).QueryInterface(U.IUIAutomationTextPattern)
            ch = pattern.RangeFromPoint(pt)
            ch.ExpandToEnclosingUnit(_CHAR)
            line = ch.Clone()
            line.ExpandToEnclosingUnit(_LINE)
            before = line.Clone()
            before.MoveEndpointByRange(_END, ch, _START)
            ends.append((len(before.GetText(-1)), line))
        (c1, line), (c2, line2) = ends
        if not line.Compare(line2):
            return None
        row = line.GetText(-1).rstrip("\r\n")
        a, b = min(c1, c2), max(c1, c2) + 1
        # a release past the end of the text selects blanks that the copy leaves out
        if row[a:a + len(text)] != text or row[a + len(text):b].strip():
            log.info("terminal selection %r does not match the screen %r", text, row[a:b])
            return None
        b = a + len(text)
        p = self._input_start(row, line, a)
        if p is None:
            log.info("terminal selection %r is not in Claude Code's input box", text)
            return None
        first, last = _char_rect(line, a), _char_rect(line, b - 1)
        rect = (first[0], first[1], last[0] + last[2] - first[0], first[3])
        click = (last[0] + last[2] + 3, last[1] + last[3] // 2)
        log.info("terminal selection %r after %r", text, row[p:a])
        return Selection(text, row[p:a], row[b:].rstrip(), "terminal", rect, click)

    def _input_start(self, row: str, line, a: int) -> int | None:
        """Where the text of Claude Code's input box starts on this row (after "❯ " or the continuation indent),
        or None when the row is outside the input box: it needs a rule (─) above its prompt line and one below
        its last line. A past prompt in the transcript also starts with ❯ but has no rules around it."""
        rows = [row[:a]] + _lines(line, -1, 30)
        i = next((k for k, r in enumerate(rows) if self._PROMPT in r), None)
        if i is None or i + 1 >= len(rows):
            return None
        col = rows[i].rindex(self._PROMPT)

        def blank(r):
            return r[col:col + 2].strip() == ""
        if not all(blank(r) for r in rows[:i]) or rows[i + 1][col:col + 1] != "─":
            return None
        for r in _lines(line, 1, 30):
            if r[col:col + 1] == "─":
                return col + 2
            if not blank(r):
                return None
        return None


term_watcher = TermWatcher()


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _lines(line, step: int, n: int) -> list[str]:
    """Up to n rows above (step -1) or below (step 1) a line range, nearest first."""
    r, out = line.Clone(), []
    for _ in range(n):
        if r.Move(_LINE, step) == 0:
            break
        r.ExpandToEnclosingUnit(_LINE)
        out.append(r.GetText(-1).rstrip("\r\n"))
    return out


def _char_rect(line, i: int) -> tuple:
    """Screen rect (x, y, w, h) of character i of a line range."""
    r = line.Clone()
    r.MoveEndpointByUnit(_START, _CHAR, i)
    r.MoveEndpointByRange(_END, r, _START)
    r.MoveEndpointByUnit(_END, _CHAR, 1)
    rect = r.GetBoundingRectangles()
    return tuple(int(v) for v in rect[:4])


def _clipboard_text() -> str:
    user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return ""
    try:
        h = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not h:
            return ""
        ptr = k32.GlobalLock(ctypes.c_void_p(h))
        try:
            return ctypes.wstring_at(ptr) if ptr else ""
        finally:
            k32.GlobalUnlock(ctypes.c_void_p(h))
    finally:
        user32.CloseClipboard()


# English terms on screen bias the recognizer toward them (worktree, LLM, sherpa-onnx in a terminal).
# Common words are left out: boosting them only risks inserting them.
_TERM = re.compile(r"(?<![A-Za-z0-9_.])[A-Za-z][A-Za-z0-9]*(?![A-Za-z0-9_]|\.[A-Za-z])")
_COMMON = set("""the and for you that this with from have are was were will would could should there their
what when where which while about after before again because being between both each into just like make
more most much must only other over same some such than then them they these those through under until very
your yours also been does done doing here how its itself our out own down off once all any can did don few
has had her him his not now nor too yes who whom why let may might new old use used using file files line
lines code text true false null none return print import class function def else elif self error value
name type data list open close save edit view help tools window select click press enter""".split())
_MAX_TERMS = 40


def terms_in(text: str) -> list[str]:
    """Distinctive English terms in text, most frequent first: acronyms, CamelCase and words with digits
    always; other words of 5+ letters only when they appear at least twice. Dotted names (ranges.GetText) are
    skipped: nobody says them aloud."""
    counts = Counter(m.group(0) for m in _TERM.finditer(text))
    out = []
    for w, n in counts.most_common():
        if len(w) < 3 or w.casefold() in _COMMON:
            continue
        strong = any(c.isupper() for c in w[1:]) or any(c.isdigit() for c in w)
        if strong or (len(w) >= 5 and n >= 2):
            out.append(w)
        if len(out) == _MAX_TERMS:
            break
    return out


def caret_rect() -> tuple | None:
    """The focused window's text caret (x, y, w, h) in physical screen pixels, from the Win32 caret (Notepad,
    classic edit boxes). Chrome and terminals draw their own caret and give None."""
    if sys.platform != "win32":
        return None

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                    ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
                    ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]
    user32 = ctypes.windll.user32
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(info)) or not info.hwndCaret:
        return None
    r = info.rcCaret
    pt = wt.POINT(r.left, r.top)
    user32.ClientToScreen(info.hwndCaret, ctypes.byref(pt))
    return pt.x, pt.y, max(1, r.right - r.left), max(1, r.bottom - r.top)


@dataclass
class Context:
    selection: Selection | None = None
    terms: list[str] = field(default_factory=list)


def read_context() -> Context:
    """The focused app's selection (see read_selection) and the English terms visible in it."""
    ctx = Context()
    if sys.platform != "win32":
        return ctx
    ctx.selection = read_selection()
    ctx.terms = terms_in(visible_text())
    return ctx


def visible_text() -> str:
    """The text visible in the focused control (UI Automation TextPattern), "" if it has none."""
    if sys.platform != "win32":
        return ""
    try:
        uia, U = _uia()
        pattern = uia.GetFocusedElement().GetCurrentPattern(U.UIA_TextPatternId)
        if not pattern:
            return ""
        ranges = pattern.QueryInterface(U.IUIAutomationTextPattern).GetVisibleRanges()
        return "\n".join(ranges.GetElement(i).GetText(20000) for i in range(min(ranges.Length, 50)))
    except Exception:
        log.exception("reading the visible text failed")
        return ""


class ContextProbe:
    """Reads the selection and on-screen terms on a background thread so a slow app does not delay the
    recording start."""

    def __init__(self):
        self._result = Context()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self._result = read_context()

    def result(self, timeout: float = 2.0) -> Context:
        self._thread.join(timeout)
        return self._result
