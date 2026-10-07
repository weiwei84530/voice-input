"""Text formatting applied to every result (after ASR, before hotwords)."""
import logging
import re

import opencc

log = logging.getLogger("voiceinput")

_s2tw = opencc.OpenCC("s2tw")  # glyphs only; s2twp would also swap vocabulary (程序 -> 程式)
_t2s = opencc.OpenCC("t2s")


def to_traditional(text: str) -> str:
    # s2tw turns 台 into 臺 (台北 -> 臺北); everyday Taiwanese writing uses 台
    return _s2tw.convert(text).replace("臺", "台")


def to_simplified(text: str) -> str:
    return _t2s.convert(text)

CJK = r"㐀-䶿一-鿿"
_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000}
_BIG = {"萬": 10_000, "億": 100_000_000}
NUM = "零〇一二兩三四五六七八九十百千萬億"
_NUM_RUN = re.compile(f"[{NUM}]+")

# Words that contain numerals but are not numbers; left untouched
_KEEP_WORDS = sorted("""
一下 一些 一起 一樣 一直 一定 一般 一切 一旦 一致 一律 一再 一向 一併 一同 一概 一整 一邊
一方面 一開始 一會兒 一陣 一時 一番 一口氣 一路 一旁 一流 一連串 一模一樣 一心 一半 一堆
一面 一帶 一手 一刻 一概 一瞬間 一目了然 一如既往 一舉兩得 一石二鳥 一五一十
統一 唯一 萬一 同一 不一 單一 專一 第一時間 之一
萬分 千萬 萬萬 百分百 七上八下 亂七八糟 五花八門 三心二意 四處 四周 四面八方 九成九
星期一 星期二 星期三 星期四 星期五 星期六 星期日 星期天
禮拜一 禮拜二 禮拜三 禮拜四 禮拜五 禮拜六 禮拜日 禮拜天
週一 週二 週三 週四 週五 週六 週日
""".split(), key=len, reverse=True)
# "一點" means "a bit" unless a numeral follows (一點五 = 1.5)
_KEEP_RE = re.compile(f"(?<![{NUM}])(?:" + "|".join(map(re.escape, _KEEP_WORDS))
                      + f"|十分(?!鐘)|一點(?![{NUM}]))|[{NUM}]*幾[{NUM}]*|[{NUM}]+分之[{NUM}]+")

_PERCENT = re.compile(f"百分之([{NUM}]+(?:點[{NUM}]+)?)")
_SPELLED = re.compile(r"(?<![A-Za-z])[A-Za-z](?: [A-Za-z](?![A-Za-z]))+")
_DECIMAL_AFTER = re.compile(f" ?點 ?[{NUM}\\d]")
_DECIMAL_BEFORE = re.compile(f"[{NUM}\\d] ?點 ?$")
_PERCENT_AFTER = re.compile(r" ?per ?cent\b", re.I)
_LETTER_AFTER = re.compile(r" ?[A-Za-z](?![A-Za-z])")
_LATIN_BEFORE = re.compile(r"[A-Za-z] ?$")     # M 三 -> M3, Opus 四 -> Opus 4 (not Python 三個月, L M 一次)
_CLASSIFIER_AFTER = re.compile("[個位種次天年月日號本台張件條隻塊元人歲秒分小週周倍份頁章層樓杯句字遍回項步套組顆支把片部首場家間名輛課題級季集]")
_KEEP_START = re.compile("|".join(map(re.escape, _KEEP_WORDS)))
_LETTER_DIGIT = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]) (\d)")
_DIGIT_LETTER = re.compile(r"(?<![A-Za-z])(\d+(?:\.\d+)?) ?([A-Za-z])(?![A-Za-z])")
_PERCENT_EN = re.compile(r"(\d+(?:\.\d+)?) ?per ?cent\b", re.I)
_DOT_DIGITS = re.compile(r"(?<=\d) ?點 ?(?=\d)")
_DOT_LETTERS = re.compile(r"(?<=[A-Za-z]) ?點 ?(?=[A-Za-z])")
_EN_PUNCT = re.compile(r"(?<=[A-Za-z0-9])\s*([，。？！；：])\s*(?=[A-Za-z])")
_HALF = str.maketrans("，。？！；：", ",.?!;:")
_SPACE_CJK_ASCII = re.compile(f"([{CJK}])\\s*([A-Za-z0-9])")
_SPACE_ASCII_CJK = re.compile(f"([A-Za-z0-9%])\\s*([{CJK}])")
_TRAILING_PUNCT = re.compile(r"[。，,.]+$")
_FILLER = re.compile(r"[嗯呃]+[，,、。]?\s*")

