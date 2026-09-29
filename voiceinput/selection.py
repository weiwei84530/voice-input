"""Read the selected text and its surrounding line in the focused app via Windows UI Automation.

Tested (2026-09-27): Chrome/Edge inputs, Win11 Notepad and LINE expose a TextPattern with the selection and
its line; the first read in LINE takes ~1.2s, later reads 5-50ms. Terminals expose the selection too, but
pasting there inserts at the prompt cursor instead of replacing it, so they are treated as "no selection".
"""
import ctypes
import ctypes.wintypes as wt
import logging
import sys
import threading
from dataclasses import dataclass

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


class SelectionProbe:
    """Reads the selection on a background thread so a slow app does not delay the recording start."""

    def __init__(self):
        self._result: Selection | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self._result = read_selection()

    def result(self, timeout: float = 2.0) -> Selection | None:
        self._thread.join(timeout)
        return self._result
