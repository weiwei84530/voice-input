"""Optional LLM pass that applies the user's custom rules, served by a local llama.cpp llama-server."""
import json
import math
import os
import platform
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

from . import models
from .models import fetch
from .paths import APP_DIR, LLAMA_LOG_PATH
from .textfmt import to_traditional

LLAMA_TAG = "b11195"
LLAMA_DIR = APP_DIR / ".tools" / "llama"
_HF = "https://huggingface.co/unsloth/{repo}/resolve/main/{file}"

LLM_LABEL = "Qwen3.5 2B"
LLM_REPO = "Qwen3.5-2B-GGUF"
LLM_FILE = "Qwen3.5-2B-Q4_K_M.gguf"


def llm_path():
    return models.models_dir() / models.LLM_SUBDIR / LLM_FILE

# Prompts live in prompts/*.txt so they are easy to read and tweak; {{name}} slots are filled by render().
# They are re-read on every call, so an edited file takes effect without restarting.
# rewrite.txt: the model only applies the user's rules; all built-in cleanup lives in textfmt. A short prompt
# matters: longer prompts with built-in rules made the 2B model ignore user rules.
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def render(name: str, **slots: str) -> str:
    text = (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    for key, value in slots.items():
        text = text.replace("{{" + key + "}}", value)
    return text.strip()

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
    return server_path() is not None and llm_path().exists()


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
    if not llm_path().exists():
        fetch(_HF.format(repo=LLM_REPO, file=LLM_FILE), llm_path(), progress)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlmServer:
    def __init__(self, rules: str = ""):
        self.port = _free_port()
        self._log = open(LLAMA_LOG_PATH, "wb")
        self.proc = subprocess.Popen(
            [str(server_path()), "-m", str(llm_path()), "--host", "127.0.0.1", "--port", str(self.port),
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

    def _chat(self, messages: list, max_tokens: int, top_logprobs: int = 0) -> dict:
        body = {"messages": messages, "temperature": 0, "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False}}
        if top_logprobs:
            body.update(logprobs=True, top_logprobs=top_logprobs)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["choices"][0]

    def rewrite(self, text: str, rules: str) -> str:
        messages = [{"role": "system", "content": render("rewrite", rules=rules.strip())},
                    {"role": "user", "content": text}]
        out = self._chat(messages, len(text) * 2 + 32)["message"]["content"].strip()
        # Guard against the model answering or rambling instead of correcting
        if not out or len(out) > len(text) * 1.5 + 10:
            return text
        # On very short input (e.g. "Claude") 2B sometimes outputs a rule itself instead of the text
        echoed = to_traditional(out)
        for rule in rules.splitlines():
            rule = rule.strip().strip("。！？.!?")
            if len(rule) >= 4 and rule in echoed and rule not in text:
                return text
        return out

    def rank(self, before: str, after: str, candidates: list[str]) -> list[tuple[float, str]]:
        """Score which candidate best fills the blank in before＿＿after, best first.

        The model answers with the word itself and we read the probabilities of its first token. Asking for a
        letter (A/B/C) instead was strongly position-biased: reversing the option order flipped 4 of 8 answers.
        Candidates sharing a first token (選字 / 選自) are split by a second request that prefills that token.
        """
        prompt = render("pick", sentence=f"{before}＿＿{after}", options="、".join(candidates))
        scores = dict.fromkeys(candidates, 0.0)
        self._split([{"role": "user", "content": prompt}], "", candidates, 1.0, scores, depth=0)
        return sorted(((p, c) for c, p in scores.items()), key=lambda pc: -pc[0])

    def _split(self, messages, prefix, group, mass, scores, depth):
        """Share mass among the candidates in group (all starting with prefix) by the next-token probabilities."""
        msgs = messages + ([{"role": "assistant", "content": prefix}] if prefix else [])
        tops = self._chat(msgs, 1, top_logprobs=20)["logprobs"]["content"][0]["top_logprobs"]
        got = dict.fromkeys(group, 0.0)
        subgroups: dict[str, float] = {}
        for t in tops:
            tok = t["token"] if prefix else t["token"].lstrip()
            p = math.exp(t["logprob"])
            match = [c for c in group if tok and c[len(prefix):].startswith(tok)]
            if len(match) == 1:
                got[match[0]] += p
            elif len(match) > 1:
                subgroups[tok] = subgroups.get(tok, 0.0) + p
        finished = [c for c in group if c == prefix]   # candidate fully spelled out already
        total = sum(got.values()) + sum(subgroups.values())
        for c in finished:
            got[c] += max(0.0, 1.0 - total) / len(finished)
            total = 1.0
        norm = total or 1.0
        for c, p in got.items():
            scores[c] += mass * p / norm
        for tok, p in subgroups.items():
            sub = [c for c in group if c[len(prefix):].startswith(tok)]
            if depth < 2 and mass * p / norm > 0.02:
                self._split(messages, prefix + tok, sub, mass * p / norm, scores, depth + 1)
            else:
                for c in sub:
                    scores[c] += mass * p / norm / len(sub)

    def instruct(self, before: str, selected: str, after: str, instruction: str) -> str:
        """Rewrite the selected text as the spoken instruction asks (e.g. 翻譯成英文). Returns selected on failure."""
        prompt = render("edit", before=before, selected=selected, after=after, instruction=instruction)
        out = self._chat([{"role": "user", "content": prompt}], len(selected) * 3 + 48)["message"]["content"]
        out = to_traditional(out.strip().strip("【】[]\"'"))   # not 「」: "加上引號" must keep them
        # 2B often returns the whole line; keep only the part that replaces the selection
        if before.strip() and out.startswith(before):
            out = out[len(before):]
        if after.strip() and out.endswith(after):
            out = out[:-len(after)]
        out = out.strip()
        # Guard: rambling, or the instruction copied into the result instead of carried out
        if not out or len(out) > len(selected) * 4 + 40 or instruction.strip("。！？") in out:
            return selected
        return out

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._log.close()
