"""Model location and downloader (sherpa-onnx pre-converted model)."""
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
BASE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"

# The single bundled model. Alternatives that were evaluated are documented in CLAUDE.md.
MODEL_LABEL = "X-ASR"
MODEL_DIR = MODELS_DIR / "sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03"
# Paths that must exist for the model to count as installed
MODEL_FILES = ["encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx",
               "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt", "bpe.model"]


def is_installed() -> bool:
    return all((MODEL_DIR / f).exists() for f in MODEL_FILES)


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


def download(progress=None) -> None:
    """Download and extract the model. progress(done_bytes, total_bytes) is optional."""
    if is_installed():
        return
    name = MODEL_DIR.name + ".tar.bz2"
    archive = MODELS_DIR / name
    fetch(BASE_URL + name, archive, progress)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(MODELS_DIR, filter="data")
    archive.unlink()
    if not is_installed():
        raise RuntimeError(f"Model extracted but expected files are missing in {MODEL_DIR}")


def _cli_progress(done, total):
    mb = 1 << 20
    if total:
        sys.stdout.write(f"\r  {done // mb} / {total // mb} MB ({done * 100 // total}%)")
    else:
        sys.stdout.write(f"\r  {done // mb} MB")
    sys.stdout.flush()


if __name__ == "__main__":
    if is_installed():
        print(f"[{MODEL_LABEL}] already installed")
    else:
        print(f"[{MODEL_LABEL}] downloading {MODEL_DIR.name} ...")
        download(_cli_progress)
        print(f"\n[{MODEL_LABEL}] done")
