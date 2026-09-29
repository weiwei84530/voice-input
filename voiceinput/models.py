"""ASR model registry and downloader (sherpa-onnx pre-converted models)."""
import sys
import tarfile
import urllib.request
from pathlib import Path

from .paths import DEFAULT_MODELS_DIR

BASE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"

# Fixed set (decided 2026-09-29: the app stays light, no model choice). "files" are the paths that must exist for
# the model to count as installed.
MAIN_MODEL = "xasr"            # recognition
SECOND_MODEL = "sensevoice"    # second opinion for suspects and 選字
MODELS = {
    MAIN_MODEL: {
        "short": "X-ASR",
        "dir": "sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03",
        "files": ["encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx",
                  "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt", "bpe.model"],
    },
    SECOND_MODEL: {
        "short": "SenseVoice",
        "dir": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        "files": ["model.int8.onnx", "tokens.txt"],
    },
}


def model_dir(key: str) -> Path:
    return DEFAULT_MODELS_DIR / MODELS[key]["dir"]


def is_installed(key: str) -> bool:
    d = model_dir(key)
    return all((d / f).exists() for f in MODELS[key]["files"])


def fetch(url: str, dest: Path, progress=None) -> None:
    """Download url to dest via a .part file. progress(done_bytes, total_bytes) is optional."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        while chunk := resp.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    tmp.replace(dest)


def download(key: str, progress=None) -> None:
    """Download and extract a model. progress(done_bytes, total_bytes) is optional."""
    if is_installed(key):
        return
    name = MODELS[key]["dir"] + ".tar.bz2"
    archive = DEFAULT_MODELS_DIR / name
    fetch(BASE_URL + name, archive, progress)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(DEFAULT_MODELS_DIR, filter="data")
    archive.unlink()
    if not is_installed(key):
        raise RuntimeError(f"Model {key} extracted but expected files are missing in {model_dir(key)}")


def _cli_progress(done, total):
    mb = 1 << 20
    if total:
        sys.stdout.write(f"\r  {done // mb} / {total // mb} MB ({done * 100 // total}%)")
    else:
        sys.stdout.write(f"\r  {done // mb} MB")
    sys.stdout.flush()


if __name__ == "__main__":
    from .paths import migrate_legacy
    migrate_legacy()
    keys = sys.argv[1:] or list(MODELS)
    for k in keys:
        if is_installed(k):
            print(f"[{k}] already installed")
            continue
        print(f"[{k}] downloading {MODELS[k]['dir']} ...")
        download(k, _cli_progress)
        print(f"\n[{k}] done")
