"""Read the selected text and its surrounding line in the focused app via Windows UI Automation.

Tested (2026-09-27): Chrome/Edge inputs, Win11 Notepad and LINE expose a TextPattern with the selection and
its line; the first read in LINE takes ~1.2s, later reads 5-50ms.

Terminals (2026-09-30, keyboard only since 2026-10-02): a TUI such as herdr / Claude Code / Codex / agy draws its
own mouse selection, so Windows Terminal reports none, and a paste goes to the app's cursor. A mouse drag there is
watched instead (TermWatcher): UIA RangeFromPoint maps the press and release points to the characters under them,
and the text between them is read from the screen (no clipboard: herdr's copy_on_select may be off), so the
selection's exact place is known (a repeated word included). It is used only on one line of the input box the
terminal cursor is in. To edit it, arrow keys move the app's cursor right after it, re-reading the cursor after
each burst (move_cursor_after), and Backspace removes it. Windows Terminal reports its cursor as an empty UIA
selection.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import queue
import re
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
    term: tuple | None = None   # terminal: screen point of the selection's last character


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
    # an input box's prompt (Claude Code ❯, Codex ›, agy >) at the pane's left edge
    _PROMPT = re.compile(r" {0,2}([❯›>])(?=[ \xa0])")

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
        self._down = (x, y)

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
        self._jobs.put((gen, hwnd, down, (x, y)))

    def selection(self) -> Selection | None:
        """The current terminal selection; waits for a drag that is still being read."""
        self._ready.wait(1.0)
        with self._lock:
            if self._sel is not None and self._hwnd == ctypes.windll.user32.GetForegroundWindow():
                return self._sel
        return None

    def _run(self):
        while True:
            gen, hwnd, down, up = self._jobs.get()
            sel = None
            try:
                sel = self._read(down, up)
            except Exception:
                log.exception("reading the terminal selection failed")
            with self._lock:
                if gen == self._gen:
                    self._sel, self._hwnd = sel, hwnd
                    self._ready.set()

    def _read(self, down, up) -> Selection | None:
        uia, U = _uia()
        ends = []
        for x, y in (down, up):
            pattern = _term_pattern(x, y)
            if pattern is None:
                return None
            ch = pattern.RangeFromPoint(wt.POINT(x, y))
            ch.ExpandToEnclosingUnit(_CHAR)
            ends.append(_pos(ch))
        (line, c1), (line2, c2) = ends
        if not line.Compare(line2):
            return None           # one line only (decided 2026-09-30)
        row = line.GetText(-1).rstrip("\r\n")
        a, b = _highlighted(line, min(c1, c2), max(c1, c2) + 1, len(row))
        text = row[a:b]
        a += len(text) - len(text.lstrip())   # a drag past the text selects blanks
        text = text.strip()
        if not text:
            return None
        b = a + len(text)
        p = self._input_start(row, line, a, _term_cursor(pattern))
        if p is None:
            log.info("terminal selection %r is not in the input box", text)
            return None
        first, last = _char_rect(line, a), _char_rect(line, b - 1)
        rect = (first[0], first[1], last[0] + last[2] - first[0], first[3])
        point = (last[0] + last[2] // 2, last[1] + last[3] // 2)
        log.info("terminal selection %r after %r (drag %s)", text, row[p:a], "right" if c2 >= c1 else "left")
        return Selection(text, row[p:a], row[b:].rstrip(), "terminal", rect, point)

    def _input_start(self, row: str, line, a: int, cursor) -> int | None:
        """Where the text of the input box starts on this row (after the prompt or the continuation indent), or
        None when the row is outside the input box. The box is a prompt row at the pane's left edge and the
        non-empty rows below it that are blank under the prompt; the terminal cursor must be in it (a past prompt
        in the transcript also starts with the prompt, but the cursor is not there)."""
        if cursor is None:
            return None
        left = max(row.rfind("│", 0, a), row.rfind("┃", 0, a)) + 1   # herdr's sidebar or a pane on the left
        right = min((i for i in (row.find("│", a), row.find("┃", a)) if i >= 0), default=len(row))
        rows = [row[:a]] + _lines(line, -1, 30)
        i = next((k for k, r in enumerate(rows) if self._PROMPT.match(r, left)), None)
        if i is None:
            return None
        col = self._PROMPT.match(rows[i], left).start(1)

        def inside(r):
            return r[left:col + 2].strip() == "" and r[left:right].strip() != ""
        if i and (rows[0][left:col + 2].strip() or not all(inside(r) for r in rows[1:i])):
            return None
        dr = _rows_between(line, cursor[0])
        if dr is None or dr < -i or (dr > 0 and not all(inside(r) for r in _lines(line, 1, dr))):
            return None
        return col + 2 if a >= col + 2 else None


term_watcher = TermWatcher()


def move_cursor_after(sel: Selection) -> bool:
    """Terminal: move the app's cursor right after sel with arrow keys. Up/Down first, then Left/Right, reading
    the cursor again after each burst, so soft-wrapped rows and the cursor's starting place do not matter.
    False (nothing deleted yet) when the text is no longer there or the cursor does not get there."""
    out = []
    t = threading.Thread(target=lambda: out.append(_move_after(sel)), daemon=True)
    t.start()
    t.join(4.0)
    return bool(out and out[0])


def _move_after(sel: Selection) -> bool:
    from .output import ARROWS, press_key
    try:
        pattern = _term_pattern(*sel.term)
        if pattern is None:
            return False
        ch = pattern.RangeFromPoint(wt.POINT(*sel.term))
        ch.ExpandToEnclosingUnit(_CHAR)
        line, col = _pos(ch)
        col += 1
        if line.GetText(-1)[col - len(sel.text):col] != sel.text:
            log.info("terminal selection %r is no longer on screen", sel.text)
            return False
        end = time.monotonic() + 3.0
        cur = _term_cursor(pattern)
        if cur is not None and cur[1] == col and _rows_between(cur[0], line) == 0:
            # A drag ending here left the cursor here, and Codex keeps that selection: a Backspace would delete
            # all of it, the next one a character more. An arrow key clears it; the loop moves back.
            press_key(ARROWS["left"], 1)
            cur = _cursor_moved(pattern, cur, time.monotonic() + 0.5)
        for _ in range(12):
            if cur is None or time.monotonic() > end:
                break
            dr = _rows_between(cur[0], line)
            log.info("terminal cursor: %s rows, column %d (target %d)", dr, cur[1], col)
            if dr is None:
                break
            if dr == 0 and cur[1] == col:
                return True
            if dr:
                press_key(ARROWS["down" if dr > 0 else "up"], abs(dr))
            else:
                press_key(ARROWS["right" if col > cur[1] else "left"], abs(col - cur[1]))
            cur = _cursor_moved(pattern, cur, end)
        log.info("could not move the terminal cursor after %r", sel.text)
    except Exception:
        log.exception("moving the terminal cursor failed")
    return False


def _term_pattern(x: int, y: int):
    """The TextPattern of the terminal under a screen point, or None."""
    uia, U = _uia()
    el = uia.ElementFromPoint(wt.POINT(x, y))
    if el.CurrentClassName != "TermControl":
        return None
    return el.GetCurrentPattern(U.UIA_TextPatternId).QueryInterface(U.IUIAutomationTextPattern)


def _term_cursor(pattern):
    """(line range, column) of the terminal's cursor, or None. Windows Terminal reports it as an empty
    selection. (While its window is in the background it was reported one character to the left.)"""
    ranges = pattern.GetSelection()
    if ranges.Length != 1:
        return None
    r = ranges.GetElement(0)
    if r.GetText(-1):
        return None               # the terminal's own selection
    return _pos(r)


def _cursor_moved(pattern, old, end: float):
    """The cursor once it has moved from old and stayed put for a moment (the keys may arrive in parts)."""
    def same(a, b):
        return a is not None and b is not None and a[1] == b[1] and a[0].Compare(b[0])
    last, still = old, 0.0
    while time.monotonic() < end:
        time.sleep(0.02)
        cur = _term_cursor(pattern)
        if same(cur, last):
            if not same(cur, old):
                still += 0.02
                if still >= 0.06:
                    return cur
        else:
            last, still = cur, 0.0
    return last


def _pos(rng):
    """(line range, column) of a range's start."""
    line = rng.Clone()
    line.ExpandToEnclosingUnit(_LINE)
    before = line.Clone()
    before.MoveEndpointByRange(_END, rng, _START)
    return line, len(before.GetText(-1))


