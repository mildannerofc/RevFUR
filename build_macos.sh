#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required")
PY
python3 -m pip install --upgrade pip pyinstaller tkinterdnd2
python3 -m PyInstaller --noconfirm --clean --onedir --windowed --name RevFUR --collect-all tkinterdnd2 main.py
echo "Build complete: dist/RevFUR.app or dist/RevFUR/ (depending on PyInstaller/platform output)."