# Disfluencies (2026-09-29, from the user's log: 我們我們明天見, 你可不可不可以, 今天今天, 先打開那個，先打開設定頁).
# Repeated 2-4 character chunks are removed unless they are ABAB reduplication (研究研究, 討論討論: listed in the
# dictionary or in _ABAB), counting (一個一個) or laughter-like (哈哈哈哈). A-not-A (是不是不會, 能不能不用) is
# a repeat only when the A comes a third time (可不可不可以). Single repeated characters (選選字, 預預期, 很很) are
# judged by the dictionary in _dedupe_chars (2026-10-02).
_REPEAT = re.compile(rf"([{CJK}]{{2,4}})\1")
_RESTART = re.compile(rf"([{CJK}]{{3,6}})(?:那個|這個|就是)?[，,、 ]*\1")
_STUTTER = re.compile(r"([我你他她它就先把這那要會])\1(?!\1)")   # works before the dictionary has loaded
_CJK_CHAR = re.compile(f"[{CJK}]")
_DISFLUENT = set("還有 就是 然後 那個 這個 所以 因為 但是 如果 我們 你們 他們 我想 我要 你要 應該 可以 今天 明天 "
                 "現在 其實 反正 不過 而且 可是 還是 或是 我覺得 就是說".split())
_KEEP_REPEAT = set("好的 對啊 是的 沒錯 謝謝 拜拜".split())
# Verbs and adjectives said twice on purpose (討論討論 = discuss a bit) that the dictionary does not list as ABAB
_ABAB = set("討論 休息 檢查 了解 瞭解 學習 練習 準備 打掃 整理 研究 考慮 商量 介紹 說明 認識 收拾 活動 放鬆 輕鬆 "
            "熱鬧 高興 開心 溝通 比較 參考 思考 測試 觀察 體驗 欣賞 運動 散步 交流 調整 反省 檢討 討教 請教 "
            "涼快 暖和 舒服 乾淨 清楚 明白 快樂".split())
_CHAR_KEEP = set("在") | set(NUM)   # 現在在猶豫: 在 is a word after 現在; numbers are not stutters
_PREFER_RATIO = 20   # 選選字: the pair is a rare word, the first character + the next one is much more common


def _dedupe_repeat(m) -> str:
    x = m.group(1)
    if x in _KEEP_REPEAT or len(set(x)) == 1 or x[0] in NUM or x[0] == "每":
        return m.group(0)
    if len(x) == 2 and x[1] in "不沒" and m.string[m.end():m.end() + 1] != x[0]:
        return m.group(0)                        # 是不是不會, 有沒有沒: A-not-A followed by another word
    if x not in _DISFLUENT:
        from .candidates import is_word         # lazy: textfmt is imported before the dictionary is needed
        known = is_word(x + x)
        if known is None or known or x in _ABAB:   # None: dictionary not loaded yet, keep to be safe
            return m.group(0)
    return x


def _dedupe_chars(text: str) -> str:
    """Drop one of two identical characters when they do not belong to a word: 選選字 -> 選字, 很很 -> 很,
    but 剛剛, 框框, 渾渾噩噩, 試試看 (dictionary words), 髒髒的 (AA的), 開關關掉 (two words) stay."""
    from .candidates import frequency, is_word
    if is_word("的") is None:
        return text
    out, i = [], 0
    while i < len(text):
        x = text[i]
        if (i + 1 < len(text) and text[i + 1] == x and _CJK_CHAR.match(x) and x not in _CHAR_KEEP
                and text[i - 1:i] != x and text[i + 2:i + 3] != x and not _keep_pair(text, i, frequency, is_word)):
            out.append(x)
            log.info("stutter: %s -> %s", text[max(0, i - 2):i + 4], text[max(0, i - 2):i + 1] + text[i + 2:i + 4])
            i += 2
            continue
        out.append(x)
        i += 1
    return "".join(out)


def _keep_pair(text: str, i: int, frequency, is_word) -> bool:
    if text[i + 2:i + 3] in ("的", "地"):
        return True                              # 髒髒的, 慢慢地
    if i > 0 and is_word(text[i - 1:i + 1]) and is_word(text[i + 1:i + 3]):
        return True                              # 開關|關掉, 小時|時間, 勾選|選項
    best = max((text[s:s + n] for n in (2, 3, 4) for s in range(i + 2 - n, i + 1)
                if s >= 0 and s + n <= len(text) and is_word(text[s:s + n])), key=frequency, default=None)
    if best is None:
        return False
    if best == text[i:i + 2] and i + 3 <= len(text):
        after = text[i] + text[i + 2]            # 選選字: 選選 is rare, 選字 is the word meant
        return not (is_word(after) and frequency(after) >= _PREFER_RATIO * frequency(best))
    return True


def _dedupe_restart(m) -> str:
    x = m.group(1)
    return m.group(0) if x[0] in NUM or x[0] == "每" else x   # 一個字一個字 is counting, not a restart


def remove_disfluencies(text: str) -> str:
    text = _RESTART.sub(_dedupe_restart, text)
    text = _REPEAT.sub(_dedupe_repeat, text)
    text = _STUTTER.sub(r"\1", text)
    return _dedupe_chars(text)


