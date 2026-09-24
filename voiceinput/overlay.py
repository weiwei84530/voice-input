"""Minimal always-on-top pill shown while recording / transcribing (Typeless-style)."""
import math
import sys
import time

from PySide6.QtCore import QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

W, H = 132, 40
BARS = 9


class Overlay(QWidget):
    IDLE, RECORDING, THINKING = range(3)

    def __init__(self, level_source):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(W, H)
        self._level_source = level_source   # callable -> float 0..1
        self._state = self.IDLE
        self._levels = [0.0] * BARS
        self._t0 = time.monotonic()
        self._timer = QTimer(self, interval=16, timeout=self._tick)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self, duration=140)
        self._fade.finished.connect(self._on_fade_done)

    # --- public API ---
    def show_recording(self):
        self._set_state(self.RECORDING)

    def show_thinking(self):
        self._set_state(self.THINKING)

    def hide_overlay(self):
        if self._state == self.IDLE:
            return
        self._state = self.IDLE
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    # --- internals ---
    def _set_state(self, state):
        was_hidden = self._state == self.IDLE or not self.isVisible()
        self._state = state
        self._t0 = time.monotonic()
        if was_hidden:
            self._reposition()
            self.setWindowOpacity(0.0)
            self.show()
            self._make_click_through()
            self._fade.stop()
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()
        self._timer.start()
        self.update()

    def _on_fade_done(self):
        if self._state == self.IDLE:
            self._timer.stop()
            self.hide()

    def _reposition(self):
        screen = QGuiApplication.screenAt(self.cursor().pos()) or QGuiApplication.primaryScreen()
        g = screen.availableGeometry()
        self.move(g.center().x() - W // 2, g.bottom() - H - 48)

    def _make_click_through(self):
        if sys.platform != "win32":
            return
        import ctypes
        hwnd = int(self.winId())
        GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TRANSPARENT, WS_EX_TOPMOST = -20, 0x08000000, 0x20, 0x8
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOPMOST)

    def _tick(self):
        if self._state == self.RECORDING:
            lvl = self._level_source()
            self._levels = self._levels[1:] + [lvl]
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, W - 1, H - 1), H / 2, H / 2)
        p.fillPath(path, QColor(20, 20, 22, 235))
        p.setPen(QColor(255, 255, 255, 30))
        p.drawPath(path)
        p.setPen(Qt.NoPen)

        t = time.monotonic() - self._t0
        cy = H / 2
        if self._state == self.RECORDING:
            # red dot + level bars
            p.setBrush(QColor(255, 69, 58))
            p.drawEllipse(QRectF(16, cy - 4, 8, 8))
            p.setBrush(QColor(255, 255, 255, 230))
            x0, gap, bw = 36, 9, 4
            for i, lvl in enumerate(self._levels):
                wobble = 0.12 + 0.06 * math.sin(t * 9 + i * 0.9)
                h = 4 + (H - 16) * max(wobble * 0.5, min(1.0, lvl))
                p.drawRoundedRect(QRectF(x0 + i * gap, cy - h / 2, bw, h), 2, 2)
        elif self._state == self.THINKING:
            # three pulsing dots
            for i in range(3):
                a = 0.5 + 0.5 * math.sin(t * 6 - i * 0.8)
                p.setBrush(QColor(255, 255, 255, int(90 + 165 * a)))
                r = 3 + 1.5 * a
                cx = W / 2 + (i - 1) * 16
                p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
        p.end()
