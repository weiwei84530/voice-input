"""Read the selected text and its surrounding line in the focused app via Windows UI Automation.

Tested (2026-09-27): Chrome/Edge inputs, Win11 Notepad and LINE expose a TextPattern with the selection and
its line; the first read in LINE takes ~1.2s, later reads 5-50ms. Terminals expose the selection too, but
pasting there inserts at the prompt cursor instead of replacing it, so they are treated as "no selection".
"""
import ctypes
import ctypes.wintypes as wt
import logging
import re
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger("voiceinput")

_TERMINALS = {"windowsterminal.exe", "conhost.exe", "openconsole.exe", "wezterm-gui.exe", "alacritty.exe",
              "mintty.exe", "tabby.exe", "warp.exe"}
_LINE, _START, _END = 3, 0, 1
_local = threading.local()


@dataclass
class Selection:
    text: str
    before: str       # rest of the line before the selection
    after: str        # rest of the line after the selection
    app: str          # process name, e.g. chrome.exe
    rect: tuple | None = None   # selection bounds (x, y, w, h) in physical screen pixels


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
            return None
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
