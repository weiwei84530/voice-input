# Porting VoiceInput to macOS

Status (2026-10-03): VoiceInput is Windows only. The half-done macOS paths were removed in the commit after
`f6a7b83` (that commit still has them: `install.command`, `start.command`, the LaunchAgent in `autostart.py`, the
`~/Library/Application Support` data folder, Cmd+V paste, a plain pynput listener in `hotkey.py`). This file lists
what a real port needs, so the work can start without re-investigating.

## What happened on the first try (2026-10-02)

Desktop Mac, Logitech Bluetooth keyboard. All four hotkeys (CapsLock, right Alt, right Ctrl, F12) did nothing and
no permission dialog appeared.

- **No dialog**: a pynput listener without Input Monitoring permission just receives no events; macOS does not
  prompt for it. The app must ask explicitly (`CGPreflightListenEventAccess` / `CGRequestListenEventAccess` for
  Input Monitoring, `AXIsProcessTrustedWithOptions` with the prompt option for Accessibility) and tell the user
  to restart after granting. When started from Terminal the permission belongs to Terminal (or the python
  binary), which is confusing; a signed `.app` bundle gets its own entry.
- **CapsLock**: macOS reports CapsLock as a modifier *toggle* (`flagsChanged`), not down/up: the first press looks
  like "down" and the release is only seen at the next press. Holding it cannot be detected this way. macOS also
  delays CapsLock to avoid accidental presses and, with a Chinese input method, uses it to switch input source.
- **Right Ctrl**: missing on Apple keyboards (present on most Logitech ones).
- **F12**: a media key unless fn is held or "use F1, F2 … as standard function keys" is on (Logitech keyboards
  may have their own Fn lock / Logi Options+ mapping).

## Hold CapsLock to talk (the user's requirement)

Recommended: remap CapsLock to an unused key at the HID level and listen to that key.

1. At launch: `hidutil property --set '{"UserKeyMapping":[{"HIDKeyboardModifierMappingSrc":0x700000039,
   "HIDKeyboardModifierMappingDst":0x70000006D}]}'` (CapsLock → F18). No admin rights; it applies to every
   keyboard (use `--matching` to limit it) and is lost on reboot, so reapply on every launch and remove it on
   quit (`UserKeyMapping: []`). This also stops CapsLock from switching input source and removes its delay.
2. Listen for F18 with a `CGEventTap` (Quartz, via pyobjc) at `kCGSessionEventTap`, and return `None` from the
   callback to swallow it. F18 has real keyDown / keyUp, so hold, tap and double tap work like on Windows.
3. A short tap must still toggle caps lock (unless a selection is in play, as on Windows): set the lock state
   with IOKit (`IOHIDSetModifierLockState` on the `IOHIDSystem` service). Replaying a CapsLock key event does
   not work once the key is remapped.
4. If the app crashes the mapping stays until reboot: CapsLock types nothing. Document `hidutil property --set
   '{"UserKeyMapping":[]}'` or restore it on the next launch.

Alternative without remapping: `IOHIDManager` sees the physical key's real down/up (usage 0x39), but cannot stop the
toggle, so caps lock flips on every press. Not recommended. Karabiner-Elements does the same as step 1, but makes
the user install a driver.

## Windows pieces and their macOS counterparts

