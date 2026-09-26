"""Speech recognition via sherpa-onnx (X-ASR zipformer transducer)."""
import os
import re

import numpy as np
import sherpa_onnx

from .models import MODEL_DIR, is_installed

SAMPLE_RATE = 16000
_THREADS = max(1, min(4, (os.cpu_count() or 2) // 2))
_PUNCT_SPACE = re.compile(r"([，。？！、；：])\s+")
_TRAILING_PUNCT = re.compile(r"[。，,.]+$")


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
        try:
            import opencc
            # s2tw converts glyphs only; s2twp would also swap vocabulary (程序 -> 程式)
            self._s2t = opencc.OpenCC("s2tw")
        except Exception:
            self._s2t = None

    def transcribe(self, audio: np.ndarray, traditional: bool = True, strip_trailing_punct: bool = True) -> str:
        if audio.size < SAMPLE_RATE * 0.2:
            return ""
        stream = self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio.astype(np.float32))
        self._rec.decode_stream(stream)
        text = self._clean(stream.result.text, strip_trailing_punct)
        if traditional and self._s2t:
            text = self._s2t.convert(text)
        return text

    @staticmethod
    def _clean(text: str, strip_trailing_punct: bool) -> str:
        text = text.strip()
        # Drop trailing sentence-final punctuation, like CapsWriter does
        if strip_trailing_punct:
            text = _TRAILING_PUNCT.sub("", text)
        # X-ASR emits a space after full-width punctuation
        return _PUNCT_SPACE.sub(r"\1", text)
