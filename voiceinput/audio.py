"""Microphone capture with sounddevice."""
import threading

import numpy as np
import sounddevice as sd

from .asr import SAMPLE_RATE


def list_input_devices() -> list[str]:
    """Input device names from the platform's default host API (avoids MME/WASAPI duplicates)."""
    try:
        default_api = sd.default.hostapi
        devices = sd.query_devices()
    except Exception:
        return []
    names = []
    for d in devices:
        if (d["max_input_channels"] > 0 and d["hostapi"] == default_api
                and "Sound Mapper" not in d["name"] and d["name"] not in names):
            names.append(d["name"])
    return names


def _resolve_device(name: str):
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and d["max_input_channels"] > 0:
            return i
    return None


class Recorder:
    def __init__(self):
        self._chunks: list[np.ndarray] = []
        self._stream = None
        self._lock = threading.Lock()
        self.level = 0.0   # latest RMS level, 0..1, read by the overlay

    def start(self, device_name: str = "") -> None:
        self._chunks = []
        self.level = 0.0
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            device=_resolve_device(device_name),
            callback=self._callback,
            blocksize=int(SAMPLE_RATE * 0.03),
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        mono = indata[:, 0].copy()
        with self._lock:
            self._chunks.append(mono)
        rms = float(np.sqrt(np.mean(mono * mono)))
        self.level = min(1.0, rms * 12)

    def stop(self) -> np.ndarray:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
            self._chunks = []
        self.level = 0.0
        return audio
