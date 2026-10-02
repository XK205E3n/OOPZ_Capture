#!/usr/bin/env bash
# Invoked as the dedicated service account; never copies another release's venv.
set -euo pipefail
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -e '.[speech,feishu,pdf]'
.venv/bin/python -m pip check
node -e 'const [a,b]=process.versions.node.split(".").map(Number); if(a<22||(a===22&&b<12)) throw Error("Node >=22.12 required")'
# npm exec uses the same PATH-selected Node as npx and the service.
PUPPETEER_SKIP_DOWNLOAD=true npm exec --yes --package=pnpm@10.11.0 -- pnpm install --frozen-lockfile
.venv/bin/python -m playwright install --no-shell chromium
.venv/bin/python - <<'PY'
import os
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    # Verify the PDF browser's real sandbox prerequisite, independently of the
    # SDK driver's default sandbox setting. Never fall back to --no-sandbox.
    launch = dict(headless=True, chromium_sandbox=True)
    browser_path = os.environ.get("MD_TO_PDF_CHROME_PATH")
    if browser_path:
        launch["executable_path"] = browser_path
    else:
        # Match the installed Chromium channel after --no-shell.
        launch["channel"] = "chromium"
    browser = p.chromium.launch(**launch)
    browser.close()
import oopz_capture.feishu_cli
import funasr, torch, torchaudio, onnxruntime
assert not torch.cuda.is_available(), 'Expected CPU runtime'
from oopz_capture.weasy_pdf import require_weasyprint
require_weasyprint()
import weasyprint
PY
.venv/bin/python scripts/download_sensevoice_model.py --target models/SenseVoiceSmall
.venv/bin/python -m pip freeze > DEPLOYED_PYTHON_PACKAGES.txt
node --version > DEPLOYED_NODE_VERSION.txt