| Feature | Windows (file) | macOS |
|---|---|---|
| Hotkey, suppress it, ignore injected keys, see other keys | `WH_KEYBOARD_LL` via pynput `win32_event_filter`, `LLKHF_INJECTED` (`hotkey.py`) | `CGEventTap`; injected events: mark ours with `kCGEventSourceUserData` and skip them |
| "Any other key ends the buffer" | Windows VK codes in `session.py` (`_EDIT_KEYS`, Backspace / Delete) | Same tap; mac key codes differ (Backspace = 51, forward delete = 117, arrows 123-126) |
| Clicks end the buffer; terminal drags | `WH_MOUSE_LL`, `LLMHF_INJECTED` (`app._watch_mouse`) | `CGEventTap` for mouse down / up |
| Paste | Ctrl+V with pynput (`output.py`) | Cmd+V; the clipboard snapshot / restore is Qt and should work |
| Backspace / arrow bursts | one `SendInput` call (`output.press_key`) | `CGEventPost` in a loop; check whether apps drop keys (Codex dropped one on Windows) |
| Foreground window (buffer ends on switch) | `GetForegroundWindow` (`session.foreground`) | `NSWorkspace.frontmostApplication` pid + focused window from AX |
| Selection text, line context, bounds | UI Automation TextPattern (`selection.read_selection`) | `AXUIElement`: `kAXFocusedUIElementAttribute`, `kAXSelectedTextAttribute`, `kAXSelectedTextRangeAttribute`, `kAXValueAttribute`, `kAXBoundsForRangeParameterizedAttribute` |
| Visible text (on-screen terms, hand-made edit check) | TextPattern `GetVisibleRanges` (`selection.visible_text`, `session.typed_fix`) | `kAXVisibleCharacterRangeAttribute` + `kAXStringForRangeParameterizedAttribute` |
| Caret position for menus | `GetGUIThreadInfo` (`selection.caret_rect`) | bounds of the selected range (length 0) |
| App name (terminal detection) | process image name (`selection._proc_name`, `_TERMINALS`) | `NSRunningApplication.bundleIdentifier` (com.apple.Terminal, com.googlecode.iterm2, …) |
| Terminal mouse selection | Windows Terminal UIA `RangeFromPoint`, cursor moved by arrows (`selection.TermWatcher`) | Rewrite: Terminal.app and iTerm2 expose their text through AX differently; reprobe like `dev/term_probe.py` |
| Menus that do not take focus | `WS_EX_NOACTIVATE` (`picker._no_activate`) | `NSPanel` with `NSWindowStyleMaskNonactivatingPanel` (Qt alone still activates the app on click) |
| Click-through indicator | `WS_EX_TRANSPARENT` (`overlay._click_through`) | `Qt.WindowTransparentForInput` should be enough; check it stays above full-screen apps (`collectionBehavior`) |
| Tray icon | `QSystemTrayIcon`, left click opens settings | Menu bar icon; a click shows the menu, so open settings from a menu item |
| Dark taskbar icon | registry (`app._dark_taskbar`) | `NSApp.effectiveAppearance` or a template icon |
| Launch at login | HKCU Run (`autostart.py`) | LaunchAgent plist (code in `f6a7b83`), or `SMAppService` for a bundle |
| Data folder | `%LOCALAPPDATA%\VoiceInput` (`paths.py`) | `~/Library/Application Support/VoiceInput` |
| Install / start | `install.bat`, `start.bat` | `install.command`, `start.command` in `f6a7b83` (uv, Python 3.12) |
| Test audio | `dev/tts.ps1` (zh-TW Hanhan) | `say -v Meijia -o x.aiff` then convert to 16kHz wav |

Platform independent, expected to work unchanged: `asr.py` (sherpa-onnx has macOS arm64 wheels), `audio.py`
(sounddevice; needs Microphone permission), `textfmt.py`, `hotwords.py`, `candidates.py`, `picks.py`,
`suspects.py`, `edit.py`, `models.py`, the Qt settings window.

## Suggested order

1. Packaging and permissions: an `.app` (PyInstaller) with `NSMicrophoneUsageDescription`, permission checks
   and prompts at launch. Without this every later step is hard to test.
2. CapsLock hold via hidutil + CGEventTap, paste with Cmd+V. This gives plain dictation and is a usable product.
3. Buffer, double-tap undo, re-dictation: key / mouse taps, foreground app, Backspace bursts.
4. Selection edits and candidate menus: AX selection reading, non-activating `NSPanel`.
5. On-screen terms and hand-made edit learning (AX visible text).
6. Terminal selections last, per terminal app.

Abstraction suggestion: put each OS-specific piece behind a small module (`platform_win.py` / `platform_mac.py`
with the same functions: `foreground()`, `read_selection()`, `visible_text()`, `caret_rect()`, `press_key()`,
`no_activate(widget)` …) rather than `sys.platform` checks spread across files, which is what the removed
partial support looked like.

## Testing

Needs a Mac to run on; nothing here can be verified from Windows. `dev/headless.py` simulates the text box and
should mostly work once TTS audio comes from `say`.
