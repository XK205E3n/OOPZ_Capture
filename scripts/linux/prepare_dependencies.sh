#!/usr/bin/env bash
# Invoked as the dedicated service account; never copies another release's venv.
set -euo pipefail
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -e '.[speech,feishu]'
.venv/bin/python -m pip check
node -e 'const [a,b]=process.versions.node.split(".").map(Number); if(a<22||(a===22&&b<12)) throw Error("Node >=22.12 required")'
# npm exec uses the same PATH-selected Node as npx and the service.
PUPPETEER_SKIP_DOWNLOAD=true npm exec --yes --package=pnpm@10.11.0 -- pnpm install --frozen-lockfile
.venv/bin/python -m playwright install --no-shell chromium
.venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    browser.close()
import oopz_capture.feishu_cli
import funasr, torch, torchaudio, onnxruntime
assert not torch.cuda.is_available(), 'Expected CPU runtime'
PY
.venv/bin/python scripts/download_sensevoice_model.py --target models/SenseVoiceSmall
.venv/bin/python -m pip freeze > DEPLOYED_PYTHON_PACKAGES.txt
node --version > DEPLOYED_NODE_VERSION.txt
