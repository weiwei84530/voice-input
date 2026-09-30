"""Small dark menu that never takes focus, so the target app keeps its caret and selection.

Two uses: a list of choices (選字 candidates, a suspected mishearing, a context hotword question) and a yes/no box
asking whether to add a hotword. Both are answered with the mouse only (decided 2026-09-29: short spoken answers
like 對 / 第二個 were often misheard). A thin bar on the bottom edge shrinks as the timeout runs out."""
import sys
import time

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStyle, QStyleOption, QVBoxLayout, QWidget

_STYLE = """
QWidget#picker { background: #202024; border: 1px solid #3a3a40; border-radius: 8px; }
QLabel { color: #9a9aa2; padding: 2px 4px; font-size: 12px; }
QLabel#text { color: #f2f2f2; font-size: 14px; }
QPushButton { color: #f2f2f2; background: transparent; border: none; border-radius: 5px;
              padding: 4px 10px; text-align: left; font-size: 15px; }
QPushButton:hover { background: #3a3a44; }
QPushButton#keep { color: #9a9aa2; font-size: 13px; }
QPushButton#close { color: #9a9aa2; padding: 2px 6px; font-size: 12px; }
QPushButton#tick { color: #7fd6a4; font-size: 16px; font-weight: bold; padding: 2px 10px; text-align: center; }
QPushButton#tick:hover { background: #2c4a3a; }
"""
_MAX_WIDTH = 360
_RADIUS = 8
_BAR = 3                              # countdown bar height (px)
_BAR_COLOR = QColor(200, 200, 206)


class _Frame(QWidget):
    """The menu's rounded panel, with the countdown bar painted along its bottom edge."""

    def __init__(self, parent):
        super().__init__(parent, objectName="picker")
        self.remaining = 0.0          # fraction of the timeout left, 0 hides the bar

    def paintEvent(self, e):
        opt = QStyleOption()
        opt.initFrom(self)
        p = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, opt, p, self)   # the stylesheet background and border
        if self.remaining > 0:
            p.setRenderHint(QPainter.Antialiasing)
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), _RADIUS - 1, _RADIUS - 1)
            p.setClipPath(clip)
            w = (self.width() - 2) * self.remaining
            p.fillRect(QRectF(1, self.height() - 1 - _BAR, w, _BAR), _BAR_COLOR)
        p.end()


