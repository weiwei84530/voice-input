"""Minimal always-on-top pill shown while recording / transcribing / rewriting (Typeless-style)."""
import math
import sys
import time

from PySide6.QtCore import QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

W, H = 132, 40
BARS = 9
CAPTION_W, CAPTION_H = 560, 34


def _click_through(widget):
    if sys.platform != "win32":
        return
    import ctypes
    hwnd = int(widget.winId())
    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TRANSPARENT, WS_EX_TOPMOST = -20, 0x08000000, 0x20, 0x8
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOPMOST)


class Caption(QWidget):
    """Live transcript above the pill while recording (streaming ASR). Shows the end of the text."""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(CAPTION_W, CAPTION_H)
        self._text = ""
        self._font = QFont()
        self._font.setPixelSize(16)

    def set_text(self, text: str, pill_x: int, pill_y: int):
        if text == self._text and self.isVisible():
            return
        self._text = text
        if not text:
            self.hide()
            return
        self.move(pill_x + W // 2 - CAPTION_W // 2, pill_y - CAPTION_H - 8)
        if not self.isVisible():
            self.show()
            _click_through(self)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(self._font)
        fm = QFontMetrics(self._font)
        text = fm.elidedText(self._text, Qt.ElideLeft, CAPTION_W - 28)
        tw = fm.horizontalAdvance(text) + 28
        x = (CAPTION_W - tw) / 2
        path = QPainterPath()
        path.addRoundedRect(QRectF(x + 0.5, 0.5, tw - 1, CAPTION_H - 1), 10, 10)
        p.fillPath(path, QColor(20, 20, 22, 225))
        p.setPen(QColor(240, 240, 240))
        p.drawText(QRectF(x, 0, tw, CAPTION_H), Qt.AlignCenter, text)
        p.end()


class Overlay(QWidget):
    IDLE, RECORDING, THINKING, REWRITING = range(4)

    def __init__(self, level_source, caption_source=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(W, H)
        self._level_source = level_source   # callable -> float 0..1
        self._caption_source = caption_source   # callable -> str (live transcript) or None
        self.caption = Caption()
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

    def show_rewriting(self):
        self._set_state(self.REWRITING)

    def hide_overlay(self):
        if self._state == self.IDLE:
            return
        self._state = self.IDLE
        self.caption.set_text("", 0, 0)
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
        _click_through(self)

    def _tick(self):
        if self._state == self.RECORDING:
            lvl = self._level_source()
            self._levels = self._levels[1:] + [lvl]
        if self._caption_source is not None and self._state in (self.RECORDING, self.THINKING):
            self.caption.set_text(self._caption_source(), self.x(), self.y())
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
        elif self._state == self.REWRITING:
            # a dot orbiting a faint ring, with a fading tail
            cx, ring = W / 2, 9
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 40), 1.5))
            p.drawEllipse(QRectF(cx - ring, cy - ring, 2 * ring, 2 * ring))
            p.setPen(Qt.NoPen)
            for k in range(6):
                a = t * 7 - k * 0.32
                r = 3.2 - k * 0.4
                p.setBrush(QColor(255, 255, 255, int(255 * (1 - k / 6))))
                x, y = cx + ring * math.cos(a), cy + ring * math.sin(a)
                p.drawEllipse(QRectF(x - r, y - r, 2 * r, 2 * r))
        p.end()
