"""Speech recognition via sherpa-onnx."""
import os
import re
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

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

# Hotword biasing (X-ASR only). Modified beam search with per-utterance hotwords costs about the same as greedy
# search (tested 2026-09-29: 0.11-0.22s either way on 3s clips). Latin terms work well (L M -> LLM,
# work tree -> worktree; cloud code -> Claude Code needs score 3). Chinese biasing is weak (張雨薇 -> 張玉薇 at
# score 3, 城市 never became 程式), so Chinese words still rely on the text-level hotwords.
_HOTWORD_SCORE = 2.0
_LATIN_TERM = re.compile(r"^[A-Za-z][A-Za-z0-9 .'+#-]*$")
_PUNCT_TOKEN = re.compile(r"^[\s，。？！、；：,.?!;:]*$")


@dataclass
class Transcript:
    text: str                                          # raw recognizer text
    tokens: list[str] = field(default_factory=list)    # per-token text (X-ASR only)
    times: list[float] = field(default_factory=list)   # token start times in seconds
    logprobs: list[float] = field(default_factory=list)
    duration: float = 0.0

    def pause_before_tail(self, tail_chars: int) -> float:
        """Silence (s) before the last tail_chars non-punctuation characters, 0 when unknown.
        Used to tell "…，送出" said after a pause from "把表單送出"."""
        idx = [i for i, t in enumerate(self.tokens) if not _PUNCT_TOKEN.match(t)]
        if not self.times or len(idx) <= tail_chars:
            return 0.0
        first, prev = idx[-tail_chars], idx[-tail_chars - 1]
        # times are token starts; a CJK token lasts ~0.2s
        return max(0.0, self.times[first] - self.times[prev] - 0.2)


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

    def transcribe(self, audio: np.ndarray) -> str:
        """Raw recognizer text; final formatting happens in textfmt."""
        return self.recognize(audio).text

    def recognize(self, audio: np.ndarray, hotwords: list[tuple[str, float]] | None = None) -> Transcript:
        """Raw text plus, for X-ASR, token times and confidences. hotwords: (word, score) pairs, X-ASR only."""
        duration = audio.size / SAMPLE_RATE
        if audio.size < SAMPLE_RATE * 0.2:
            return Transcript("", duration=duration)
        if self.key == "qwen3_asr":
            return Transcript(self._decode_chunked(audio), duration=duration)
        spec = self.hotword_spec(hotwords) if hotwords and self.supports_hotwords else ""
        stream = self._rec.create_stream(spec) if spec else self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio.astype(np.float32))
        self._rec.decode_stream(stream)
        r = stream.result
        tr = Transcript(self._clean(r.text), duration=duration)
        if self.key == "xasr":
            tr.tokens, tr.times, tr.logprobs = list(r.tokens), list(r.timestamps), list(r.ys_log_probs)
        return tr

    def _decode_chunked(self, audio: np.ndarray) -> str:
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
        return self._clean(stream.result.text)

    @staticmethod
    def _clean(text: str) -> str:
        # SenseVoice may emit tags like <|zh|><|NEUTRAL|>
        text = _TAGS.sub("", text).strip()
        # X-ASR emits a space after full-width punctuation
        return _PUNCT_SPACE.sub(r"\1", text)


class StreamingPreview:
    """Live captions while the hotkey is held (streaming X-ASR). Display only: the final text still comes from
    the selected offline model after release. Audio is fed from the recorder callback and decoded on its own
    thread so the audio callback never blocks."""

    def __init__(self, d: Path):
        self._rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(d / "tokens.txt"),
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.onnx"),
            joiner=str(d / "joiner.int8.onnx"),
            num_threads=1,
            sample_rate=SAMPLE_RATE,
            decoding_method="greedy_search",
        )
        self._stream = None
        self._pending: list[np.ndarray] = []
        self._cond = threading.Condition()
        self._active = False
        self.text = ""

    def start(self):
        with self._cond:
            self._stream = self._rec.create_stream()
            self._pending = []
            self._active = True
            self.text = ""
        threading.Thread(target=self._run, daemon=True).start()

    def feed(self, chunk: np.ndarray):
        with self._cond:
            if self._active:
                self._pending.append(chunk)
                self._cond.notify()

    def stop(self):
        with self._cond:
            self._active = False
            self._cond.notify()

    def _run(self):
        stream = self._stream
        while True:
            with self._cond:
                while self._active and not self._pending:
                    self._cond.wait()
                if not self._active:
                    return
                data = np.concatenate(self._pending)
                self._pending = []
            stream.accept_waveform(SAMPLE_RATE, data)
            while self._rec.is_ready(stream):
                self._rec.decode_stream(stream)
            text = self._clean(self._rec.get_result(stream))
            if self._stream is stream:
                self.text = text

    @staticmethod
    def _clean(text) -> str:
        text = text if isinstance(text, str) else getattr(text, "text", "")
        return _PUNCT_SPACE.sub(r"\1", text.strip())


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