def _rows_between(a, b) -> int | None:
    """Signed number of rows from line range a down to line range b (None when more than 60 apart)."""
    if a.Compare(b):
        return 0
    step = 1 if a.CompareEndpoints(_START, b, _START) < 0 else -1
    r = a.Clone()
    for n in range(1, 61):
        if r.Move(_LINE, step) == 0:
            return None
        r.ExpandToEnclosingUnit(_LINE)
        if r.Compare(b):
            return n * step
    return None


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


def _char(line, i: int):
    """Range of character i of a line range."""
    r = line.Clone()
    r.MoveEndpointByUnit(_START, _CHAR, i)
    r.MoveEndpointByRange(_END, r, _START)
    r.MoveEndpointByUnit(_END, _CHAR, 1)
    return r


def _char_rect(line, i: int) -> tuple:
    """Screen rect (x, y, w, h) of character i of a line range."""
    rect = _char(line, i).GetBoundingRectangles()
    return tuple(int(v) for v in rect[:4])


def _highlighted(line, a: int, b: int, n: int) -> tuple[int, int]:
    """Narrow characters a..b-1 (those under the press and release points and between them) to what the TUI
    highlighted. herdr selects up to the cell edge nearest the pointer, so the character under an end point is
    often not selected; it inverts the colors of selected cells, and Windows Terminal reports cell colors via
    UIA. Ends colored like the characters just outside the drag are dropped (2026-10-03)."""
    _, U = _uia()

    def style(i):
        r = _char(line, i)
        return (r.GetAttributeValue(U.UIA_BackgroundColorAttributeId),
                r.GetAttributeValue(U.UIA_ForegroundColorAttributeId))
    try:
        styles = [style(i) for i in range(a, b)]
        if len(set(styles)) < 2:
            return a, b           # nothing to tell apart
        plain = {style(i) for i in (a - 1, b) if 0 <= i < n}
        while a < b and styles[0] in plain:
            a, styles = a + 1, styles[1:]
        while a < b and styles[-1] in plain:
            b, styles = b - 1, styles[:-1]
    except Exception:
        log.exception("reading the terminal highlight failed")
    return a, b


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
    ctx.selection = read_selection()
    ctx.terms = terms_in(visible_text())
    return ctx


def visible_text() -> str:
    """The text visible in the focused control (UI Automation TextPattern), "" if it has none."""
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
