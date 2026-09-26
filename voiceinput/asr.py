"""Speech recognition via sherpa-onnx (X-ASR zipformer transducer)."""
import os
import re

import numpy as np
import sherpa_onnx

from .models import MODEL_DIR, is_installed

SAMPLE_RATE = 16000
_THREADS = max(1, min(4, (os.cpu_count() or 2) // 2))
_PUNCT_SPACE = re.compile(r"([，。？！、；：])\s+")


def _load() -> sherpa_onnx.OfflineRecognizer:
    d = MODEL_DIR
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(d / "encoder-epoch-99-avg-1.int8.onnx"),
        decoder=str(d / "decoder-epoch-99-avg-1.onnx"),
        joiner=str(d / "joiner-epoch-99-avg-1.int8.onnx"),
        tokens=str(d / "tokens.txt"),
        num_threads=_THREADS,
        modeling_unit="cjkchar+bpe",
        bpe_vocab=str(d / "bpe.model"),
    )


class Recognizer:
    def __init__(self):
        if not is_installed():
            raise FileNotFoundError("Model is not installed. Run install again.")
        self._rec = _load()

    def transcribe(self, audio: np.ndarray) -> str:
        """Raw recognizer text; final formatting happens in textfmt."""
        if audio.size < SAMPLE_RATE * 0.2:
            return ""
        stream = self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio.astype(np.float32))
        self._rec.decode_stream(stream)
        # X-ASR emits a space after full-width punctuation
        return _PUNCT_SPACE.sub(r"\1", stream.result.text.strip())
