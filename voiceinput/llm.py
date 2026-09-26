"""Optional LLM rewrite of the ASR result, served by a local llama.cpp llama-server process."""
import json
import os
import platform
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

from .models import MODELS_DIR, ROOT, fetch

LLAMA_TAG = "b11195"
LLAMA_DIR = ROOT / ".tools" / "llama"
LLM_DIR = MODELS_DIR / "llm"
_HF = "https://huggingface.co/unsloth/{repo}/resolve/main/{file}"

# Order here is the order shown in the settings dropdown ("" = off is added by the UI).
LLM_MODELS = {
    "qwen3.5-0.8b": {"label": "Qwen3.5 0.8B（~0.5GB）", "repo": "Qwen3.5-0.8B-GGUF",
                     "file": "Qwen3.5-0.8B-Q4_K_M.gguf"},
    "qwen3.5-2b": {"label": "Qwen3.5 2B（~1.2GB）", "repo": "Qwen3.5-2B-GGUF",
                   "file": "Qwen3.5-2B-Q4_K_M.gguf"},
}

DEFAULT_PROMPT = """你是語音輸入的文字校正器。使用者傳來的是語音辨識的原始結果，請只做以下修正：
1. 說話時改口、重說造成的重複片段，只保留最後、最完整的說法，刪掉前面被放棄的說法。
   例：「然後如果有各種如果有其他的方式的話」→「然後如果有其他的方式的話」
2. 其他文字一字不改：不要改寫、不要潤飾、不要摘要、不要回答內容、不要翻譯。
3. 只輸出校正後的文字，不要加任何說明或引號。"""

_THREADS = max(1, (os.cpu_count() or 2) - 1)


def _llama_asset() -> str:
    if sys.platform == "win32":
        return f"llama-{LLAMA_TAG}-bin-win-cpu-x64.zip"
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "x64"
    return f"llama-{LLAMA_TAG}-bin-macos-{arch}.tar.gz"


def server_path():
    name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    return next(LLAMA_DIR.rglob(name), None) if LLAMA_DIR.exists() else None


def model_path(key: str):
    return LLM_DIR / LLM_MODELS[key]["file"]


def is_installed(key: str) -> bool:
    return server_path() is not None and model_path(key).exists()


def download(key: str, progress=None) -> None:
    """Fetch llama-server (once) and the GGUF for key. progress(done_bytes, total_bytes) is optional."""
    if server_path() is None:
        asset = _llama_asset()
        archive = LLAMA_DIR / asset
        fetch(f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/{asset}", archive, progress)
        if asset.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(LLAMA_DIR)
        else:
            with tarfile.open(archive) as t:
                t.extractall(LLAMA_DIR, filter="data")
        archive.unlink()
        if sys.platform != "win32":
            server_path().chmod(0o755)
    m = LLM_MODELS[key]
    if not model_path(key).exists():
        fetch(_HF.format(repo=m["repo"], file=m["file"]), model_path(key), progress)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlmServer:
    def __init__(self, key: str):
        self.key = key
        self.label = LLM_MODELS[key]["label"].split("（")[0]
        self.port = _free_port()
        self._log = open(ROOT / "llama-server.log", "wb")
        self.proc = subprocess.Popen(
            [str(server_path()), "-m", str(model_path(key)), "--host", "127.0.0.1", "--port", str(self.port),
             "-c", "4096", "-np", "1", "-t", str(_THREADS), "--no-webui", "--reasoning", "off"],
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self._wait_ready()

    def _wait_ready(self, timeout: float = 120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited with code {self.proc.returncode} (see llama-server.log)")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.25)
        self.close()
        raise TimeoutError("llama-server did not become ready")

    def rewrite(self, text: str, prompt: str) -> str:
        body = {
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
            "temperature": 0,
            "max_tokens": len(text) * 2 + 32,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.load(r)["choices"][0]["message"]["content"].strip()
        # Guard against the model answering or rambling instead of correcting
        if not out or len(out) > len(text) * 1.5 + 10:
            return text
        return out

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._log.close()
