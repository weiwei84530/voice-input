# VoiceInput

Offline push-to-talk voice input. Hold **CapsLock**, speak, release — text is pasted into the focused app.
Single process: one tray icon, settings window, floating indicator. Speech recognition via [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

## Install / run

| | Windows | macOS |
|---|---|---|
| Install | `install.bat` | `bash install.command` |
| Run | `start.bat` | `./start.command` |

The installer puts uv, Python and the venv inside this folder. Nothing is installed system-wide.
If you move the folder, run the installer again (the venv records absolute paths).

User data lives outside the app folder, so it survives moving or reinstalling the app:

| | Windows | macOS |
|---|---|---|
| Settings, logs | `%LOCALAPPDATA%\VoiceInput` | `~/Library/Application Support/VoiceInput` |
| Models | `%LOCALAPPDATA%\VoiceInput\models` | `…/VoiceInput/models` |

Data from the old layout (`config.json`, `models/` inside the app folder) is moved there automatically on first launch.

macOS: on first launch, grant **Microphone**, **Accessibility** and **Input Monitoring** in System Settings → Privacy & Security.

## Usage

- Hold the hotkey (default CapsLock) ≥ 0.3s to record; a quick tap still toggles CapsLock.
- Double-tap the hotkey to undo what you just dictated; hold the second press to say it again.
- Left-click the tray icon to open settings (microphone, hotkey, trailing punctuation removal, launch at login, the
  words for 刪除 / 選字, hotwords) and a per-utterance log for this session. Changes apply immediately.
- Missing models are downloaded automatically on launch (progress shown in settings / tray tooltip).

## Correcting

Select text with the mouse, then hold the hotkey and say:

| Say | Does |
|---|---|
| 刪除 (editable in settings) | delete the selection |
| 選字 (editable), or the selected word again | a menu of same-sounding words; click one |
| anything else | replaces the selection |

After a dictation a small menu may offer a likely misheard word (a second model heard it differently), or ask whether
a learned correction applies here (「城市」換成？ with a 保留「城市」 row). Menus are answered with the mouse only.
Corrections are offered as hotwords (click ✓ to add): a replaced selection, a picked word, a sentence undone and said
again, or a word you fixed by typing. A hotword remembers the words around it to decide next time.

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
  hotkey.py    global hotkey, double tap (pynput; Windows low-level hook suppresses CapsLock)
  audio.py     microphone capture
  asr.py       sherpa-onnx recognizers, hotword biasing
  textfmt.py   final text formatting (incl. repeated-word removal)
  session.py   what was just typed at the caret, undo, learning from re-dictation and hand edits
  edit.py      voice edits on a selection
  suspects.py  second-opinion check for misheard words
  selection.py selection / on-screen terms via UI Automation
  hotwords.py  learned corrections; ASR biasing words
  candidates.py same-sounding words (jieba dictionary)
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
