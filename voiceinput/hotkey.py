"""Global push-to-talk hotkey via pynput.

The hotkey is suppressed with a low-level hook so holding CapsLock
does not toggle caps state. Whether a short tap (< TAP_THRESHOLD) is replayed as a normal key press is up to the
app (tap()): a tap on selected text is VoiceInput's own gesture, anywhere else CapsLock still works as CapsLock.

A double tap is a second press within DOUBLE_TAP_GAP of a tap's release; on_press / on_release get double=True
for that second press.

The upper side button of a mouse (XButton2, "forward") can be the hotkey too: a mouse hook suppresses it the same
way, and it is never replayed: its "forward" action is replaced entirely (the user's choice, 2026-10-06).
"""
import time

from pynput import keyboard, mouse

TAP_THRESHOLD = 0.3  # seconds; shorter presses are treated as a normal tap
DOUBLE_TAP_GAP = 0.35  # seconds from a tap's release to the next press to count as a double tap

HOTKEYS = {
    "caps_lock": ("CapsLock", keyboard.Key.caps_lock),
    "alt_r": ("右 Alt", keyboard.Key.alt_r),
    "ctrl_r": ("右 Ctrl", keyboard.Key.ctrl_r),
    "f12": ("F12", keyboard.Key.f12),
    "mouse_x2": ("滑鼠側鍵（上）", mouse.Button.x2),
}

# Virtual-key codes for the hook-level filter
_WIN_VK = {"caps_lock": 0x14, "alt_r": 0xA5, "ctrl_r": 0xA3, "f12": 0x7B}
_WM_KEYDOWN, _WM_SYSKEYDOWN = 0x0100, 0x0104
_LLKHF_INJECTED = 0x10
_WM_XBUTTONDOWN, _WM_XBUTTONUP = 0x20B, 0x20C
_XBUTTON = {"mouse_x2": 2}   # high word of MSLLHOOKSTRUCT.mouseData
_LLMHF_INJECTED = 1


def is_mouse_hotkey(key_name: str, msg: int, mouse_data: int) -> bool:
    """A low-level mouse event is a press or release of this mouse hotkey."""
    return msg in (_WM_XBUTTONDOWN, _WM_XBUTTONUP) and mouse_data >> 16 == _XBUTTON.get(key_name)


class PushToTalk:
    """Calls on_press(double) when the hotkey goes down and on_release(held_seconds, double) when it goes up."""

    def __init__(self, key_name: str, on_press, on_release, on_other_key=None):
        self.on_press = on_press
        self.on_release = on_release
        self.on_other_key = on_other_key   # on_other_key(vk): any other key the user pressed
        self._key_name = key_name
        self._down_at = None
        self._tap_up_at = None      # release time of the last short tap (double-tap detection)
        self._double = False        # the current press is the second of a double tap
        self._listener = None
        self._mouse_listener = None
        self._controller = keyboard.Controller()
        self._replaying = False

    def set_key(self, key_name: str) -> None:
        self._key_name = key_name
        self._down_at = None

    def start(self) -> None:
        self._listener = keyboard.Listener(win32_event_filter=self._win32_filter)
        self._listener.daemon = True
        self._listener.start()
        self._mouse_listener = mouse.Listener(win32_event_filter=self._win32_mouse_filter)
        self._mouse_listener.daemon = True
        self._mouse_listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()

    # Everything is handled inside the hook so the key can be suppressed
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

    def _win32_mouse_filter(self, msg, data):
        if data.flags & _LLMHF_INJECTED or not is_mouse_hotkey(self._key_name, msg, data.mouseData):
            return False   # never passed on to pynput's (unused) callbacks
        if msg == _WM_XBUTTONDOWN:
            self._handle_down()
        else:
            self._handle_up()
        self._mouse_listener.suppress_event()

    def _handle_down(self):
        if self._down_at is not None:  # auto-repeat
            return
        self._down_at = time.monotonic()
        self._double = self._tap_up_at is not None and self._down_at - self._tap_up_at < DOUBLE_TAP_GAP
        self._tap_up_at = None
        self.on_press(self._double)

    def _handle_up(self):
        if self._down_at is None:
            return
        now = time.monotonic()
        held = now - self._down_at
        self._down_at = None
        self._tap_up_at = now if held < TAP_THRESHOLD and not self._double else None
        self.on_release(held, self._double)

    def tap(self):
        """Replay one press of the hotkey to the focused app (injected, so the hook lets it through)."""
        key = HOTKEYS[self._key_name][1]
        if isinstance(key, mouse.Button):
            return   # the side button's "forward" is replaced entirely
        self._controller.press(key)
        self._controller.release(key)
