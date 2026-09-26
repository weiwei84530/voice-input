"""Settings persisted as config.json in the project folder (portable)."""
import json
from dataclasses import asdict, dataclass

from .models import ROOT

CONFIG_PATH = ROOT / "config.json"


@dataclass
class Config:
    mic: str = ""                  # input device name, "" = system default
    hotkey: str = "caps_lock"      # key in hotkey.HOTKEYS
    autostart: bool = False
    strip_trailing_punct: bool = True  # drop sentence-final 。，,. from the result
    llm_enabled: bool = False      # run the local LLM with llm_user_rules (model only loaded when on)
    llm_user_rules: str = ""       # custom rules the LLM applies; empty = LLM is skipped

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
