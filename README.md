# VoiceInput

Offline push-to-talk voice input for Windows (macOS is not supported; see [docs/macos-port.md](docs/macos-port.md)). Hold **CapsLock**, speak, release — text is pasted into the focused app.
Single process: one tray icon, settings window, floating indicator. Speech recognition via [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

## Install / run

Install with `install.bat`, run with `start.bat`.

The installer puts uv, Python and the venv inside this folder. Nothing is installed system-wide.
If you move the folder, run the installer again (the venv records absolute paths).

User data lives outside the app folder, so it survives moving or reinstalling the app:

`%LOCALAPPDATA%\VoiceInput` holds settings, hotwords, logs and `models\`.

Data from the old layout (`config.json`, `models/` inside the app folder) is moved there automatically on first launch.

## Usage

- Hold the hotkey (default CapsLock) ≥ 0.3s to record; a quick tap still toggles CapsLock (except on selected text).
- Double-tap the hotkey to delete what you just dictated (the last sentence only, before the caret moves); hold the second press to say it again.
- Say only 逗號 / 句號 / 問號 / 驚嘆號 / 頓號 / 分號 / 冒號 / 點點點 to type that mark.
- Left-click the tray icon to open settings (microphone, hotkey, trailing punctuation removal, launch at login,
  how long menus stay open, hotwords) and a per-utterance log for this session. Changes apply immediately.
- Missing models are downloaded automatically on launch (progress shown in settings / tray tooltip).

## Correcting

Select text with the mouse, then:

| Hotkey | Does |
|---|---|
| tap once | a menu of same-sounding words; click one |
| double tap | delete the selection |
| hold and speak | replaces the selection |
| double tap, hold the second press and speak | deletes the selection, then types what you say |

When what you say sounds like what it replaced (城市 → 程式), or you delete a sentence and say it again with a word
changed, the new text is typed and a menu of same-sounding words opens for it. Words you picked before come first.

After a dictation a small menu may offer a likely misheard word (a second model heard it differently), or ask whether
a learned correction applies here (「城市」換成？ with a 保留「城市」 row). Menus are answered with the mouse only.
Corrections are offered as hotwords (click ✓ to add) after you pick a word in a menu, or when you fix a word by typing.
A bar on the menu's bottom edge shows the time left before it closes. A hotword remembers the words around it to decide next time.

## Models

Fixed, all [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) models, ~285MB in total, no LLM:

| Model | Size | Role |
|---|---|---|
| X-ASR int8 | 130MB | Recognition (~0.75s for 18s of audio on CPU); punctuation, good English |
| SenseVoice Small | 155MB | Second opinion in the background (~0.1s per sentence) |

## Pipeline

ASR (biased toward learned hotwords and English terms on screen) → formatting (`textfmt.py`) → hotwords → paste.

- **Formatting**: Traditional Chinese glyphs (OpenCC `s2tw`, wording kept), repeated words and restarts removed
  (我們我們, 先打開那個，先打開設定頁), multi-character Chinese numerals → digits (single-character ones like 一個 / 兩個
  stay), 百分之X / X percent → X%, 嗯 / 呃 removed, spelled letters joined (A P I → API), 點 between digits/letters →
  `.`, half-width punctuation inside English, a space between CJK and English/digits, optional trailing punctuation
  removal.

## Layout

```
voiceinput/
  app.py       tray, wiring, push-to-talk flow
  hotkey.py    global hotkey, double tap (pynput; a low-level hook suppresses CapsLock)
  audio.py     microphone capture
  asr.py       sherpa-onnx recognizers, hotword biasing
  textfmt.py   final text formatting (incl. repeated-word removal)
  session.py   what was just typed at the caret, deleting the last dictation, learning from re-dictation and hand edits
  edit.py      replacing a selection, punctuation names, "sounds alike" test
  suspects.py  second-opinion check for misheard words
  selection.py selection / on-screen terms via UI Automation
  hotwords.py  learned corrections; ASR biasing words
  candidates.py same-sounding words (McBopomofo word list)
  picks.py     words picked in menus, listed first next time
  picker.py    candidate / suggestion menu
  overlay.py   floating recording / thinking indicator
  settings.py  settings dialog + transcript log
  output.py    clipboard paste, Delete / Backspace
  models.py    the fixed model set + downloader
  paths.py     app folder vs per-user data folder, legacy data migration
  autostart.py launch at login
dev/
  headless.py  end-to-end test with TTS audio against a simulated text box
  e2e.py       the same against a real Notepad / Windows Terminal window
  tts.ps1      Windows zh-TW TTS to 16kHz wav
```
