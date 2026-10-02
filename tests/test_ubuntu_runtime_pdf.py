from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from oopz_capture import env_loader, pdf_reports, settings


def test_shared_config_loading_and_in_place_writes(tmp_path, monkeypatch):
    shared = tmp_path / '共享 config' / '.env'
    shared.parent.mkdir()
    shared.write_text('OOPZ_LOGIN_PASSWORD="中文 # $ = 密码"\nOOPZ_CHUNK_SECONDS=60\n', encoding='utf-8-sig')
    release = tmp_path / 'release.env'
    os.link(shared, release)
    inode = shared.stat().st_ino
    monkeypatch.setenv('OOPZ_ENV_FILE', str(shared))
    monkeypatch.delenv('OOPZ_LOGIN_PASSWORD', raising=False)
    monkeypatch.setenv('OOPZ_CHUNK_SECONDS', '120')
    assert env_loader.load_project_env() == shared
    assert os.environ['OOPZ_LOGIN_PASSWORD'] == '中文 # $ = 密码'
    assert os.environ['OOPZ_CHUNK_SECONDS'] == '120'
    settings.upsert_env('OOPZ_FEISHU_ADMIN_CHAT_ID', 'oc_fixture')
    assert 'oc_fixture' in release.read_text(encoding='utf-8')
    assert shared.stat().st_ino == inode
    assert env_loader.project_env_path(release) == release


def test_shared_config_symlink_before_activation(tmp_path, monkeypatch):
    shared = tmp_path / 'shared' / '.env'
    shared.parent.mkdir()
    shared.write_text('OOPZ_ENV_FILE=do-not-follow.env\n', encoding='utf-8')
    release = tmp_path / 'prepared' / '.env'
    release.parent.mkdir()
    try:
        release.symlink_to(shared)
    except OSError:
        pytest.skip('symlinks unavailable')
    monkeypatch.setenv('OOPZ_ENV_FILE', str(release))
    env_loader.load_project_env()
    settings.upsert_env('OOPZ_FEISHU_APP_ID', 'cli_fixture')
    assert release.is_symlink()
    assert 'cli_fixture' in shared.read_text()
    assert os.environ['OOPZ_ENV_FILE'] == str(release)


def test_env_relative_path_uses_project_root(tmp_path, monkeypatch):
    monkeypatch.setattr(env_loader, '_PROJECT_ROOT', tmp_path)
    monkeypatch.setenv('OOPZ_ENV_FILE', '共享/.env')
    monkeypatch.chdir(tmp_path.parent)
    assert env_loader.project_env_path() == tmp_path / '共享/.env'


def test_node_override_and_same_runtime_path(tmp_path, monkeypatch):
    node = tmp_path / 'runtime space' / 'bin' / 'node'
    node.parent.mkdir(parents=True)
    node.write_text('fixture')
    monkeypatch.setenv('OOPZ_NODE_PATH', str(node))
    assert pdf_reports.find_node() == node
    assert pdf_reports.node_environment(node)['PATH'].split(os.pathsep)[0] == str(node.parent)
    monkeypatch.setenv('OOPZ_NODE_PATH', str(tmp_path / 'absent'))
    with pytest.raises(FileNotFoundError):
        pdf_reports.find_node()


@pytest.mark.parametrize('version,valid', [('v22.11.0', False), ('v20.20.1', False),
                                          ('v22.12.0', True), ('v24.1.0', True), ('invalid', False)])
def test_node_version_floor(version, valid, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, version, ''))
    if valid:
        pdf_reports.validate_node(Path('node'), {})
    else:
        with pytest.raises(RuntimeError, match='22.12.0'):
            pdf_reports.validate_node(Path('node'), {})


def node_program(program):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    result = subprocess.run([node, '--input-type=module', '-e', program],
                            cwd=pdf_reports.PROJECT_ROOT, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_browser_cache_real_directories_and_fonts(tmp_path):
    import json
    root = tmp_path / '缓存 browsers'
    root.mkdir()
    for layout in ['chromium-1200/chrome-linux64/chrome', 'chromium-1100/chrome-linux/chrome',
                   'chromium_headless_shell-1200/chrome-headless-shell-linux64/chrome-headless-shell']:
        browser = root / layout
        browser.parent.mkdir(parents=True)
        browser.touch()
        node_program(f'''
import {{findChrome, checkChineseFonts}} from './tools/md_to_pdf.mjs';
import assert from 'node:assert/strict';
assert.equal(findChrome({{PLAYWRIGHT_BROWSERS_PATH:{json.dumps(str(root))}}}), {json.dumps(str(browser))});
assert.throws(() => checkChineseFonts('中文', 'linux', () => ''), /fonts missing/);
assert.throws(() => checkChineseFonts('中文', 'linux', () => {{throw Error('no fc-list')}}), /font check failed/);
checkChineseFonts('中文', 'linux', () => 'Noto Sans CJK SC');
checkChineseFonts('English', 'linux', () => {{throw Error('should not run')}});
checkChineseFonts('中文', 'win32', () => {{throw Error('should not run')}});
''')
        browser.unlink()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process groups')
def test_renderer_timeout_kills_owned_child(tmp_path):
    child_pid = tmp_path / 'child.pid'
    script = tmp_path / 'renderer.py'
    script.write_text('import subprocess,sys,time\n'
                      'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
                      f'open({str(child_pid)!r},"w").write(str(p.pid))\n'
                      'time.sleep(60)\n')
    with pytest.raises(subprocess.TimeoutExpired):
        pdf_reports._run_renderer([sys.executable, str(script)], os.environ.copy(), timeout=1)
    pid = int(child_pid.read_text())
    # Orphaned killed children may remain zombies until the container init reaps.
    for _ in range(100):
        stat = Path(f'/proc/{pid}/stat')
        if not stat.exists() or stat.read_text().split()[2] == 'Z':
            break
        time.sleep(.01)
    else:
        pytest.fail('renderer descendant survived timeout')


def test_renderer_error_removes_partial_pdf(tmp_path, monkeypatch):
    source = tmp_path / 'test.md'
    source.write_text('test')
    output = tmp_path / 'test.pdf'
    monkeypatch.setattr(pdf_reports, 'find_node', lambda: Path(sys.executable))
    monkeypatch.setattr(pdf_reports, 'validate_node', lambda *args: None)
    monkeypatch.setattr(pdf_reports, 'NODE_MODULES', tmp_path)
    def fail(*args):
        output.write_bytes(b'partial')
        raise subprocess.CalledProcessError(1, [], stderr='font error')
    monkeypatch.setattr(pdf_reports, '_run_renderer', fail)
    with pytest.raises(RuntimeError, match='font error'):
        pdf_reports.render_markdown_pdf(source, output)
    assert not output.exists()
