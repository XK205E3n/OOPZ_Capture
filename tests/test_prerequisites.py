from pathlib import Path
import os
import subprocess

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell bootstrap')
def test_prerequisites_skip_install_and_fail_closed(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         str(root / 'tests/prerequisites_checks.ps1'), '-RepositoryRoot', str(root),
         '-TestRoot', str(tmp_path / 'installers')],
        capture_output=True, text=True, errors='replace', timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'prerequisite branch checks passed' in result.stdout


def test_documented_bootstrap_matches_script():
    root = Path(__file__).resolve().parents[1]
    doc = (root / 'README_CLOUD_SERVER_DEPLOYMENT.md').read_text(encoding='utf-8')
    start = '<!-- prerequisites-copy:start -->'
    end = '<!-- prerequisites-copy:end -->'
    block = doc.split(start, 1)[1].split(end, 1)[0].strip()
    source = (root / 'scripts/install_prerequisites.ps1').read_text(encoding='utf-8').strip()
    assert block == '```powershell\n& {\n' + source + '\n}\n```'
