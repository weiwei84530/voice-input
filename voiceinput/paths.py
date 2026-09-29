"""Where things live. The app folder holds only code and runtime tools; user data (settings, models, logs)
lives in a per-user data folder so it survives moving, reinstalling or updating the app."""
import os
import shutil
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent

if sys.platform == "win32":
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "VoiceInput"
elif sys.platform == "darwin":
    DATA_DIR = Path.home() / "Library" / "Application Support" / "VoiceInput"
else:
    DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "VoiceInput"

CONFIG_PATH = DATA_DIR / "config.json"
HOTWORDS_PATH = DATA_DIR / "hotwords.json"
LOG_PATH = DATA_DIR / "voiceinput.log"
LOCK_PATH = DATA_DIR / ".voiceinput.lock"
DEFAULT_MODELS_DIR = DATA_DIR / "models"


def migrate_legacy() -> None:
    """Move data from the old layout (everything inside the app folder) into DATA_DIR, once."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for src, dst in ((APP_DIR / "config.json", CONFIG_PATH), (APP_DIR / "voiceinput.log", LOG_PATH),
                     (APP_DIR / "models", DEFAULT_MODELS_DIR)):
        if src.exists() and not dst.exists():
            shutil.move(src, dst)
    for stale in ("llama-server.log", ".voiceinput.lock"):
        (APP_DIR / stale).unlink(missing_ok=True)
