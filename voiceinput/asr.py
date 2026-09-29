"""Speech recognition via sherpa-onnx."""
import os
import re
import struct
from pathlib import Path

import numpy as np
import sherpa_onnx

from .models import is_installed, model_dir

SAMPLE_RATE = 16000
_THREADS = max(1, min(4, (os.cpu_count() or 2) // 2))
_PUNCT_SPACE = re.compile(r"([，。？！、；：])\s+")
_TAGS = re.compile(r"<\|[^|]*\|>")

# Hotword biasing (X-ASR only). Modified beam search with per-utterance hotwords costs about the same as greedy
# search (tested 2026-09-29: 0.11-0.22s either way on 3s clips). Latin terms work well (L M -> LLM,
# work tree -> worktree; cloud code -> Claude Code needs score 3). Chinese biasing is weak (張雨薇 -> 張玉薇 at
# score 3, 城市 never became 程式), so Chinese words still rely on the text-level hotwords.
_HOTWORD_SCORE = 2.0
_LATIN_TERM = re.compile(r"^[A-Za-z][A-Za-z0-9 .'+#-]*$")


def _bpe_vocab(d: Path) -> str:
    """sherpa-onnx wants the sentencepiece vocab as text ("piece score" lines) to encode hotwords. The model
    ships only bpe.model, so export it once by reading the protobuf directly (no sentencepiece dependency)."""
    out = d / "bpe.vocab"
    if out.exists():
        return str(out)

    def varint(b, i):
        r = shift = 0
        while True:
            c = b[i]
            i += 1
            r |= (c & 0x7F) << shift
            shift += 7
            if c < 0x80:
                return r, i

    def fields(b):
        i = 0
        while i < len(b):
            key, i = varint(b, i)
            num, kind = key >> 3, key & 7
            if kind == 0:
                v, i = varint(b, i)
            elif kind == 2:
                n, i = varint(b, i)
                v, i = b[i:i + n], i + n
            elif kind == 5:
                v, i = struct.unpack("<f", b[i:i + 4])[0], i + 4
            elif kind == 1:
                v, i = b[i:i + 8], i + 8
            else:
                raise ValueError(f"unsupported protobuf wire type {kind}")
            yield num, v

    lines = []
    for num, v in fields((d / "bpe.model").read_bytes()):
        if num == 1:   # ModelProto.pieces: {1: piece, 2: score}
            piece = dict(fields(v))
            lines.append(f"{piece[1].decode()}\t{piece.get(2, 0.0)}\n")
    out.write_text("".join(lines), encoding="utf-8")
    return str(out)


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
        # The vocab is pure BPE with CJK characters as "▁X" pieces: hotwords are encoded as bpe with the CJK
        # characters space-separated ("cjkchar+bpe" looks the characters up without ▁ and fails)
        modeling_unit="bpe",
        bpe_vocab=_bpe_vocab(d),
        decoding_method="modified_beam_search",
        max_active_paths=4,
        hotwords_score=_HOTWORD_SCORE,
    )


_LOADERS = {
    "sensevoice": _sense_voice,
    "xasr": _xasr,
}


class Recognizer:
    def __init__(self, key: str):
        if not is_installed(key):
            raise FileNotFoundError(f"Model '{key}' is not installed.")
        self.key = key
        self._rec = _LOADERS[key](model_dir(key))
        self._pieces = None
        if key == "xasr":
            lines = (model_dir(key) / "tokens.txt").read_text(encoding="utf-8").splitlines()
            self._pieces = {line.rsplit(" ", 1)[0] for line in lines if line}

    @property
    def supports_hotwords(self) -> bool:
        return self._pieces is not None

    def hotword_spec(self, words: list[tuple[str, float]]) -> str:
        """sherpa-onnx per-stream hotwords ("a/b c :3/…") from (word, score) pairs. Words the vocab cannot
        encode are dropped: a single bad word makes sherpa-onnx skip the whole list."""
        from .textfmt import to_simplified
        out, seen = [], set()
        for word, score in words:
            word = word.strip()
            if not word or word.casefold() in seen:
                continue
            if _LATIN_TERM.match(word):
                spec = word
            else:
                simp = to_simplified(word)
                if not all(("▁" + ch) in self._pieces for ch in simp):
                    continue
                spec = " ".join(simp)
            seen.add(word.casefold())
            out.append(spec if score == _HOTWORD_SCORE else f"{spec} :{score:g}")
        return "/".join(out)

    def recognize(self, audio: np.ndarray, hotwords: list[tuple[str, float]] | None = None) -> str:
        """Raw recognizer text; final formatting happens in textfmt. hotwords: (word, score) pairs, X-ASR only."""
        if audio.size < SAMPLE_RATE * 0.2:
            return ""
        spec = self.hotword_spec(hotwords) if hotwords and self.supports_hotwords else ""
        stream = self._rec.create_stream(spec) if spec else self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio.astype(np.float32))
        self._rec.decode_stream(stream)
        return self._clean(stream.result.text)

    @staticmethod
    def _clean(text: str) -> str:
        # SenseVoice may emit tags like <|zh|><|NEUTRAL|>
        text = _TAGS.sub("", text).strip()
        # X-ASR emits a space after full-width punctuation
        return _PUNCT_SPACE.sub(r"\1", text)
