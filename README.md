# VoiceInput

Offline push-to-talk voice input. Hold **CapsLock**, speak, release — text is pasted into the focused app.
Single process: one tray icon, settings window, floating indicator. Speech recognition via [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

## Install / run

| | Windows | macOS |
|---|---|---|
| Install | `install.bat` | `bash install.command` |
| Run | `start.bat` | `./start.command` |

The installer puts uv, Python, the venv and models inside this folder. Nothing is installed system-wide.
If you move the folder, run the installer again.

macOS: on first launch, grant **Microphone**, **Accessibility** and **Input Monitoring** in System Settings → Privacy & Security.

## Usage

- Hold the hotkey (default CapsLock) ≥ 0.3s to record; a quick tap still toggles CapsLock.
- Left-click the tray icon to open settings (speech model, microphone, hotkey, trailing punctuation removal, launch at login, LLM custom rules)
  and shows a per-utterance log (ASR / formatted / LLM) for this session. Changes apply immediately.
- If the model is missing it is downloaded automatically on launch (progress shown in settings / tray tooltip).

## Models

Selectable in settings; a model that isn't downloaded yet is fetched automatically when selected.

| Model | Size | 18s audio (CPU) | Notes |
|---|---|---|---|
| X-ASR (default) | 130MB | ~0.75s | Punctuation, good English |
| SenseVoice Small | 155MB | ~0.8s | Punctuation, numbers as digits, weaker English |
| Qwen3-ASR 0.6B | 840MB | ~4.4s | Most accurate |
| Fun-ASR-Nano (fp16) | 1GB | ~7.5s | The int8 build returns empty output on some CPUs |

## Pipeline

ASR → formatting (`textfmt.py`) → optional LLM custom rules → paste.

- **LLM custom rules** (off by default): Qwen3.5 2B (Q4_K_M GGUF) served by a local `llama-server`
  (llama.cpp, CPU build in `.tools/llama`), downloaded and loaded only while enabled. It applies only the rules
  typed in settings and leaves everything else unchanged; with no rules it is skipped.
  Timings are written to `voiceinput.log`.
- **Formatting**: Traditional Chinese glyphs (OpenCC `s2tw`, wording kept), multi-character Chinese numerals → digits
  (single-character ones like 一個 / 兩個 stay), 百分之X / X percent → X%, 嗯 / 呃 removed, spelled letters joined (A P I → API), 點 between digits/letters → `.`,
  half-width punctuation inside English, a space between CJK and English/digits, optional trailing punctuation removal.

## Layout

```
voiceinput/
  app.py       tray, wiring, push-to-talk flow
  hotkey.py    global hotkey (pynput; Windows low-level hook suppresses CapsLock)
  audio.py     microphone capture
  asr.py       sherpa-onnx recognizers
  llm.py       llama-server lifecycle + custom-rule rewrite
  textfmt.py   final text formatting
  overlay.py   floating recording / thinking indicator
  settings.py  settings dialog + transcript log
  output.py    clipboard paste
  models.py    ASR model registry + downloader
  autostart.py launch at login
```
