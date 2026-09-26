"""ASR model registry and downloader (sherpa-onnx pre-converted models)."""
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
BASE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"

# Order here is the order shown in the settings dropdown.
# "files" are the paths that must exist for the model to count as installed.
MODELS = {
    "xasr": {
        "label": "X-ASR（最快最輕，英文佳，~130MB）",
        "short": "X-ASR",
        "dir": "sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03",
        "files": ["encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx",
                  "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt", "bpe.model"],
    },
    "sensevoice": {
        "label": "SenseVoice Small（快，英文較弱，~160MB）",
        "short": "SenseVoice",
        "dir": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        "files": ["model.int8.onnx", "tokens.txt"],
    },
    "qwen3_asr": {
        "label": "Qwen3-ASR 0.6B（最準，約慢 5 倍，~840MB）",
        "short": "Qwen3-ASR",
        "dir": "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        "files": ["conv_frontend.onnx", "encoder.int8.onnx", "decoder.int8.onnx", "tokenizer"],
    },
    "funasr_nano": {
        "label": "Fun-ASR-Nano（最慢，~1GB）",
        "short": "Fun-ASR-Nano",
        "dir": "sherpa-onnx-funasr-nano-fp16-2025-12-30",
        "files": ["encoder_adaptor.int8.onnx", "llm.fp16.onnx", "embedding.int8.onnx", "Qwen3-0.6B"],
    },
}
DEFAULT_MODEL = "xasr"


def model_dir(key: str) -> Path:
    return MODELS_DIR / MODELS[key]["dir"]


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
    archive = MODELS_DIR / name
    fetch(BASE_URL + name, archive, progress)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(MODELS_DIR, filter="data")
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
    keys = sys.argv[1:] or [DEFAULT_MODEL]
    for k in keys:
        if is_installed(k):
            print(f"[{k}] already installed")
            continue
        print(f"[{k}] downloading {MODELS[k]['dir']} ...")
        download(k, _cli_progress)
        print(f"\n[{k}] done")
