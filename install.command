#!/bin/bash
# macOS installer. If double-click does nothing, run: bash install.command
set -e
cd "$(dirname "$0")"

TOOLS="$PWD/.tools"
UV="$TOOLS/uv"
export UV_PYTHON_INSTALL_DIR="$TOOLS/python"
export UV_PYTHON_PREFERENCE=only-managed

if [ ! -x "$UV" ]; then
    echo "[1/4] Installing uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$TOOLS" UV_NO_MODIFY_PATH=1 sh
else
    echo "[1/4] uv already installed"
fi

echo "[2/4] Creating Python environment ..."
[ -x .venv/bin/python ] || "$UV" venv --python 3.12 .venv

echo "[3/4] Installing packages ..."
"$UV" pip install --python .venv/bin/python -r requirements.txt

echo "[4/4] Downloading default speech model ..."
.venv/bin/python -m voiceinput.models

chmod +x start.command 2>/dev/null || true
echo
echo "Done. Run start.command to launch VoiceInput."
echo "On first launch, allow Microphone, Accessibility and Input Monitoring in System Settings > Privacy & Security."
