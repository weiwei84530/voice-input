# VoiceInput — notes for Claude

## Working with the user

- Whenever you can infer what the user is really after and see a better approach or a missing detail,
  stop and ask before building. Use the AskUserQuestion tool freely, as many rounds as needed, and put
  the recommended option first.
- Don't guess between materially different designs; asking is cheaper than rebuilding.

## ASR model

The app ships with a single model: **X-ASR** int8 zipformer transducer
(`sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03`, see `voiceinput/models.py`).
There is no model picker in the UI on purpose.

### Previously supported alternatives (removed, kept here for reference)

Benchmarks: 18s zh/en test audio, CPU, `num_threads = min(4, cpu_count // 2)`.

| Model | Archive dir (sherpa-onnx `asr-models` release) | Size | 18s audio | Notes |
|---|---|---|---|---|
| X-ASR (current) | `sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03` | 130MB | ~0.75s | Punctuation, good English |
| SenseVoice Small | `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` | 155MB | ~0.8s | Punctuation, numbers as digits (ITN), weaker English; emits tags like `<\|zh\|><\|NEUTRAL\|>` that must be stripped |
| Qwen3-ASR 0.6B | `sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25` | 840MB | ~4.4s | Most accurate |
| Fun-ASR-Nano | `sherpa-onnx-funasr-nano-fp16-2025-12-30` | 1GB | ~7.5s | The int8 build returns empty output on some CPUs; use fp16 `llm` |

Download URL: `https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/<dir>.tar.bz2`

Loader code for the removed models (`d` = model dir):

```python
# SenseVoice Small — files: model.int8.onnx, tokens.txt
sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=str(d / "model.int8.onnx"), tokens=str(d / "tokens.txt"),
    num_threads=_THREADS, language="auto", use_itn=True)

# Qwen3-ASR 0.6B — files: conv_frontend.onnx, encoder.int8.onnx, decoder.int8.onnx, tokenizer/
sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
    conv_frontend=str(d / "conv_frontend.onnx"), encoder=str(d / "encoder.int8.onnx"),
    decoder=str(d / "decoder.int8.onnx"), tokenizer=str(d / "tokenizer"), num_threads=_THREADS)

# Fun-ASR-Nano — files: encoder_adaptor.int8.onnx, llm.fp16.onnx, embedding.int8.onnx, Qwen3-0.6B/
sherpa_onnx.OfflineRecognizer.from_funasr_nano(
    encoder_adaptor=str(d / "encoder_adaptor.int8.onnx"), llm=str(d / "llm.fp16.onnx"),
    embedding=str(d / "embedding.int8.onnx"), tokenizer=str(d / "Qwen3-0.6B"),
    num_threads=_THREADS, itn=True)
```

### Other candidates seen (not evaluated)

- X-ASR fp32 (non-int8) `sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-2026-06-03`, ~560MB — same model, possibly slightly more accurate.
- X-ASR streaming variants (`...-160ms/480ms/960ms/1920ms-streaming-...-2026-06-05`) — would allow live partial results.
- Nemotron 3.5 ASR streaming 0.6B (multilingual, ~450MB int8) — reported weak on Chinese.

## Text pipeline

`asr.py` (raw text, fixes X-ASR's space after full-width punctuation) → `llm.py` (optional rewrite) →
`textfmt.format_text` → paste. Order inside `format_text` matters: s2tw → 百分之 → numerals → spelled letters →
點 → English punctuation → CJK/ASCII spacing → trailing punctuation.

- OpenCC `s2tw` only (glyph conversion). `s2twp` was dropped because it rewrites vocabulary (程序 → 程式).
- Numerals: single-character numbers stay Chinese (一個, 兩個計劃, 第二種, 十個) unless part of a decimal or
  percentage; multi-character ones become digits (十五 → 15, 七百二十八 → 728). Also skipped: `_KEEP_WORDS`
  (統一, 星期三, 十分 …), ranges like 三四, anything with 幾, fractions.
- `百分之X` and `<number> percent` → `X%`. English number words (eighty) are not converted.
- 點 becomes `.` only when both sides are digits or both are letters (三點 meeting stays).
- Custom vocabulary replacement (e.g. cloud code → Claude Code) is intentionally not done yet; the user plans a dedicated feature.

## LLM rewrite

- llama.cpp `llama-server` pinned to release `b11195`, auto-downloaded to `.tools/llama`; GGUFs in `models/llm`.
- Measured on the user's i5-8500 / 8GB / no GPU: 0.8B ≈ 0.5s per sentence but mangles text (cloudCode → 云代码);
  2B ≈ 0.7–1.9s, conservative. 2B server uses ~1.9GB RAM; cold load 15–40s.
- Output longer than 1.5× input (+10) is discarded as a hallucination guard.
- Prompt: `voiceinput/prompts/rewrite.txt` (developer-facing, re-read on every request). `{{user_rules}}` is replaced
  with the rules typed in settings (`Config.llm_user_rules`), which the template says override the built-in rules.
  The transcript is sent as a separate user message, not inlined, so the model treats it as data.
- The prompt tells the model not to touch numerals; `textfmt` decides numeral style after the LLM.
- The server warms up with the current user rules so the system prompt is in the KV cache (first request ~0.5s
  instead of ~4s). Changing the rules makes the next request slow once.
- 2B does not reliably follow user rules such as term replacement (tested: rules at the end, at the top, and inside
  the user message all failed for cloudCode → Claude Code).
- Known gap: if VoiceInput crashes, the llama-server child process is not killed automatically.
