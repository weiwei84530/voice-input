import sys

if sys.platform != "win32":
    sys.exit("VoiceInput runs on Windows only (see docs/macos-port.md).")

from voiceinput.app import main

sys.exit(main())
