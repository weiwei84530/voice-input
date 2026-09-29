# VoiceInput — notes for coding agents

## Working with the user

- Whenever you can infer what the user is really after and see a better approach or a missing detail,
  stop and ask before building, as many rounds as needed, with the recommended option first.
- Don't guess between materially different designs; asking is cheaper than rebuilding.
- Ask clarifying questions with the AskUserQuestion tool (when available); use it freely.
- After every commit, restart VoiceInput if it is running so the user is testing the new code (don't start it if
  it isn't running). Kill the llama-server too: the app doesn't stop it when killed. PowerShell:

  ```powershell
  $root = 'E:\Projects\voice-input'
  $procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$root\run.pyw*" -or $_.ExecutablePath -eq "$root\.tools\llama\llama-server.exe" }
  if ($procs) {
      $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
      $procs | ForEach-Object { Wait-Process -Id $_.ProcessId -Timeout 10 -ErrorAction SilentlyContinue }
      Start-Process "$root\.venv\Scripts\pythonw.exe" -ArgumentList "`"$root\run.pyw`"" -WorkingDirectory $root
  }
  ```

  Then check the tail of `%LOCALAPPDATA%\VoiceInput\voiceinput.log` for "llm loaded" / errors.

## File layout

Decided 2026-09-26 (the user plans to share the app with other users): code and runtime tools (`.tools/`, `.venv/`)
stay in the app folder; user data lives in `paths.DATA_DIR` (`%LOCALAPPDATA%\VoiceInput`): `config.json`, logs, lock
and `models/`. The models folder can be changed in settings (`Config.models_dir`); changing it moves the known
model folders over. `paths.migrate_legacy()` moves the old in-app-folder data on first launch. Autostart is
re-registered on every launch so it follows the app if its folder moves. Next step, not started: package as an exe
with an installer (PyInstaller + Inno Setup).

## ASR models

Four sherpa-onnx models are selectable in settings (`voiceinput/models.py`), default **X-ASR** int8.
The picker was removed once and restored on 2026-09-26 at the user's request. Loader code lives in `asr.py`.

### Model notes

Benchmarks: 18s zh/en test audio, CPU, `num_threads = min(4, cpu_count // 2)`.

| Model | Archive dir (sherpa-onnx `asr-models` release) | Size | 18s audio | Notes |
|---|---|---|---|---|
| X-ASR (default) | `sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03` | 130MB | ~0.75s | Punctuation, good English |
| SenseVoice Small | `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` | 155MB | ~0.8s | Punctuation, numbers as digits (ITN), weaker English; emits tags like `<\|zh\|><\|NEUTRAL\|>` that must be stripped |
| Qwen3-ASR 0.6B | `sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25` | 840MB | ~4.4s | Most accurate; 512-token limit, long audio is chunked (below) |
| Fun-ASR-Nano | `sherpa-onnx-funasr-nano-fp16-2025-12-30` | 1GB | ~7.5s | The int8 build returns empty output on some CPUs; use fp16 `llm` |

Download URL: `https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/<dir>.tar.bz2`

Qwen3-ASR long audio (fixed 2026-09-29): prompt + audio (~13 tokens/s) + output is capped at 512 tokens by the
model, and `max_new_tokens` defaults to 128. A 39s utterance came back as just "language". `asr._split` cuts audio
over 25s at the quietest 0.4s between 10–25s into the rest; `max_new_tokens` is 160. The model ends each chunk
with 。 even mid-sentence (又會。以), so a trailing 。 is dropped at joins. Short audio is untouched (0 of 50 real
clips changed). Fun-ASR-Nano (`max_new_tokens` 512, no total cap exposed) was not checked: model not installed.

### Other candidates seen (not evaluated)

- X-ASR fp32 (non-int8) `sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-2026-06-03`, ~560MB — same model, possibly slightly more accurate.
- X-ASR streaming variants (`...-160ms/480ms/960ms/1920ms-streaming-...-2026-06-05`) — the 480ms int8 one is now
  used for live captions only.
- Nemotron 3.5 ASR streaming 0.6B (multilingual, ~450MB int8) — reported weak on Chinese.

## Text pipeline

`asr.py` (raw text) → `textfmt.format_text` → `llm.py` (only if custom rules are enabled and non-empty) → paste.
Formatting before the LLM makes it see Traditional Chinese / API / 728, matching how rules are written (tested: rule
"API → 應用程式介面" failed on raw "A P I" and mangled the sentence, worked after formatting). There is deliberately no
formatting pass after the LLM: it changed 0 of 25 real LLM outputs, and it would override user rules such as
"add a trailing period" or "write numbers in Chinese". Order inside `format_text` matters: s2tw → filler removal →
disfluencies → 百分之 →
numerals → spelled letters → 點 → percent → number+letter → English punctuation → CJK/ASCII spacing → trailing punctuation.

- OpenCC `s2tw` only (glyph conversion). `s2twp` was dropped because it rewrites vocabulary (程序 → 程式).
- After s2tw, 臺 is mapped back to 台 (s2tw turns 台北 into 臺北; the user wants 台).
- 嗯 / 呃 are removed by regex (tested as an LLM rule first: it worked but sometimes over-deleted; regex is free and exact).
- Disfluencies (`remove_disfluencies`, 2026-09-29): repeated 2-4 character chunks (我們我們, 你可不可不可以, 今天今天),
  restarts (先打開那個，先打開設定頁 → 先打開設定頁) and stutters of a few characters (我我, 先先). ABAB reduplication is
  kept by jieba POS tag (研究研究, 討論討論 are verbs), as are counting (一個一個), laughter and 好的好的. 在 is not a
  stutter character (現在在猶豫). Over the ~400 logged utterances every change was a real repeat.
- Numerals: single-character numbers stay Chinese (一個, 兩個計劃, 第二種, 十個) unless part of a decimal,
  percentage or followed by a single letter; multi-character ones become digits (十五 → 15, 七百二十八 → 728).
  Also skipped: `_KEEP_WORDS` (統一, 星期三, 十分 …), ranges like 三四, anything with 幾, fractions.
- A number directly followed by a single letter is joined, case kept: 二 B → 2B, 五 h → 5h.
- `百分之X` and `<number> percent` → `X%`. English number words (eighty) are not converted.
- 點 becomes `.` only when both sides are digits or both are letters (三點 meeting stays).
- User hotwords (`hotwords.py`) are applied right after `format_text`, before the LLM; see below.

## Voice edits on selected text and hotwords

Plan agreed 2026-09-27. If text is selected when the hotkey goes down (`selection.py`, UI Automation TextPattern,
read on a background thread), the utterance edits the selection instead of being pasted as new text (`edit.py`):
刪除 → Delete key; 選字 → candidate menu; letters spelled one by one → that word, with the selection's capitalisation
(Cloud + C L A U D E → Claude); saying the selected word again → auto-pick the best other candidate; 改成X → X when X
sounds like the selection (2B echoes "改成程式" back instead of doing it); 大寫/小寫 and punctuation by name
(。+ 改成逗號 → ，, 點點點 → ……, 句號改成問號) → done in code (2B returned "，。" and left 點點點 unchanged);
otherwise any cue word (改, 換, 加上, 去掉, 翻譯, 這句, 一點, 問號, 禮貌 … — `_INSTRUCT`, chosen by the user on
2026-09-29 over an LLM classifier) → LLM (`prompts/edit.txt`); anything else → replaces the selection.
2B often returns the whole line or Simplified text, so `instruct` strips the surrounding context and converts to
Traditional. Tested 2026-09-29: 7/9 instructions right (politer, 句號改成問號, 加上請, 翻譯成英文, 後面加驚嘆號 …);
"改成標點符號的點點點" and "改得正式一點" came back unchanged, which is reported as a failure and leaves the text alone.
A bare symbol name (逗號) or 改成逗號 is also done in code (2026-09-29; the LLM turned 。 + 逗號 into the context
line): punctuation-only selection → the symbol, trailing punctuation → replaced (好。 → 好，), no punctuation →
appended (好 → 好？); punctuation only in the middle still goes to the LLM. `instruct` also rejects output that is
just the surrounding text echoed back, and non-punctuation output for a punctuation-only selection.
All LLM work (rank, judge, instruct, rewrite) shows the ring animation; the dots mean ASR only. Commands are matched by fuzzy
pinyin because ASR hears 選字 as 選自. Terminals are excluded: pasting there inserts at the prompt cursor.
UIA probe (2026-09-27): Chrome/Edge inputs, Win11 Notepad, LINE give selection + line context; LINE's first read ~1.2s.
Claude Code in Windows Terminal (tested 2026-09-29): its mouse selection is invisible to UIA (always empty), Shift+arrow
does not select, and a paste goes to the caret instead of replacing the highlight, so voice edits cannot work there.
UIA does report the text around the caret. Caret-based correction (click after the wrong word, say the right one)
was proposed and declined by the user; terminals stay plain dictation.

Candidates (`candidates.py`): jieba's dict.txt (349k words + frequency) indexed by fuzzy toneless pinyin, cached in
DATA_DIR/cache (build ~15s, load ~0.5s). Ranking (`LlmServer.rank`, `prompts/pick.txt`): the model is asked which
option fills the blank and answers with the word; we read first-token probabilities, and candidates sharing a first
token (選字/選自) are split by a second request with that token prefilled. Asking for a letter (A/B/C) instead was
strongly position-biased (reversed order flipped 4/8). Tested 2026-09-29: auto-pick 8/9 correct; the judge kept
台北這個城市 and fixed 修這個城市的 bug. llama-server has no prompt logprobs (`echo` is ignored), so full-sentence
scoring is not available. The menu (`picker.py`) is a non-activating window below the selection; a click or saying
第二個 / 二 while it is open pastes that row.

Every replacement that looks like a correction is learned as a hotword (`hotwords.json` in DATA_DIR, key = wrong
text, value = correction): spelled, Chinese words of 2+ characters with near-identical pinyin (fuzzy zh/z, ing/in,
l/n …), or similar Latin spelling. Any selection counts (the "only recent VoiceInput output" rule was dropped on
2026-09-29 at the user's request). A tray notification offers undo. Hotword modes: "always" replaces blindly;
"context" (default) replaces only if `rank` prefers the value in that sentence (4B test showed blind 城市 → 程式
breaks 台北這個城市); without the LLM running, context hotwords are skipped. The LLM runs while custom rules,
voice edits or the second opinion are enabled.

Learning was tightened on 2026-09-29: Chinese words must match in every syllable (fuzzy), 4+ character words may differ
in one (明天見 → 後天見 had been learned from 改成后天见). A one-character fix is learned with its neighbours
(張雨薇: 雨 → 育 is stored as 張雨薇 → 張育薇), never as a single character. Re-learning an existing identical hotword is
a no-op (undo once deleted the user's existing 城市 → 程式 because of this).

Not built yet: cross-script corrections (地符 → diff) are neither suggested nor learned.

## Hands-free editing (branch `smart-editing`, 2026-09-29)

Goal from the user: fix text by voice without touching the keyboard or mouse. Built in one pass after a review;
the user asked to see the whole result and adjust later.

- **Dictated-text buffer (`session.py`)**: the text we pasted is known to sit right before the caret until the user
  presses a key (keyboard hook, injected keys ignored), clicks (mouse hook; clicks on our menu ignored) or another
  window comes to the front. Edits to it are Backspace up to the edit point (one `SendInput` burst) + paste of the
  new tail. Works in terminals too, which UIA selection never could (Claude Code in Windows Terminal: tested with
  cmd in WT, 23 backspaces + paste, and Enter; Notepad tested the same way).
- **Voice commands (`commands.py`)**, without a selection: 復原 (undo stack of buffer states; also removes a hotword
  learned by that step), 不要記, 送出 (Enter), 換行 (Shift+Enter), 刪掉上一句, 全部刪掉, X改成Y / 不是X是Y, 刪掉X,
  bare symbol names (問號 replaces trailing punctuation). Matched by fuzzy pinyin (ASR wrote 復原 as 复员). X must be
  found in the buffer (exact, case-insensitive, or same pinyin, so 城市改成城市 heard for 城市改成程式 still finds it
  and then auto-picks the best homophone); otherwise the utterance is dictated, so "把這個函式改成 async" still reaches
  Claude Code. A trailing 送出 presses Enter only after a pause: ASR punctuation before it or ≥0.35s gap in X-ASR
  token times (TTS: 0.64s with a pause); 請把表單送出 is dictated. Enter is sent 150ms after the paste (WT reads the
  clipboard asynchronously).
- **Character descriptions** (edit.py): 教育的育, 偉大的偉字, 弓長張 (table of common ones). The character is taken from the
  describing word by sound because ASR writes 教育的欲. In a longer selection the character that sounds alike is
  replaced (雨薇 + 教育的育 → 育薇). Works with 改成 too (玉改成教育的育).
- **Re-dictation learning**: dictation deleted (Backspace/Delete without typing, or 刪掉上一句 / 復原) and re-said within
  45s → one changed word/phrase is learned (cloud → Claude, 城市 → 程式). Content changes fail `worth_learning`.
- **X-ASR hotword biasing** (`asr.py`): modified beam search + per-stream hotwords, same speed as greedy (0.11-0.22s
  on 3s clips). The model's vocab is pure BPE with CJK as "▁X" pieces: `modeling_unit="bpe"`, CJK hotwords
  space-separated and converted to Simplified, and a `bpe.vocab` exported from `bpe.model` by a tiny protobuf reader
  (sentencepiece is not installed). One unencodable word makes sherpa-onnx drop the whole list, so words are
  checked against tokens.txt. Results: L M → LLM, work tree → worktree at 2.0; cloud code → Claude Code needs 3.0
  (learned Latin values get 3.0); Chinese biasing barely works (張雨薇 → 張玉薇 at 3.0, 城市 never → 程式).
- **On-screen terms**: at key-down UIA reads the focused control's visible text (0.12s in WT) and English terms bias
  X-ASR at 1.5: CamelCase / acronyms / digits always, other 5+ letter words if seen twice, dotted names skipped.
- **Second opinion + suspects** (`suspects.py`): SenseVoice re-decodes each dictation in the background (0.1s);
  where it differs in Chinese characters, the LLM compares the two versions in context and a small menu offers the
  alternative if it gets ≥25% (2B leans mainland: 函數 0.71 vs 函式 0.29). 對 / 第幾個 picks, anything else closes.
  Qwen3-ASR was tried first: better alternatives but ~1.2GB and ~1s; the app reached 1.8GB beside the 2B LLM on
  the user's 8GB machine. Asking the LLM alone to scan homophones is useless (80 real utterances: every flag was
  wrong, 就是 → 就勢 0.79), and X-ASR token confidence was dropped for the same reason. Suspects are rare because
  both models often share an error (主機版, 剪貼布).
  The second model's version also feeds the 選字 menu when the selection came from a recent dictation.
- **Live captions**: streaming X-ASR 480ms int8 (aux model `xasr_stream`, 134MB) fed from the recorder callback,
  shown above the pill; display only, the final text still comes from the selected model.
- **Clipboard**: every format is restored after a paste (images copied before dictating used to be lost).
- Testing without a microphone: `dev/e2e.py` drives the real App with Windows TTS audio (zh-TW Hanhan voice via
  `dev/tts.ps1`) into a fresh Notepad or Windows Terminal window and reads the result back through UIA. It types
  into real windows and changes `hotwords.json`; back that up first.
- Memory with everything on: app ~1.1GB (X-ASR, streaming, SenseVoice, pinyin index) + llama-server ~1.9GB.

## LLM custom rules

Design (decided 2026-09-26): the LLM does **no built-in cleanup**. It only applies the rules the user types in settings
(`Config.llm_user_rules`) and must leave everything else unchanged. Disabled or empty rules → the LLM is not called.

Why: with a long built-in cleanup prompt, 2B changed 11 of 65 real utterances and about 6 of those were harmful
(deleted 我剛剛說 / 二 / 十分, turned 八十 into eighty); it never added quotes or fixed homophones, and it ignored user
rules. With the short rules-only prompt (`prompts/rewrite.txt`) it followed rules (cloud code → Claude Code)
and left unrelated sentences alone. "Add ？ to questions" was tested as a default rule and rejected: it added ？ to
statements too. A diff-based guard (keep only edits of allowed types) was prototyped and dropped for now because
user rules such as term replacement would be blocked by it.

- llama.cpp `llama-server` pinned to release `b11195`, auto-downloaded to `.tools/llama`; GGUF in `models/llm`.
- Only Qwen3.5 2B. 0.8B was removed: it mangled text (cloudCode → 云代码) or did nothing, for ~0.3s saved.
- Qwen3.5 4B was tested (2026-09-26) and not adopted. With the user's example-based rules it fixed 4/5 (dedupe 2/2,
  城市 → 程式 2/2) but applied the example as a blind replacement (台北這個城市 → 程式, 2 of 7 normal sentences damaged)
  and added trailing punctuation; without examples it fixed only 1/5. Neither 2B nor 4B does real context-based
  homophone correction (地符 → diff never fixed). Cost: ~1.9s/sentence vs 0.85s, ~3GB RAM, 88s load on 8GB machine.
- Small-model benchmark (2026-09-27, 244 real + labelled cases from the user's log): nothing beats
  Qwen3.5 2B. MiniCPM5 2B/1B, LFM2.5 1.2B and the Chinese correction models (chinese-text-correction 1.5B,
  ChineseErrorCorrector3 4B) all damage more normal sentences and fix fewer repeats. Four prompt styles moved 2B only
  between 14–18/44 repeats fixed, so the prompt is not the bottleneck. Homophones by context: 0/25 for every
  general model.
- Thinking mode on 2B (tested 2026-09-27, `--reasoning-budget`): unbounded it loops ("Wait, is there a repetition…")
  past 4096 tokens. Budget 256: repeats fixed 21/44 vs 18/44 without thinking, homophones still 0, ~20s per sentence;
  asking for brief thinking did not help (18/44). Budget 1024: 4/19 hard cases vs 3/19 at 256, ~80s per sentence.
  It thinks in English, restates the rules first (~200 tokens) and does not know the homophones (guessed 城市 → 規範).
  Not worth it.
- 2B cannot follow descriptive / example-style rules ("請依照語義修改…例如…"): 0/5. It only follows short imperative
  rules, and applies them mechanically.
- Measured on the user's i5-8500 / 8GB / no GPU: 2B ≈ 0.5–1.9s per sentence, ~1.9GB RAM, load 3–40s (cold disk).
- Prompts are plain text files in `voiceinput/prompts/` with `{{name}}` slots (`llm.render`), re-read on every call
  so they can be read and tweaked without touching code.
- The server runs while "啟用自訂規則", voice edits or the second opinion are enabled; the rules box is editable only
  once it is ready.
- Output longer than 1.5× input (+10) is discarded as a hallucination guard.
- Warm-up request with the current rules at load keeps the system prompt in the KV cache (first request ~0.5s
  instead of ~4s). Changing the rules makes the next request slow once.
- Known gap: if VoiceInput crashes, the llama-server child process is not killed automatically.
