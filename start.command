#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "Not installed yet. Run install.command first."
    exit 1
fi
nohup .venv/bin/python run.pyw >/dev/null 2>&1 &
