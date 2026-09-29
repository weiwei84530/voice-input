# VoiceInput — notes for coding agents

## Working with the user

- Whenever you can infer what the user is really after and see a better approach or a missing detail,
  stop and ask before building, as many rounds as needed, with the recommended option first.
- Don't guess between materially different designs; asking is cheaper than rebuilding.
- Ask clarifying questions with the AskUserQuestion tool (when available); use it freely.
- After every commit, restart VoiceInput if it is running so the user is testing the new code (don't start it if
  it isn't running). PowerShell:

  ```powershell
  $root = 'E:\Projects\voice-input'
  $procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$root\run.pyw*" }
  if ($procs) {
      $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
      $procs | ForEach-Object { Wait-Process -Id $_.ProcessId -Timeout 10 -ErrorAction SilentlyContinue }
      Start-Process "$root\.venv\Scripts\pythonw.exe" -ArgumentList "`"$root\run.pyw`"" -WorkingDirectory $root
  }
  ```

  Then check the tail of `%LOCALAPPDATA%\VoiceInput\voiceinput.log` for "就緒" / errors.

## File layout

Decided 2026-09-26 (the user plans to share the app with other users): code and runtime tools (`.tools/`, `.venv/`)
stay in the app folder; user data lives in `paths.DATA_DIR` (`%LOCALAPPDATA%\VoiceInput`): `config.json`, logs, lock
and `models/`. The models folder can be changed in settings (`Config.models_dir`); changing it moves the known
model folders over. `paths.migrate_legacy()` moves the old in-app-folder data on first launch. Autostart is
re-registered on every launch so it follows the app if its folder moves. Next step, not started: package as an exe
with an installer (PyInstaller + Inno Setup).

## Models (fixed)

Decided 2026-09-29: the app stays light, with a fixed set of sherpa-onnx models and no model choice or LLM
(`models.py`). Download URL: `https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/<dir>.tar.bz2`.

| Role | Model (archive dir) | Size | Notes |
|---|---|---|---|
| Recognition | X-ASR int8 `sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03` | 130MB | ~0.75s for 18s audio; punctuation, good English |
| Second opinion | SenseVoice Small `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` | 155MB | ~0.1s per sentence; emits tags like `<\|zh\|>` that are stripped |
| Live captions | X-ASR streaming 480ms int8 `sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05` | 134MB | display only |

Qwen3-ASR 0.6B (840MB, ~4.4s per 18s) and Fun-ASR-Nano (1GB, ~7.5s) were selectable before and were removed with the
picker. As a second opinion Qwen3-ASR gave better alternatives but took ~1.2GB of the user's 8GB machine.
Memory with everything on: ~1.1GB (was ~3GB with the LLM). The pinyin index held as Python objects is a large part.

## Text pipeline

`asr.py` (raw text) → `textfmt.format_text` → hotwords → paste. Order inside `format_text` matters: s2tw → filler
removal → disfluencies → 百分之 → numerals → spelled letters → 點 → percent → number+letter → English punctuation →
CJK/ASCII spacing → trailing punctuation.

- OpenCC `s2tw` only (glyph conversion). `s2twp` was dropped because it rewrites vocabulary (程序 → 程式).
- After s2tw, 臺 is mapped back to 台 (s2tw turns 台北 into 臺北; the user wants 台).
- 嗯 / 呃 are removed by regex.
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

## Voice edits on selected text

Plan agreed 2026-09-27. If text is selected when the hotkey goes down (`selection.py`, UI Automation TextPattern,
read on a background thread), the utterance edits the selection instead of being pasted as new text (`edit.py`):
刪除 → Delete key; 選字 or saying the selected word again → candidate menu; letters spelled one by one → that word,
with the selection's capitalisation (Cloud + C L A U D E → Claude); 教育的育 / 弓長張 → that character; 改成X → X;
大寫/小寫 and punctuation by name (。+ 改成逗號 → ，, 點點點 → ……, 句號改成問號, 好 + 問號 → 好？); anything else replaces
the selection. Instructions that need understanding (翻譯成英文, 改得有禮貌一點) are not supported. Commands are matched
by fuzzy pinyin because ASR hears 選字 as 選自.
UIA probe (2026-09-27): Chrome/Edge inputs, Win11 Notepad, LINE give selection + line context; LINE's first read ~1.2s.
Terminals give no selection: Claude Code in Windows Terminal hides its mouse selection from UIA, Shift+arrow does
not select, and a paste goes to the caret. Terminals are edited through the dictated-text buffer instead (below).

Candidates (`candidates.py`): jieba's dict.txt (349k words + frequency + POS) indexed by fuzzy toneless pinyin,
cached in DATA_DIR/cache (build ~15s, load ~0.5s). Menu order: the second model's version of the word (when it
came from a recent dictation), the value of a learned hotword for it, then homophones by exact pinyin and frequency.
The menu (`picker.py`) is a non-activating window below the selection; a click or saying 第二個 / 二 / 對 picks.

## Hotwords

Every replacement that looks like a correction is learned (`hotwords.json` in DATA_DIR, key = wrong text,
value = correction): spelled, Chinese words of 2+ characters whose syllables all match (fuzzy zh/z, ing/in, l/n …;
4+ characters may differ in one; 明天見 → 後天見 was once learned from 改成后天见), or similar Latin spelling. A one-character
fix is learned with its neighbours (張雨薇: 雨 → 育 is stored as 張雨薇 → 張育薇). An existing hotword is never
overwritten by a different value (picking 乘勢 for 城市 in one sentence must not replace 城市 → 程式). A tray
notification offers undo, as does saying 不要記.

Modes: "always" replaces blindly. "context" (default) decides by the words around the key (`context_words`: content
words within 6 characters, function words dropped). Each hotword keeps the words seen where the value was right
(`contexts`: the sentence it was learned in, and every 對) and where it was wrong (`negatives`: every 不用). A declined
word wins, then a confirmed one; with neither the key is left as spoken and the menu asks 「城市」要換成「程式」嗎. So
台北這個城市 is asked once and then left alone, 修這個城市的 bug is replaced. Hotword values also bias X-ASR (below).

## Hands-free editing (2026-09-29)

Goal from the user: fix text by voice without touching the keyboard or mouse.

- **Dictated-text buffer (`session.py`)**: the text we pasted is known to sit right before the caret until the user
  presses a key (keyboard hook, injected keys ignored), clicks (mouse hook; clicks on our menu ignored) or another
  window comes to the front. Edits to it are Backspace up to the edit point (one `SendInput` burst) + paste of the
  new tail. Works in terminals too (tested with cmd in Windows Terminal: 23 backspaces + paste, and Enter).
- **Voice commands (`commands.py`)**, without a selection: 復原 (undo stack of buffer states; also removes a hotword
  learned by that step), 不要記, 送出 (Enter), 換行 (Shift+Enter), 刪掉上一句, 全部刪掉, X改成Y / 不是X是Y, 刪掉X,
  bare symbol names (問號 replaces trailing punctuation). Matched by fuzzy pinyin (ASR wrote 復原 as 复员). X must be
  found in the buffer (exact, case-insensitive, or same pinyin; 城市改成城市 heard for 城市改成程式 opens the candidate
  menu); otherwise the utterance is dictated, so "把這個函式改成 async" still reaches Claude Code. A trailing 送出
  presses Enter only after a pause: ASR punctuation before it or ≥0.35s gap in X-ASR token times (TTS: 0.64s with a
  pause); 請把表單送出 is dictated. Enter is sent 150ms after the paste (WT reads the clipboard asynchronously).
- **Character descriptions** (edit.py): 教育的育, 偉大的偉字, 弓長張 (table of common ones). The character is taken from the
  describing word by sound because ASR writes 教育的欲. In a longer word the character that sounds alike is replaced
  (雨薇 + 教育的育 → 育薇). Works with 改成 too (玉改成教育的育).
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
- **Suspects** (`suspects.py`): SenseVoice re-decodes each dictation in the background; where it heard other Chinese
  characters and its version is the likelier word (a dictionary word where ours is not, or ≥20× as frequent), a
  menu offers it. Words a hotword produced are never offered back (jieba's frequencies are mainland: 城市 25084 vs
  程式 197). Rare, because both models often share an error (主機版, 剪貼布). Guessing from the dictionary alone was
  rejected: every flag on 80 real utterances was wrong.
- **Live captions**: the streaming model is fed from the recorder callback and shown above the pill.
- **Clipboard**: every format is restored after a paste (images copied before dictating used to be lost).
- Tests without a microphone, with Windows TTS audio (zh-TW Hanhan voice via `dev/tts.ps1`):
  `dev/headless.py` runs the real App against a simulated text box and restores `hotwords.json`; use it by default.
  `dev/e2e.py` types into a real Notepad / Windows Terminal window: only when the user is away from the machine
  (Win11 Notepad opens new windows as tabs, and a run once typed into the user's own Notepad document).
