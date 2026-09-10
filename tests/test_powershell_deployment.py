import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell deployment')
def test_anonymous_staging_and_config_preservation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    release_id = 'v0.0.0-test'
    commit = '1' * 40
    package = fixture / 'test.zip'
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('RELEASE_MANIFEST.json', json.dumps({'git_commit': commit, 'release_id': release_id}))
        archive.writestr('scripts/install_release.ps1', '# test script')
        archive.writestr('scripts/rollback_release.ps1', '# test script')
        archive.writestr('.env.example', '# empty template')
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    package.with_suffix('.zip.sha256').write_text(digest + '  test.zip', encoding='utf-8')
    (fixture / 'definition.json').write_text(json.dumps({'Tag': 'test', 'File': 'test.zip', 'Sha256': digest, 'Commit': commit, 'ReleaseId': release_id}), encoding='utf-8')
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         str(root / 'tests/powershell_deployment_checks.ps1'), '-RepositoryRoot', str(root),
         '-TestRoot', str(tmp_path), '-FixtureDirectory', str(fixture)],
        capture_output=True, text=True, errors='replace', timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PowerShell deployment checks passed' in result.stdout


@pytest.mark.parametrize('marker,script', [('prepare-release', 'prepare_release.ps1'), ('server-env', 'configure_server_env.ps1'), ('dependency-recovery', 'retry_dependency_install.ps1')])
def test_documented_powershell_sources_match(marker, script):
    root = Path(__file__).resolve().parents[1]
    doc = (root / 'README_CLOUD_SERVER_DEPLOYMENT.md').read_text(encoding='utf-8')
    block = doc.split(f'<!-- {marker}-copy:start -->', 1)[1].split(f'<!-- {marker}-copy:end -->', 1)[0]
    source = (root / 'scripts' / script).read_text(encoding='utf-8').strip()
    assert ('& {\n' + source + '\n}') in block
