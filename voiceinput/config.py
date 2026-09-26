"""Settings persisted as config.json in the per-user data folder (see paths.py)."""
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import DEFAULT_MODEL
from .paths import CONFIG_PATH, DEFAULT_MODELS_DIR


@dataclass
class Config:
    model: str = DEFAULT_MODEL     # key in models.MODELS
    mic: str = ""                  # input device name, "" = system default
    hotkey: str = "caps_lock"      # key in hotkey.HOTKEYS
    autostart: bool = False
    strip_trailing_punct: bool = True  # drop sentence-final 。，,. from the result
    llm_enabled: bool = False      # run the local LLM with llm_user_rules (model only loaded when on)
    llm_user_rules: str = ""       # custom rules the LLM applies; empty = LLM is skipped
    edit_enabled: bool = True      # speaking with text selected edits the selection (刪除, spelling, replace)
    models_dir: str = ""           # where ASR / LLM models are stored, "" = paths.DEFAULT_MODELS_DIR

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def models_path(self) -> Path:
        return Path(self.models_dir) if self.models_dir else DEFAULT_MODELS_DIR

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
