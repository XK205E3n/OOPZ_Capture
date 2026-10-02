import os
from pathlib import Path
import subprocess
import zipfile

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows first-install recovery')
def test_recovery_gates_backup_and_environment_restoration(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture = tmp_path / 'fixture.zip'
    with zipfile.ZipFile(fixture, 'w') as archive:
        archive.writestr('scripts/install_release.ps1', '# test-only installer')
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         str(root / 'tests/dependency_recovery_checks.ps1'), '-RepositoryRoot', str(root),
         '-TestRoot', str(tmp_path), '-FixtureZip', str(fixture)],
        capture_output=True, text=True, errors='replace', timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'dependency recovery checks passed' in result.stdout
