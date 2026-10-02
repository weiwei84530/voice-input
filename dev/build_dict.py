"""Build voiceinput/dict/words.tsv.gz from McBopomofo's Taiwanese word list (MIT licence).

  python dev/build_dict.py

Each line: word <TAB> Bopomofo reading (space-separated syllables with tones) <TAB> frequency.
Phrases (2-6 characters) come from BPMFMappings.txt, single characters (Big5 only) from BPMFBase.txt, frequencies
from phrase.occ (occurrences in McBopomofo's Taiwanese corpus; 1 for listed words never counted). A word with
several readings has one line per reading.
"""
import gzip
import urllib.request
from pathlib import Path

COMMIT = "be6564acad6c4d3265c34a2e1a872d80f9db6068"
BASE = f"https://raw.githubusercontent.com/openvanilla/McBopomofo/{COMMIT}/Source/Data/"
OUT = Path(__file__).resolve().parent.parent / "voiceinput" / "dict" / "words.tsv.gz"


def fetch(name: str) -> list[str]:
    with urllib.request.urlopen(BASE + name) as r:
        return r.read().decode("utf-8").splitlines()


def main():
    occ = {}
    for line in fetch("phrase.occ"):
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            occ[parts[0]] = int(parts[1])
    rows = {}
    for line in fetch("BPMFBase.txt"):
        parts = line.split()
        if len(parts) >= 5 and parts[-1] == "big5" and len(parts[0]) == 1 and "㐀" <= parts[0]:
            rows[(parts[0], parts[1])] = None
    for line in fetch("BPMFMappings.txt"):
        word, *reading = line.split()
        if 2 <= len(word) <= 6 and len(reading) == len(word):
            rows[(word, " ".join(reading))] = None
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        for word, reading in rows:
            f.write(f"{word}\t{reading}\t{max(occ.get(word, 0), 1)}\n")
    print(f"{len(rows)} entries -> {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
