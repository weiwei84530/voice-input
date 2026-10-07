"""Settings persisted as config.json in the per-user data folder (see paths.py)."""
import json
from dataclasses import asdict, dataclass

from .paths import CONFIG_PATH


@dataclass
class Config:
    mic: str = ""                  # input device name, "" = system default
    hotkey: str = "caps_lock"      # key in hotkey.HOTKEYS
    autostart: bool = False
    fix_menu: bool = True          # a replacement that sounds alike opens the candidate menu (else the ✓ box)
    menu_seconds: int = 10         # menus and the ✓ box close after this many seconds
    loose_homophones: bool = True  # menus also list other tones and fuzzy readings (else exact readings only)
    mute_while_recording: bool = False  # mute the speakers while recording, until the text is pasted
    newline_command: bool = True   # an utterance of just 換行 presses Shift+Enter
    send_command: bool = True      # an utterance of just 送出 presses Enter
    period_to_comma: bool = True   # speaking on a selected 。 turns it into ， + the utterance + 。

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