class Picker(QWidget):
    picked = Signal(int)      # 0-based row
    confirmed = Signal()      # ✓ clicked in a yes/no box
    declined = Signal()       # the "keep" row clicked in a list

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.candidates: list[str] = []
        self.timeout_ms = 10_000    # every menu closes after this (a setting)
        self._timer = QTimer(self, singleShot=True, interval=self.timeout_ms, timeout=self.hide)
        self._tick = QTimer(self, interval=40, timeout=self._countdown)
        self._deadline = self._total = 0.0

        self.frame = _Frame(self)
        self.frame.setStyleSheet(_STYLE)
        self.frame.setMaximumWidth(_MAX_WIDTH)
        self.title = QLabel()
        close = QPushButton("✕", objectName="close", clicked=self.hide)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.title, 1)
        head.addWidget(close, 0, Qt.AlignTop)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        inner = QVBoxLayout(self.frame)
        inner.setContentsMargins(6, 4, 6, 6 + _BAR)
        inner.setSpacing(2)
        inner.addLayout(head)
        inner.addLayout(self.rows)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)

    def show_loading(self, word: str, rect):
        self._fill(f"選字：{word}　候選產生中…", [])
        self._place(rect)

    def show_candidates(self, word: str, candidates: list[str]):
        self.candidates = candidates
        self._fill(f"選字：{word}" if candidates else f"選字：{word}　找不到同音的候選", candidates)
        if self.isVisible():
            self._clamp()

    def show_list(self, title: str, words: list[str], rect, keep: str = ""):
        """A list of choices with its own title. rect as in _place; None puts it above the recording indicator.
        keep adds a last row that emits declined (e.g. 保留「城市」)."""
        self.candidates = words
        self._fill(title, words)
        if keep:
            self.rows.addWidget(QPushButton(keep, objectName="keep", clicked=self._decline))
            self._shrink()
        self._start_timer(self.timeout_ms)
        self._place(rect, bottom=True)

    def show_confirm(self, title: str, text: str, rect):
        """A yes/no box: title, text and a ✓ button. Only a click on ✓ confirms; ✕ or the timeout declines."""
        self.candidates = []
        self._clear()
        self.title.setText(title)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 0, 0, 0)
        h.addWidget(QLabel(text, objectName="text"), 1)
        h.addWidget(QPushButton("✓", objectName="tick", clicked=self._confirm))
        self.rows.addWidget(row)
        self._shrink()
        self._start_timer(self.timeout_ms)
        self._place(rect, bottom=True)

    def _start_timer(self, ms: int):
        self._timer.start(ms)
        self._total = ms / 1000
        self._deadline = time.monotonic() + self._total
        self._tick.start()
        self._countdown()

    def _countdown(self):
        left = self._deadline - time.monotonic()
        self.frame.remaining = max(0.0, left / self._total) if self._total else 0.0
        self.frame.update()
        if left <= 0:
            self._tick.stop()

    def physical_rect(self) -> tuple[int, int, int, int]:
        """(left, top, right, bottom) in physical pixels, for telling clicks on the menu from clicks elsewhere."""
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        g, r = screen.geometry(), screen.devicePixelRatio()
        x = g.x() + (self.x() - g.x()) * r
        y = g.y() + (self.y() - g.y()) * r
        return int(x), int(y), int(x + self.width() * r), int(y + self.height() * r)

    def _clear(self):
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            w.hide()               # removed now, not at deleteLater: the old rows kept the window large
            w.setParent(None)
            w.deleteLater()

    def _shrink(self):
        self.frame.layout().activate()
        self.layout().activate()
        self.resize(self.sizeHint())

    def _fill(self, title: str, candidates: list[str]):
        self._clear()
        self.title.setText(title)
        for i, word in enumerate(candidates):
            self.rows.addWidget(QPushButton(word, clicked=lambda _=False, i=i: self._pick(i)))
        self._shrink()
        self._start_timer(self.timeout_ms)

    def _pick(self, i: int):
        self.hide()
        self.picked.emit(i)

    def _decline(self):
        self.hide()
        self.declined.emit()

    def _confirm(self):
        self.hide()
        self.confirmed.emit()

    def _place(self, rect, bottom: bool = False):
        """rect: selection bounds in physical screen pixels (x, y, w, h) from UI Automation, or None (then next
        to the mouse, or with bottom=True above the recording indicator at the bottom of the screen)."""
        if rect:
            x, y, w, h = rect
            pos = self._to_logical(QPoint(int(x), int(y + h))) + QPoint(0, 6)
        elif bottom:
            screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            g = screen.availableGeometry()
            pos = QPoint(g.center().x() - self.width() // 2, g.bottom() - 110 - self.height())
        else:
            pos = QCursor.pos() + QPoint(0, 6)
        self.move(pos)
        self.show()
        self._no_activate()
        self._clamp()

    @staticmethod
    def _to_logical(p: QPoint) -> QPoint:
        # Qt keeps each screen's origin in device pixels and scales only within the screen
        for s in QGuiApplication.screens():
            g, r = s.geometry(), s.devicePixelRatio()
            if g.x() <= p.x() < g.x() + g.width() * r and g.y() <= p.y() < g.y() + g.height() * r:
                return QPoint(g.x() + int((p.x() - g.x()) / r), g.y() + int((p.y() - g.y()) / r))
        return p

    def _clamp(self):
        screen = QGuiApplication.screenAt(self.pos()) or QGuiApplication.primaryScreen()
        a = screen.availableGeometry()
        x = min(max(self.x(), a.left()), a.right() - self.width())
        y = min(max(self.y(), a.top()), a.bottom() - self.height())
        self.move(x, y)

    def _no_activate(self):
        if sys.platform != "win32":
            return
        import ctypes
        hwnd = int(self.winId())
        GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOPMOST = -20, 0x08000000, 0x8
        user32 = ctypes.windll.user32
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                              | WS_EX_NOACTIVATE | WS_EX_TOPMOST)

    def hideEvent(self, e):
        self._timer.stop()
        self._tick.stop()
        super().hideEvent(e)
