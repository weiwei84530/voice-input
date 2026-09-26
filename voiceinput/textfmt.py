"""Final text formatting applied to every result (after ASR / LLM, before paste)."""
import re

import opencc

_s2tw = opencc.OpenCC("s2tw")  # glyphs only; s2twp would also swap vocabulary (程序 -> 程式)

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
_DIGIT_LETTER = re.compile(r"(?<![A-Za-z])(\d+(?:\.\d+)?) ?([A-Za-z])(?![A-Za-z])")
_PERCENT_EN = re.compile(r"(\d+(?:\.\d+)?) ?per ?cent\b", re.I)
_DOT_DIGITS = re.compile(r"(?<=\d) ?點 ?(?=\d)")
_DOT_LETTERS = re.compile(r"(?<=[A-Za-z]) ?點 ?(?=[A-Za-z])")
_EN_PUNCT = re.compile(r"(?<=[A-Za-z0-9])\s*([，。？！；：])\s*(?=[A-Za-z])")
_HALF = str.maketrans("，。？！；：", ",.?!;:")
_SPACE_CJK_ASCII = re.compile(f"([{CJK}])\\s*([A-Za-z0-9])")
_SPACE_ASCII_CJK = re.compile(f"([A-Za-z0-9%])\\s*([{CJK}])")
_TRAILING_PUNCT = re.compile(r"[。，,.]+$")


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
        # Single-character numbers stay in Chinese (一個, 兩個計劃, 第二種) unless part of a
        # decimal (三點五), a percentage (五 percent) or a unit letter (二 B -> 2B)
        if len(m.group(0)) == 1 and not (_DECIMAL_AFTER.match(text, m.end())
                                         or _DECIMAL_BEFORE.search(text, 0, m.start())
                                         or _PERCENT_AFTER.match(text, m.end())
                                         or _LETTER_AFTER.match(text, m.end())):
            return m.group(0)
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


def format_text(text: str, strip_trailing_punct: bool = True) -> str:
    text = _s2tw.convert(text)
    text = _PERCENT.sub(_percent, text)
    text = _convert_numbers(text)
    text = _SPELLED.sub(lambda m: m.group(0).replace(" ", "").upper(), text)
    text = _DOT_DIGITS.sub(".", text)
    text = _DOT_LETTERS.sub(".", text)
    text = _PERCENT_EN.sub(r"\1%", text)
    text = _DIGIT_LETTER.sub(r"\1\2", text)  # 5 h -> 5h, 2 B -> 2B
    text = _EN_PUNCT.sub(lambda m: m.group(1).translate(_HALF) + " ", text)
    text = _SPACE_CJK_ASCII.sub(r"\1 \2", text)
    text = _SPACE_ASCII_CJK.sub(r"\1 \2", text)
    if strip_trailing_punct:
        text = _TRAILING_PUNCT.sub("", text)
    return text
