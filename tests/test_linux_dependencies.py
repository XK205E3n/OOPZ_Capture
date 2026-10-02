"""Execute the real Linux dependency entrypoint with isolated external tools."""
from __future__ import annotations

import json
import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == 'nt', reason='Linux dependency shell entrypoint')
def test_preparation_launches_installed_chromium_channel(tmp_path):
    bins = tmp_path / 'bin'
    bins.mkdir()
    modules = tmp_path / 'modules'
    modules.mkdir()
    for package in ('playwright', 'oopz_capture'):
        (modules / package).mkdir()
        (modules / package / '__init__.py').write_text('')
    (modules / 'oopz_capture/feishu_cli.py').write_text('')
    for name in ('funasr', 'torchaudio', 'onnxruntime'):
        (modules / f'{name}.py').write_text('')
    (modules / 'torch.py').write_text('class cuda:\n @staticmethod\n def is_available(): return False\n')
    (modules / 'playwright/sync_api.py').write_text('''
import json, os
from pathlib import Path
class Browser:
    def close(self): Path('browser-closed').write_text('yes')
class Playwright:
    chromium = None
    def __init__(self): self.chromium = self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def launch(self, **kwargs):
        assert Path('chromium-installed').is_file(), 'Chromium was not installed'
        assert kwargs == {'headless': True, 'channel': 'chromium'}, 'No headless shell was installed'
        Path('browser-launch.json').write_text(json.dumps(kwargs))
        return Browser()
def sync_playwright(): return Playwright()
''')
    wrapper = f'''#!{sys.executable}
import os, sys
from pathlib import Path
args = sys.argv[1:]
if args == ['-']:
    os.execv(sys.executable, [sys.executable, '-'])
elif args[:2] == ['-m', 'playwright']:
    assert args == ['-m', 'playwright', 'install', '--no-shell', 'chromium']
    Path('chromium-installed').write_text('yes')
elif args[:2] == ['-m', 'pip']:
    pass
elif args[:1] == ['scripts/download_sensevoice_model.py']:
    Path('model-step-completed').write_text('yes')
else:
    raise RuntimeError('Unexpected external command: ' + repr(args))
'''
    bootstrap = bins / 'python3.12'
    bootstrap.write_text(f'''#!{sys.executable}
import sys
from pathlib import Path
assert sys.argv[1:] == ['-m', 'venv', '.venv']
p=Path('.venv/bin/python'); p.parent.mkdir(parents=True)
p.write_text({wrapper!r}); p.chmod(0o755)
''')
    bootstrap.chmod(0o755)
    for name in ('node', 'npm'):
        path = bins / name
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
    env = {**os.environ, 'PATH': str(bins) + os.pathsep + os.environ['PATH'],
           'PYTHONPATH': str(modules)}
    result = subprocess.run(['bash', str(ROOT / 'scripts/linux/prepare_dependencies.sh')],
                            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / 'browser-launch.json').read_text()) == {
        'headless': True, 'channel': 'chromium',
    }
    assert (tmp_path / 'browser-closed').is_file()
    assert (tmp_path / 'model-step-completed').is_file()


@pytest.mark.skipif(os.name == 'nt', reason='Linux service-user preparation')
def test_privileged_preparation_keeps_project_home_after_runuser(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/linux'))
    manager = importlib.import_module('manage_release')
    bins = tmp_path / 'bin'
    bins.mkdir()
    runuser = bins / 'runuser'
    runuser.write_text(f'''#!{sys.executable}
import os, sys
assert sys.argv[1:4] == ['-u', 'existing-user', '--']
os.environ['HOME'] = '/home/existing-user'
os.execvp(sys.argv[4], sys.argv[4:])
''')
    runuser.chmod(0o755)
    monkeypatch.setenv('PATH', str(bins) + os.pathsep + os.environ['PATH'])
    monkeypatch.setattr(manager.os, 'geteuid', lambda: 0)
    release = tmp_path / 'release'
    release.mkdir()
    manager.run_as(SimpleNamespace(pw_name='existing-user'), [sys.executable, '-c',
        "from pathlib import Path; import os; Path('home.txt').write_text(os.environ['HOME'])"],
        release, tmp_path)
    assert (release / 'home.txt').read_text() == str(tmp_path / 'shared/home')
