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
_ALNUM_END = re.compile(r"[A-Za-z0-9]$")
_ALNUM_START = re.compile(r"^[A-Za-z0-9]")

# Qwen3-ASR caps prompt + audio + generated tokens at 512 (a model limit; ~13 audio tokens per
# second). Past ~37s sherpa-onnx truncates the audio and the output turns to garbage ("language"),
# so long audio is decoded in chunks cut at the quietest point.
_QWEN3_MAX_TOTAL = 512
_QWEN3_MAX_NEW = 160
_CHUNK_MAX_SEC = 25
_CHUNK_MIN_SEC = 10


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
        max_total_len=_QWEN3_MAX_TOTAL,
        max_new_tokens=_QWEN3_MAX_NEW,  # default 128 can cut off a dense 25s chunk
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
        if self.key != "qwen3_asr":
            return self._decode(audio)
        text = ""
        for chunk in _split(audio):
            part = self._decode(chunk)
            if text and part:
                # The model ends every chunk with 。 even when the cut is mid-sentence (又會。以)
                text = text.rstrip("。.")
                if _ALNUM_END.search(text) and _ALNUM_START.search(part):
                    text += " "
            text += part
        return text

    def _decode(self, audio: np.ndarray) -> str:
        stream = self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio.astype(np.float32))
        self._rec.decode_stream(stream)
        # SenseVoice may emit tags like <|zh|><|NEUTRAL|>
        text = _TAGS.sub("", stream.result.text).strip()
        # X-ASR emits a space after full-width punctuation
        return _PUNCT_SPACE.sub(r"\1", text)


def _split(audio: np.ndarray) -> list[np.ndarray]:
    """Cut audio into chunks of at most _CHUNK_MAX_SEC, each cut in the middle of the quietest
    0.4s between _CHUNK_MIN_SEC and _CHUNK_MAX_SEC into the remaining audio, so cuts land in
    real pauses rather than a short gap inside a word."""
    frame = SAMPLE_RATE // 20
    max_len, min_len = _CHUNK_MAX_SEC * SAMPLE_RATE, _CHUNK_MIN_SEC * SAMPLE_RATE
    chunks = []
    while audio.size > max_len:
        window = audio[min_len:max_len]
        n = window.size // frame
        energy = np.square(window[: n * frame].reshape(n, frame)).mean(axis=1)
        energy = np.convolve(energy, np.ones(8), mode="same")
        cut = min_len + int(np.argmin(energy)) * frame + frame // 2
        chunks.append(audio[:cut])
        audio = audio[cut:]
    chunks.append(audio)
    return chunks
