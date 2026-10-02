"""Run the actual formal release builder on a clean isolated Git fixture."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('pwsh') or shutil.which('powershell.exe')


def run(command, cwd):
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=90)


@pytest.fixture
def clean_source(tmp_path):
    source = tmp_path / 'source with space'
    source.mkdir()
    for relative in (
        'scripts/build_release.ps1', 'scripts/install_release.ps1',
        'scripts/download_sensevoice_model.py', '.env.example',
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, path)
    shutil.copytree(ROOT / 'scripts/linux', source / 'scripts/linux',
                    ignore=shutil.ignore_patterns('__pycache__'))
    (source / 'pyproject.toml').write_text('[project]\nversion = "0.0.1"\n')
    (source / '.gitignore').write_text('.venv/\nartifacts/\n')
    (source / 'tests').mkdir()
    (source / 'tests/test_fixture.py').write_text('def test_fixture(): assert True\n')
    (source / '中文说明.txt').write_text('正式发布构建验证', encoding='utf-8')
    for command in (
        ['git', 'init', '-q'], ['git', 'add', '.'],
        ['git', '-c', 'user.name=Release Test', '-c', 'user.email=release-test@example.invalid',
         'commit', '-qm', 'Isolated release fixture'],
    ):
        result = run(command, source)
        assert result.returncode == 0, result.stderr
    return source


@pytest.mark.skipif(not POWERSHELL, reason='PowerShell release builder is not installed')
def test_formal_builder_preserves_hidden_files_and_portable_entries(clean_source):
    result = run([POWERSHELL, '-NoProfile', '-File', 'scripts/build_release.ps1', '-SkipTests'], clean_source)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    artifact = Path(summary['artifact'])
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == summary['sha256']
    with zipfile.ZipFile(artifact) as archive:
        assert '.env.example' in archive.namelist()
        assert '.gitignore' in archive.namelist()
        assert '中文说明.txt' in archive.namelist()
        assert not any('\\' in name for name in archive.namelist())
        manifest = json.loads(archive.read('RELEASE_MANIFEST.json').decode('utf-8-sig'))
        assert manifest['git_commit'] == run(['git', 'rev-parse', 'HEAD'], clean_source).stdout.strip()
    again = run([POWERSHELL, '-NoProfile', '-File', 'scripts/build_release.ps1', '-SkipTests'], clean_source)
    assert again.returncode != 0
    assert 'Release output already exists' in again.stderr


@pytest.mark.skipif(not POWERSHELL or os.name == 'nt', reason='Unix PowerShell/venv integration')
def test_formal_builder_runs_unix_venv_tests(clean_source):
    (clean_source / '.venv/bin').mkdir(parents=True)
    python = clean_source / '.venv/bin/python'
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    result = run([POWERSHELL, '-NoProfile', '-File', 'scripts/build_release.ps1'], clean_source)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 passed' in result.stdout
    assert len(list((clean_source / 'artifacts').glob('*.zip'))) == 1


@pytest.mark.skipif(not POWERSHELL, reason='PowerShell release builder is not installed')
def test_formal_builder_refuses_dirty_source(clean_source):
    (clean_source / '.env.example').write_text('changed\n')
    result = run([POWERSHELL, '-NoProfile', '-File', 'scripts/build_release.ps1', '-SkipTests'], clean_source)
    assert result.returncode != 0
    assert 'Refusing to build' in result.stderr
    assert not (clean_source / 'artifacts').exists()
