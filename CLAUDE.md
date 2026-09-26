# VoiceInput — notes for Claude

## Working with the user

- Whenever you can infer what the user is really after and see a better approach or a missing detail,
  stop and ask before building. Use the AskUserQuestion tool freely, as many rounds as needed, and put
  the recommended option first.
- Don't guess between materially different designs; asking is cheaper than rebuilding.

## ASR models

Four sherpa-onnx models are selectable in settings (`voiceinput/models.py`), default **X-ASR** int8.
The picker was removed once and restored on 2026-09-26 at the user's request. Loader code lives in `asr.py`.

### Model notes

Benchmarks: 18s zh/en test audio, CPU, `num_threads = min(4, cpu_count // 2)`.

| Model | Archive dir (sherpa-onnx `asr-models` release) | Size | 18s audio | Notes |
|---|---|---|---|---|
| X-ASR (default) | `sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03` | 130MB | ~0.75s | Punctuation, good English |
| SenseVoice Small | `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` | 155MB | ~0.8s | Punctuation, numbers as digits (ITN), weaker English; emits tags like `<\|zh\|><\|NEUTRAL\|>` that must be stripped |
| Qwen3-ASR 0.6B | `sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25` | 840MB | ~4.4s | Most accurate |
| Fun-ASR-Nano | `sherpa-onnx-funasr-nano-fp16-2025-12-30` | 1GB | ~7.5s | The int8 build returns empty output on some CPUs; use fp16 `llm` |

Download URL: `https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/<dir>.tar.bz2`

### Other candidates seen (not evaluated)

- X-ASR fp32 (non-int8) `sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-2026-06-03`, ~560MB — same model, possibly slightly more accurate.
- X-ASR streaming variants (`...-160ms/480ms/960ms/1920ms-streaming-...-2026-06-05`) — would allow live partial results.
- Nemotron 3.5 ASR streaming 0.6B (multilingual, ~450MB int8) — reported weak on Chinese.

## Text pipeline

`asr.py` (raw text, fixes X-ASR's space after full-width punctuation) → `llm.py` (only if custom rules are enabled
and non-empty) → `textfmt.format_text` → paste. Order inside `format_text` matters: s2tw → filler removal → 百分之 →
numerals → spelled letters → 點 → percent → number+letter → English punctuation → CJK/ASCII spacing → trailing punctuation.

- OpenCC `s2tw` only (glyph conversion). `s2twp` was dropped because it rewrites vocabulary (程序 → 程式).
- 嗯 / 呃 are removed by regex (tested as an LLM rule first: it worked but sometimes over-deleted; regex is free and exact).
- Numerals: single-character numbers stay Chinese (一個, 兩個計劃, 第二種, 十個) unless part of a decimal,
  percentage or followed by a single letter; multi-character ones become digits (十五 → 15, 七百二十八 → 728).
  Also skipped: `_KEEP_WORDS` (統一, 星期三, 十分 …), ranges like 三四, anything with 幾, fractions.
- A number directly followed by a single letter is joined, case kept: 二 B → 2B, 五 h → 5h.
- `百分之X` and `<number> percent` → `X%`. English number words (eighty) are not converted.
- 點 becomes `.` only when both sides are digits or both are letters (三點 meeting stays).
- Custom vocabulary replacement (e.g. cloud code → Claude Code) is intentionally not done yet; the user plans a dedicated feature.

## LLM custom rules

Design (decided 2026-09-26): the LLM does **no built-in cleanup**. It only applies the rules the user types in settings
(`Config.llm_user_rules`) and must leave everything else unchanged. Disabled or empty rules → the LLM is not called.

Why: with a long built-in cleanup prompt, 2B changed 11 of 65 real utterances and about 6 of those were harmful
(deleted 我剛剛說 / 二 / 十分, turned 八十 into eighty); it never added quotes or fixed homophones, and it ignored user
rules. With the short rules-only prompt (`SYSTEM_PROMPT` in `llm.py`) it followed rules (cloud code → Claude Code)
and left unrelated sentences alone. "Add ？ to questions" was tested as a default rule and rejected: it added ？ to
statements too. A diff-based guard (keep only edits of allowed types) was prototyped and dropped for now because
user rules such as term replacement would be blocked by it.

- llama.cpp `llama-server` pinned to release `b11195`, auto-downloaded to `.tools/llama`; GGUF in `models/llm`.
- Only Qwen3.5 2B. 0.8B was removed: it mangled text (cloudCode → 云代码) or did nothing, for ~0.3s saved.
- Measured on the user's i5-8500 / 8GB / no GPU: 2B ≈ 0.5–1.9s per sentence, ~1.9GB RAM, load 3–40s (cold disk).
- The server is only running while "啟用自訂規則" is checked; the rules box is editable only once it is ready.
- Output longer than 1.5× input (+10) is discarded as a hallucination guard.
- Warm-up request with the current rules at load keeps the system prompt in the KV cache (first request ~0.5s
  instead of ~4s). Changing the rules makes the next request slow once.
- Known gap: if VoiceInput crashes, the llama-server child process is not killed automatically.
