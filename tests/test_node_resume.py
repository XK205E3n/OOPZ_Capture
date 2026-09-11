import os
from pathlib import Path
import subprocess
import zipfile

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows deployment recovery')
def test_node_resume_preserves_completed_stages_and_checks_readiness(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture = tmp_path / 'fixture.zip'
    with zipfile.ZipFile(fixture, 'w') as archive:
        archive.writestr('package.json', '{}')
        archive.writestr('pnpm-lock.yaml', 'lockfileVersion: 9.0')
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         str(root / 'tests/node_resume_checks.ps1'), '-RepositoryRoot', str(root),
         '-TestRoot', str(tmp_path), '-FixtureZip', str(fixture)],
        capture_output=True, text=True, errors='replace', timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'node resume checks passed' in result.stdout
