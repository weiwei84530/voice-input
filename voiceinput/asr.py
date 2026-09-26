"""Speech recognition via sherpa-onnx."""
import os
import re

import numpy as np
import sherpa_onnx

from .models import is_installed, model_dir

SAMPLE_RATE = 16000
_THREADS = max(1, min(4, (os.cpu_count() or 2) // 2))
_PUNCT_SPACE = re.compile(r"([，。？！、；：])\s+")
_TAGS = re.compile(r"<\|[^|]*\|>")


def _sense_voice(d):
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(d / "model.int8.onnx"),
        tokens=str(d / "tokens.txt"),
        num_threads=_THREADS,
        language="auto",   # auto handles zh/en code-switching
        use_itn=True,
    )


def _xasr(d):
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(d / "encoder-epoch-99-avg-1.int8.onnx"),
        decoder=str(d / "decoder-epoch-99-avg-1.onnx"),
        joiner=str(d / "joiner-epoch-99-avg-1.int8.onnx"),
        tokens=str(d / "tokens.txt"),
        num_threads=_THREADS,
        modeling_unit="cjkchar+bpe",
        bpe_vocab=str(d / "bpe.model"),
    )


def _qwen3_asr(d):
    return sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
        conv_frontend=str(d / "conv_frontend.onnx"),
        encoder=str(d / "encoder.int8.onnx"),
        decoder=str(d / "decoder.int8.onnx"),
        tokenizer=str(d / "tokenizer"),
        num_threads=_THREADS,
    )


def _funasr_nano(d):
    return sherpa_onnx.OfflineRecognizer.from_funasr_nano(
        encoder_adaptor=str(d / "encoder_adaptor.int8.onnx"),
        llm=str(d / "llm.fp16.onnx"),  # the int8 export yields empty output on some CPUs
        embedding=str(d / "embedding.int8.onnx"),
        tokenizer=str(d / "Qwen3-0.6B"),
        num_threads=_THREADS,
        itn=True,
    )


_LOADERS = {
    "sensevoice": _sense_voice,
    "xasr": _xasr,
    "qwen3_asr": _qwen3_asr,
    "funasr_nano": _funasr_nano,
}


class Recognizer:
    def __init__(self, key: str):
        if not is_installed(key):
            raise FileNotFoundError(f"Model '{key}' is not installed.")
        self.key = key
        self._rec = _LOADERS[key](model_dir(key))

    def transcribe(self, audio: np.ndarray) -> str:
        """Raw recognizer text; final formatting happens in textfmt."""
        if audio.size < SAMPLE_RATE * 0.2:
            return ""
        stream = self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio.astype(np.float32))
        self._rec.decode_stream(stream)
        # SenseVoice may emit tags like <|zh|><|NEUTRAL|>
        text = _TAGS.sub("", stream.result.text).strip()
        # X-ASR emits a space after full-width punctuation
        return _PUNCT_SPACE.sub(r"\1", text)
