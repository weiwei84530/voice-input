"""Optional LLM pass that applies the user's custom rules, served by a local llama.cpp llama-server."""
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

LLM_LABEL = "Qwen3.5 2B"
LLM_REPO = "Qwen3.5-2B-GGUF"
LLM_FILE = "Qwen3.5-2B-Q4_K_M.gguf"
LLM_PATH = LLM_DIR / LLM_FILE

# The model only applies the user's rules; all built-in cleanup lives in textfmt.
# A short prompt matters: longer prompts with built-in rules made the 2B model ignore user rules.
SYSTEM_PROMPT = """你會收到一段語音輸入的文字。請依照下列規則修改這段文字，規則沒有提到的地方一字不改，原樣輸出。
不要回答或評論文字內容，只輸出修改後的文字。

規則：
{rules}"""

_THREADS = max(1, (os.cpu_count() or 2) - 1)


def _llama_asset() -> str:
    if sys.platform == "win32":
        return f"llama-{LLAMA_TAG}-bin-win-cpu-x64.zip"
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "x64"
    return f"llama-{LLAMA_TAG}-bin-macos-{arch}.tar.gz"


def server_path():
    name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    return next(LLAMA_DIR.rglob(name), None) if LLAMA_DIR.exists() else None


def is_installed() -> bool:
    return server_path() is not None and LLM_PATH.exists()


def download(progress=None) -> None:
    """Fetch llama-server and the GGUF if missing. progress(done_bytes, total_bytes) is optional."""
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
    if not LLM_PATH.exists():
        fetch(_HF.format(repo=LLM_REPO, file=LLM_FILE), LLM_PATH, progress)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlmServer:
    def __init__(self, rules: str = ""):
        self.port = _free_port()
        self._log = open(ROOT / "llama-server.log", "wb")
        self.proc = subprocess.Popen(
            [str(server_path()), "-m", str(LLM_PATH), "--host", "127.0.0.1", "--port", str(self.port),
             "-c", "4096", "-np", "1", "-t", str(_THREADS), "--no-webui", "--reasoning", "off"],
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self._wait_ready()
        # Warm up so the system prompt is already in the KV cache for the first real request
        if rules.strip():
            self.rewrite("你好", rules)

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

    def rewrite(self, text: str, rules: str) -> str:
        body = {
            "messages": [{"role": "system", "content": SYSTEM_PROMPT.replace("{rules}", rules.strip())},
                         {"role": "user", "content": text}],
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
