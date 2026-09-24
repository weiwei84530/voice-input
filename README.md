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
- Left-click the tray icon to open settings (model, microphone, hotkey, Traditional Chinese output, launch at login).
- Selecting a model that isn't downloaded yet downloads it automatically (progress shown in settings / tray tooltip).

## Models

| Model | Size | 18s audio (CPU) | Notes |
|---|---|---|---|
| X-ASR (default) | 130MB | ~0.75s | Punctuation, good English |
| SenseVoice Small | 155MB | ~0.8s | Punctuation, numbers as digits, weaker English |
| Qwen3-ASR 0.6B | 840MB | ~4.4s | Most accurate |
| Fun-ASR-Nano (fp16) | 1GB | ~7.5s | The int8 build returns empty output on some CPUs |

## Layout

```
voiceinput/
  app.py       tray, wiring, push-to-talk flow
  hotkey.py    global hotkey (pynput; Windows low-level hook suppresses CapsLock)
  audio.py     microphone capture
  asr.py       sherpa-onnx recognizers, text cleanup, s2twp conversion
  overlay.py   floating recording / thinking indicator
  settings.py  settings dialog
  output.py    clipboard paste
  models.py    model registry + downloader
  autostart.py launch at login
```