def _parse_section(s: str) -> int | None:
    """Parse a numeral below 10000 such as 七百二十八, 十五, 兩千零五, 三百五 (=350)."""
    total, digit, last_unit = 0, None, None
    for ch in s:
        if ch in _DIGITS:
            if digit is not None and _DIGITS[ch] != 0 and digit != 0:
                return None                     # two digits in a row, e.g. 三四
            digit = _DIGITS[ch]
        elif ch in _UNITS:
            u = _UNITS[ch]
            if last_unit is not None and u >= last_unit:
                return None
            total += (1 if digit is None else digit) * u
            digit, last_unit = None, u
        else:
            return None
    if digit:
        # 三百五 -> 350, 兩千五 -> 2500 (a trailing digit right after a unit is one place lower)
        total += digit * (last_unit // 10 if last_unit and last_unit > 10 and s[-2] in _UNITS else 1)
    return total


def _parse_number(s: str) -> int | None:
    # Plain digit strings like 二零二六 (length >= 3, or containing 零); 三四 stays as a range
    if all(ch in _DIGITS for ch in s):
        if len(s) == 1 or len(s) >= 3 or "零" in s or "〇" in s:
            return int("".join(str(_DIGITS[ch]) for ch in s))
        return None
    total, rest = 0, s
    for big_ch, big in sorted(_BIG.items(), key=lambda kv: -kv[1]):
        if big_ch in rest:
            head, rest = rest.split(big_ch, 1)
            v = _parse_section(head) if head else 1
            if v is None:
                return None
            total += v * big
    if rest:
        v = _parse_section(rest.lstrip("零〇"))
        if v is None:
            return None
        total += v
    return total


def _convert_numbers(text: str) -> str:
    kept: list[str] = []

    def protect(m):
        kept.append(m.group(0))
        return f"{len(kept) - 1}"

    text = _KEEP_RE.sub(protect, text)

    def repl(m):
        s = m.group(0)
        if _DECIMAL_BEFORE.search(text, 0, m.start()) and all(ch in _DIGITS for ch in s):
            # decimals are read digit by digit: 點一五 -> .15, but 三點八一樣 is 3.8 一樣 and 三點五二 B is 3.5 2B
            k = next((k for k in range(1, len(s)) if _KEEP_START.match(text, m.start() + k)), len(s))
            digits = "".join(str(_DIGITS[ch]) for ch in s[:k])
            if k == len(s) > 1 and _LETTER_AFTER.match(text, m.end()):
                digits = digits[:-1] + " " + digits[-1]
            return digits + s[k:]
        # Single-character numbers stay in Chinese (一個, 兩個計劃, 第二種) unless part of a decimal (三點五), a
        # percentage (五 percent), next to a letter (二 B -> 2B, M 三 -> M3) or after an English word (Opus 四)
        if len(s) == 1 and not (_DECIMAL_AFTER.match(text, m.end())
                                or _PERCENT_AFTER.match(text, m.end())
                                or _LETTER_AFTER.match(text, m.end())
                                or (_LATIN_BEFORE.search(text, 0, m.start())
                                    and not _CLASSIFIER_AFTER.match(text, m.end()))):
            return s
        v = _parse_number(m.group(0))
        return m.group(0) if v is None else str(v)

    text = _NUM_RUN.sub(repl, text)
    return re.sub("(\\d+)", lambda m: kept[int(m.group(1))], text)


def _percent(m):
    whole, _, frac = m.group(1).partition("點")
    w = _parse_number(whole)
    if w is None:
        return m.group(0)
    if frac:
        f = "".join(str(_DIGITS.get(ch, "")) for ch in frac)
        return f"{w}.{f}%"
    return f"{w}%"


def strip_trailing(text: str) -> str:
    return _TRAILING_PUNCT.sub("", text)


def ends_with_punct(text: str) -> bool:
    return bool(_TRAILING_PUNCT.search(text))


def format_text(text: str, strip_trailing_punct: bool = False) -> str:
    text = to_traditional(text)
    text = _FILLER.sub("", text)
    text = remove_disfluencies(text)
    text = _PERCENT.sub(_percent, text)
    text = _convert_numbers(text)
    text = _SPELLED.sub(lambda m: m.group(0).replace(" ", "").upper(), text)
    text = _DOT_DIGITS.sub(".", text)
    text = _DOT_LETTERS.sub(".", text)
    text = _PERCENT_EN.sub(r"\1%", text)
    text = _DIGIT_LETTER.sub(r"\1\2", text)  # 5 h -> 5h, 2 B -> 2B
    text = _LETTER_DIGIT.sub(r"\1\2", text)  # M 3 -> M3, V 4.1 -> V4.1 (one letter only: Opus 4.8 keeps its space)
    text = _EN_PUNCT.sub(lambda m: m.group(1).translate(_HALF) + " ", text)
    text = _SPACE_CJK_ASCII.sub(r"\1 \2", text)
    text = _SPACE_ASCII_CJK.sub(r"\1 \2", text)
    if strip_trailing_punct:
        text = _TRAILING_PUNCT.sub("", text)
    return text
