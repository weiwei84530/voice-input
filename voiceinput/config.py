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
    traditional: bool = True       # convert output to Traditional Chinese (Taiwan glyphs)
    strip_trailing_punct: bool = True  # drop sentence-final 。，,. from the result

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
