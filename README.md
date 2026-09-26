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
- Left-click the tray icon to open settings (microphone, hotkey, trailing punctuation removal, launch at login, LLM rewrite). Changes apply immediately.
- If the model is missing it is downloaded automatically on launch (progress shown in settings / tray tooltip).

## Model

X-ASR int8 zipformer transducer (zh/en, punctuation, ~130MB, ~0.75s for 18s of audio on CPU).
Previously evaluated alternatives are recorded in `CLAUDE.md`.

## Pipeline

ASR → optional LLM rewrite → formatting (`textfmt.py`) → paste.

- **LLM rewrite** (off by default): Qwen3.5 0.8B / 2B (Q4_K_M GGUF) served by a local `llama-server`
  (llama.cpp, CPU build in `.tools/llama`). Downloaded on first selection. The prompt is editable in settings;
  the default one removes restarted / repeated phrases. Timings are written to `voiceinput.log`.
- **Formatting**: Traditional Chinese glyphs (OpenCC `s2tw`, wording kept), Chinese numerals → digits,
  百分之X → X%, spelled letters joined (A P I → API), 點 between digits/letters → `.`,
  half-width punctuation inside English, a space between CJK and English/digits, optional trailing punctuation removal.

## Layout

```
voiceinput/
  app.py       tray, wiring, push-to-talk flow
  hotkey.py    global hotkey (pynput; Windows low-level hook suppresses CapsLock)
  audio.py     microphone capture
  asr.py       sherpa-onnx recognizer
  llm.py       llama-server lifecycle + LLM rewrite
  textfmt.py   final text formatting
  overlay.py   floating recording / thinking indicator
  settings.py  settings dialog
  output.py    clipboard paste
  models.py    model location + downloader
  autostart.py launch at login
```
