"""Launch at login: HKCU Run key."""
import sys
import winreg
from pathlib import Path

from .paths import APP_DIR

APP_ID = "VoiceInput"
LAUNCHER = APP_DIR / "run.pyw"


def _python() -> str:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return str(pyw if pyw.exists() else exe)


def set_enabled(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                        0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, f'"{_python()}" "{LAUNCHER}"')
        else:
            try:
                winreg.DeleteValue(k, APP_ID)
            except FileNotFoundError:
                pass
