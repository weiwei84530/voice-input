"""IME-style candidate menu shown next to the selection (選字). It never takes focus, so the target app keeps
its selection and a click (or saying 第二個) pastes the choice straight over it."""
import sys

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

_STYLE = """
QWidget#picker { background: #202024; border: 1px solid #3a3a40; border-radius: 8px; }
QLabel { color: #9a9aa2; padding: 2px 4px; }
QPushButton { color: #f2f2f2; background: transparent; border: none; border-radius: 5px;
              padding: 5px 10px; text-align: left; font-size: 15px; }
QPushButton:hover { background: #3a3a44; }
QPushButton#close { color: #9a9aa2; padding: 2px 6px; font-size: 13px; }
"""


class Picker(QWidget):
    picked = Signal(int)      # 0-based row

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.candidates: list[str] = []
        self._timer = QTimer(self, singleShot=True, interval=30_000, timeout=self.hide)

        self.frame = QWidget(self, objectName="picker")
        self.frame.setStyleSheet(_STYLE)
        self.title = QLabel()
        close = QPushButton("✕", objectName="close", clicked=self.hide)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.title, 1)
        head.addWidget(close)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        inner = QVBoxLayout(self.frame)
        inner.setContentsMargins(6, 4, 6, 6)
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
        if candidates:
            self._fill(f"選字：{word}　點選或說「第幾個」", candidates)
        else:
            self._fill(f"選字：{word}　找不到同音的候選", [])
        if self.isVisible():
            self.adjustSize()
            self._clamp()

    def _fill(self, title: str, candidates: list[str]):
        self.title.setText(title)
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            w.deleteLater()
        for i, word in enumerate(candidates):
            self.rows.addWidget(QPushButton(f"{i + 1}　{word}", clicked=lambda _=False, i=i: self._pick(i)))
        self.adjustSize()
        self._timer.start()

    def _pick(self, i: int):
        self.hide()
        self.picked.emit(i)

    def _place(self, rect):
        """rect: selection bounds in physical screen pixels (x, y, w, h) from UI Automation, or None."""
        if rect:
            x, y, w, h = rect
            pos = self._to_logical(QPoint(int(x), int(y + h)))
        else:
            pos = QCursor.pos()
        self.move(pos + QPoint(0, 6))
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
        super().hideEvent(e)
