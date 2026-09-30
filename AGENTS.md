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
and `models/`. The models folder is fixed (the setting to move it was removed 2026-09-29). `paths.migrate_legacy()` moves the old in-app-folder data on first launch. Autostart is
re-registered on every launch so it follows the app if its folder moves. Next step, not started: package as an exe
with an installer (PyInstaller + Inno Setup).

## Models (fixed)

Decided 2026-09-29: the app stays light, with a fixed set of sherpa-onnx models and no model choice or LLM
(`models.py`). Download URL: `https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/<dir>.tar.bz2`.

| Role | Model (archive dir) | Size | Notes |
|---|---|---|---|
| Recognition | X-ASR int8 `sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03` | 130MB | ~0.75s for 18s audio; punctuation, good English |
| Second opinion | SenseVoice Small `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` | 155MB | ~0.1s per sentence; emits tags like `<\|zh\|>` that are stripped |

Live captions (streaming X-ASR 480ms, 134MB) were removed 2026-09-29: they appeared too slowly to help; the pill
shows only the level. Qwen3-ASR 0.6B (840MB, ~4.4s per 18s) and Fun-ASR-Nano (1GB, ~7.5s) were selectable before and
were removed with the picker. As a second opinion Qwen3-ASR gave better alternatives but took ~1.2GB of the user's 8GB machine.
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

## Settings (2026-09-29)

Only microphone, hotkey, strip trailing punctuation, autostart, "candidate menu for sounding-alike changes" (on by
default; off → the ✓ box is offered directly, as before 2026-09-30), menu timeout (one value for every menu and the ✓
box, default 10s, added 2026-09-30) and the hotwords page (cards in two columns). The
刪除 / 選字 trigger-word lists were removed 2026-09-30 with the spoken triggers. Selection edits, the second
opinion and on-screen terms are always on.

## Voice edits on selected text

Plan agreed 2026-09-27, cut down 2026-09-29, spoken triggers replaced by the hotkey 2026-09-30. The selection is
read when the hotkey goes down (`selection.py`, UI Automation TextPattern, on a background thread). On a selection:
one tap → candidate menu (opened DOUBLE_TAP_GAP after the release, so a double tap can cancel it); double tap →
Delete key; hold and speak → replaces it; double tap with the second press held → Delete at the second press, then
the dictation is pasted there. There are no spoken commands on a selection (刪除 / 選字 / saying the word again
were removed). When the new text sounds like what it replaced (`edit.similar`: same text, same syllables, one
character of the same sound, or similar Latin spelling), it is pasted and the candidate menu opens for it; a pick
there is what offers the hotword. CapsLock: the app, not `hotkey.py`, decides replays (`PushToTalk.tap()`). A
single tap is replayed at once (typing right after a CapsLock tap must not wait for UIA) and replayed again once
the probe finds a selection; other hotkeys are replayed only when there is no selection. Spelling letters,
教育的育, 改成X, 大寫/小寫 were removed: the user prefers selecting with the mouse and picking.
UIA probe (2026-09-27): Chrome/Edge inputs, Win11 Notepad, LINE give selection + line context; LINE's first read ~1.2s.
Terminals give no selection: Claude Code in Windows Terminal hides its mouse selection from UIA, Shift+arrow does
not select, and a paste goes to the caret.

Candidates (`candidates.py`): jieba's dict.txt (349k words + frequency + POS) indexed by fuzzy toneless pinyin,
cached in DATA_DIR/cache (build ~15s, load ~0.5s). Menu order: the second model's version of the word (when it
came from a recent dictation), the value of a learned hotword for it, then homophones by exact pinyin and frequency.
Words the user picked in any menu come first when they sound like the word (`picks.py`, DATA_DIR/picks.json, by
fuzzy syllables, most recent first; decided 2026-09-30). The menu after a sounding-alike replacement lists what was
pasted first and never the replaced text.

## Menus: mouse only (2026-09-29)

The menu (`picker.py`) is a non-activating window. Speech never answers it: short answers (對, 第二個) were often
misheard. The user clicks a row; a context hotword question has a last row 保留「X」 (what saying 不用 used to do);
✕ or the timeout just closes it. A white-gray bar along the bottom edge counts the timeout down (2026-09-30).

## Hotwords

