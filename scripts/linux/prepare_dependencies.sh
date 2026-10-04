#!/usr/bin/env bash
# Invoked as the service account (ubuntu); never copies another release's venv.
set -euo pipefail
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -e '.[speech,feishu]'
.venv/bin/python -m pip check
.venv/bin/python -m playwright install --no-shell chromium
.venv/bin/python - <<'PY'
import os
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    # Launch the way the OOPZ recording SDK does (Playwright defaults, the installed
    # Chromium channel after --no-shell); the project starts no other browser.
    browser = p.chromium.launch(headless=True, channel="chromium")
    browser.close()
import oopz_capture.feishu_cli
import funasr, torch, torchaudio, onnxruntime
assert not torch.cuda.is_available(), 'Expected CPU runtime'
import PIL
PY
.venv/bin/python scripts/download_sensevoice_model.py --target models/SenseVoiceSmall
.venv/bin/python -m pip freeze > DEPLOYED_PYTHON_PACKAGES.txt
