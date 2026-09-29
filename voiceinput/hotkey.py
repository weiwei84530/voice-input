"""Global push-to-talk hotkey via pynput.

On Windows the hotkey is suppressed with a low-level hook so holding CapsLock
does not toggle caps state. A short tap (< TAP_THRESHOLD) is replayed as a
normal key press, so CapsLock still works as CapsLock when tapped.

A double tap (second press within DOUBLE_TAP_GAP of a tap) calls on_double() before on_press(): the app undoes
the last dictation, and holding the second press records again. Two replayed taps cancel out; when the second
press is held (no replay) one more tap is replayed so the first tap's toggle is undone.
"""
import sys
import threading
import time

from pynput import keyboard

TAP_THRESHOLD = 0.3  # seconds; shorter presses are treated as a normal tap
DOUBLE_TAP_GAP = 0.35  # seconds from a tap's release to the next press to count as a double tap

HOTKEYS = {
    "caps_lock": ("CapsLock", keyboard.Key.caps_lock),
    "alt_r": ("右 Alt", keyboard.Key.alt_r),
    "ctrl_r": ("右 Ctrl", keyboard.Key.ctrl_r),
    "f12": ("F12", keyboard.Key.f12),
}

# Windows virtual-key codes for the hook-level filter
_WIN_VK = {"caps_lock": 0x14, "alt_r": 0xA5, "ctrl_r": 0xA3, "f12": 0x7B}
_WM_KEYDOWN, _WM_SYSKEYDOWN = 0x0100, 0x0104
_LLKHF_INJECTED = 0x10


class PushToTalk:
    """Calls on_press() when the hotkey goes down and on_release(held_seconds) when it goes up."""

    def __init__(self, key_name: str, on_press, on_release, on_other_key=None, on_double=None):
        self.on_press = on_press
        self.on_release = on_release
        self.on_double = on_double
        self.on_other_key = on_other_key   # on_other_key(vk): any other key the user pressed (Windows only)
        self._key_name = key_name
        self._down_at = None
        self._tap_up_at = None      # release time of the last short tap (double-tap detection)
        self._double = False        # the current press is the second of a double tap
        self._listener = None
        self._controller = keyboard.Controller()
        self._replaying = False

    def set_key(self, key_name: str) -> None:
        self._key_name = key_name
        self._down_at = None

    def start(self) -> None:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["win32_event_filter"] = self._win32_filter
            self._listener = keyboard.Listener(**kwargs)
        else:
            self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()

    # --- Windows: handle everything inside the hook so we can suppress the key ---
    def _win32_filter(self, msg, data):
        if data.flags & _LLKHF_INJECTED:
            return True
        if data.vkCode != _WIN_VK.get(self._key_name):
            if self.on_other_key and msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                self.on_other_key(data.vkCode)
            return True
        if msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
            self._handle_down()
        else:
            self._handle_up()
        self._listener.suppress_event()

    # --- macOS / others ---
    def _matches(self, key) -> bool:
        return key == HOTKEYS[self._key_name][1]

    def _on_press(self, key):
        if self._matches(key):
            self._handle_down()

    def _on_release(self, key):
        if self._matches(key):
            self._handle_up()

    def _handle_down(self):
        if self._down_at is not None:  # auto-repeat
            return
        self._down_at = time.monotonic()
        self._double = self._tap_up_at is not None and self._down_at - self._tap_up_at < DOUBLE_TAP_GAP
        self._tap_up_at = None
        if self._double and self.on_double:
            self.on_double()
        self.on_press()

    def _handle_up(self):
        if self._down_at is None:
            return
        now = time.monotonic()
        held = now - self._down_at
        self._down_at = None
        self.on_release(held)
        tap = held < TAP_THRESHOLD
        self._tap_up_at = now if tap and not self._double else None
        undo_toggle = self._double and not tap and self._key_name == "caps_lock"
        if (tap or undo_toggle) and sys.platform == "win32":
            # Replay the tap outside the hook callback
            threading.Thread(target=self._replay_tap, daemon=True).start()

    def _replay_tap(self):
        key = HOTKEYS[self._key_name][1]
        self._controller.press(key)
        self._controller.release(key)