Every replacement that looks like a correction is offered as a hotword (`hotwords.json` in DATA_DIR, key = wrong
text, value = correction) in a small box with a ✓ button; it is added only when the user clicks ✓ (decided
2026-09-29, replacing "add, then offer undo"; speech never answers this box so the next utterance cannot confirm it
by accident, and ✕ or the menu timeout declines). Offered when: Chinese words of 2+ characters whose syllables all match (fuzzy zh/z, ing/in, l/n …;
4+ characters may differ in one), or similar Latin spelling. A one-character
fix is learned with its neighbours (張雨薇: 雨 → 育 is stored as 張雨薇 → 張育薇). An existing hotword is never
overwritten by a different value (picking 乘勢 for 城市 in one sentence must not replace 城市 → 程式); that
sentence's words become a declined context instead. Changing a hotword's replacement back (程式 → 城市 by 選字,
re-saying or typing) is never offered as a reverse hotword: the sentence's words go to that hotword's `negatives`,
as if 保留 had been clicked (a reverse hotword would fight the original). Offer sources (2026-09-30): a pick in a menu, and a
hand-made edit (below). A replaced selection or re-dictation that sounds alike opens the candidate menu first; only
when it has nothing else to list (an English word) is the ✓ box offered directly.

Modes (settings: 一律取代 / 自動判斷): "always" replaces blindly. "context" (default) decides by the words around the key (`context_words`: content
words within 6 characters, function words dropped). Each hotword keeps the words seen where the value was right
(`contexts`: the sentence it was learned in, and every pick of the value) and where it was wrong (`negatives`:
every 保留). A declined
word wins, then a confirmed one; with neither the key is left as spoken and the menu asks 「城市」換成？ So
台北這個城市 is asked once and then left alone, 修這個城市的 bug is replaced. Hotword values also bias X-ASR (below).

## After a dictation (2026-09-29)

Voice commands on what was just dictated (復原, 送出, 換行, 刪掉上一句, X改成Y, 刪掉X, symbol names, trailing 送出) were
removed the same day they were built: the user corrects by selecting with the mouse instead. What remains:

- **Punctuation names** (restored 2026-09-30, `edit.symbol`): an utterance that is exactly 逗號 / 句號 / 句點 / 問號 /
  驚嘆號 / 感嘆號 / 頓號 / 分號 / 冒號 / 點點點 / 刪節號 / 省略號 types the mark, appended after any existing
  punctuation (the user chose not to replace it). Exact text only: 都好 is not 逗號. On a selection it replaces it.
  This is the only spoken command left.

- **Dictated-text buffer (`session.py`)**: the text we pasted is known to sit right before the caret until the user
  presses a key (keyboard hook, injected keys ignored), clicks (mouse hook; clicks on our menu ignored) or another
  window comes to the front. Changes to it are Backspace up to the edit point (one `SendInput` burst) + paste of
  the new tail; works in terminals too.
- **Double tap of the hotkey (`hotkey.py`)**, nothing selected, undoes the last change (dictation, replaced
  selection, picked suggestion); holding the second press records again, so it becomes a re-dictation. CapsLock: the
  two replayed taps cancel out; a held second press replays one extra tap to undo the first tap's toggle.
  Recording starts at key-down, but the indicator (overlay, tray icon) only after TAP_THRESHOLD, so taps and
  double taps do not flash it.
- **Re-dictation**: dictation deleted (Backspace/Delete without typing, or double tap) and re-said within 45s → one
  changed word that sounds alike (widened to the dictionary word around it: 黨案 → 檔案) gets the candidate menu.
- **Hand-made edits**: after a dictation, 3s after the user stops typing (within 120s), the focused control's visible
  text is read by UIA and compared with the dictation (`session.typed_fix`: longest prefix/suffix anchors, then
  `redictation_pair`). One changed word is offered (✓ box), once per dictation. Needs a TextPattern (not every app).
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
- **Clipboard**: every format is restored after a paste (images copied before dictating used to be lost).
- Tests without a microphone, with Windows TTS audio (zh-TW Hanhan voice via `dev/tts.ps1`):
  `dev/headless.py` runs the real App against a simulated text box with a private copy of `hotwords.json`; use it
  by default. (An earlier version restored the real file at the end while the user's VoiceInput was running and
  writing to it: never share the real file with a test.)
  `dev/e2e.py` types into a real Notepad / Windows Terminal window: only when the user is away from the machine
  (Win11 Notepad opens new windows as tabs, and a run once typed into the user's own Notepad document).
