"""Launch at login: HKCU Run key on Windows, LaunchAgent on macOS."""
import sys
from pathlib import Path

from .models import ROOT

APP_ID = "VoiceInput"
LAUNCHER = ROOT / "run.pyw"


def _python() -> str:
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pyw = exe.with_name("pythonw.exe")
        return str(pyw if pyw.exists() else exe)
    return str(exe)


def set_enabled(enabled: bool) -> None:
    if sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, f'"{_python()}" "{LAUNCHER}"')
            else:
                try:
                    winreg.DeleteValue(k, APP_ID)
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents/com.voiceinput.app.plist"
        if enabled:
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.voiceinput.app</string>
  <key>ProgramArguments</key><array><string>{_python()}</string><string>{LAUNCHER}</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
</dict></plist>
""")
        else:
            plist.unlink(missing_ok=True)
