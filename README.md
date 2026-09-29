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
| Models (default, changeable in settings) | `%LOCALAPPDATA%\VoiceInput\models` | `…/VoiceInput/models` |

Data from the old layout (`config.json`, `models/` inside the app folder) is moved there automatically on first launch.

macOS: on first launch, grant **Microphone**, **Accessibility** and **Input Monitoring** in System Settings → Privacy & Security.

## Usage

- Hold the hotkey (default CapsLock) ≥ 0.3s to record; a quick tap still toggles CapsLock.
- Left-click the tray icon to open settings (models folder, microphone, hotkey, trailing punctuation removal, launch at
  login, voice commands, live captions, second opinion, on-screen terms, hotwords) and a per-utterance log for this
  session. Changes apply immediately.
- Missing models are downloaded automatically on launch (progress shown in settings / tray tooltip).
- Live captions appear above the indicator while you speak (streaming X-ASR, display only).

## Editing by voice

What you just dictated can be changed without selecting it, as long as you have not pressed a key or clicked since
(works in terminals too):

| Say | Does |
|---|---|
| 城市改成程式 / 不是城市是程式 | replace a word in what you dictated (玉改成教育的育, `cloud 改成 C L A U D E`) |
| 刪掉城市 / 刪掉上一句 / 全部刪掉 | delete |
| 復原 | undo the last change |
| 問號 / 逗號 … | type the symbol (replaces trailing punctuation) |
| 換行 / 送出 | Shift+Enter / Enter; "…（pause）送出" at the end of a sentence pastes and sends |
| 不要記 | forget the hotword just learned |

With text selected: 刪除, 選字 (candidate menu), a new word, 改成X, spelled letters, 教育的育, 大寫 / 小寫, or a
punctuation name.
After a dictation a small menu may offer a likely misheard word (a second model heard it differently), or ask whether
a learned correction applies here (「城市」要換成「程式」嗎): say 對, 第幾個 or 不用. Corrections are offered as hotwords (click ✓ to add),
including a sentence deleted and said again; a hotword remembers the words around it to decide next time.

## Models

Fixed, all [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) models, ~420MB in total, no LLM:

| Model | Size | Role |
|---|---|---|
| X-ASR int8 | 130MB | Recognition (~0.75s for 18s of audio on CPU); punctuation, good English |
| SenseVoice Small | 155MB | Second opinion in the background (~0.1s per sentence) |
| X-ASR streaming 480ms | 134MB | Live captions while recording |

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
  hotkey.py    global hotkey (pynput; Windows low-level hook suppresses CapsLock)
  audio.py     microphone capture
  asr.py       sherpa-onnx recognizers, hotword biasing, live captions
  textfmt.py   final text formatting (incl. repeated-word removal)
  session.py   what was just typed at the caret, undo, re-dictation
  commands.py  voice commands without a selection
  edit.py      voice edits on a selection
  suspects.py  second-opinion check for misheard words
  selection.py selection / on-screen terms via UI Automation
  hotwords.py  learned corrections; ASR biasing words
  candidates.py same-sounding words (jieba dictionary)
  picker.py    candidate / suggestion menu
  overlay.py   floating recording / thinking indicator
  settings.py  settings dialog + transcript log
  output.py    clipboard paste, Backspace / Enter
  models.py    the fixed model set + downloader
  paths.py     app folder vs per-user data folder, legacy data migration
  autostart.py launch at login
dev/
  headless.py  end-to-end test with TTS audio against a simulated text box
  e2e.py       the same against a real Notepad / Windows Terminal window
  tts.ps1      Windows zh-TW TTS to 16kHz wav
```
