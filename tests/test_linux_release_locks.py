"""Task-state guard regressions runnable on both Windows and Linux."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from contextlib import nullcontext
import os

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts/linux'
spec = importlib.util.spec_from_file_location('guard_under_test', SCRIPTS / 'release_locks.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

BUSY = ['analyzing', 'analyzing_short_windows', 'analyzing_long_windows',
        'building_final_report', 'preparing_windows']


def task_tree(tmp_path: Path, *, last=None, status=None) -> Path:
    shared = tmp_path / 'shared'
    (shared / 'output').mkdir(parents=True)
    (shared / 'feishu_state').mkdir()
    (shared / 'feishu_state/controller.json').write_text(
        json.dumps({'active': None, 'last_job': last}), encoding='utf-8')
    if status:
        lifecycle = shared / 'output/session/analysis_variants/configured-api/lifecycle.json'
        lifecycle.parent.mkdir(parents=True)
        lifecycle.write_text(json.dumps({'status': status}), encoding='utf-8')
    return shared


@pytest.mark.parametrize('status', BUSY)
def test_background_work_without_lock_refuses_switch(tmp_path: Path, status: str) -> None:
    shared = task_tree(tmp_path, last={'status': status})
    assert not list(shared.rglob('*.lock'))
    with pytest.raises(RuntimeError, match='pending analysis/work'):
        guard.check_idle(shared)


@pytest.mark.parametrize('status', BUSY)
def test_analysis_lifecycle_without_lock_refuses_switch(tmp_path: Path, status: str) -> None:
    with pytest.raises(RuntimeError, match='still active'):
        guard.check_idle(task_tree(tmp_path, status=status))


@pytest.mark.parametrize('status', ['completed', 'failed', 'analysis_failed',
                                   'analysis_completed_report_queued', 'ready_for_analysis', 'prepared'])
def test_finished_or_window_plan_only_state_allows_switch(tmp_path: Path, status: str) -> None:
    guard.check_idle(task_tree(tmp_path, last={'status': status}, status=status))


@pytest.mark.parametrize('last', [['analyzing'], {'status': ['analyzing']}])
def test_malformed_last_job_fails_closed(tmp_path: Path, last) -> None:
    with pytest.raises(RuntimeError, match='Cannot establish task state'):
        guard.check_idle(task_tree(tmp_path, last=last))


def test_activation_entrypoint_rejects_registered_analysis_before_any_switch(tmp_path: Path, monkeypatch) -> None:
    """Exercise main's guard gate; Unix account/lock setup is isolated here."""
    # The CLI's Unix account imports are unused before the gate in this test.
    with monkeypatch.context() as imports:
        imports.syspath_prepend(str(SCRIPTS))
        if os.name == 'nt':
            for name in ('fcntl', 'pwd', 'grp'):
                imports.setitem(__import__('sys').modules, name, ModuleType(name))
        cli_spec = importlib.util.spec_from_file_location('manager_under_test', SCRIPTS / 'manage_release.py')
        manager = importlib.util.module_from_spec(cli_spec)
        cli_spec.loader.exec_module(manager)
    shared = task_tree(tmp_path, last={'status': 'analyzing'})
    root = shared.parent
    release = root / 'releases' / ('v0.11.15-' + 'b' * 12)
    release.mkdir(parents=True)
    (release / '.prepared.json').write_text('{}', encoding='utf-8')
    (root / 'current').write_text('original current placeholder', encoding='utf-8')
    monkeypatch.setattr(manager, 'account', lambda _: object())
    monkeypatch.setattr(manager, 'management_lock', lambda _: nullcontext())
    monkeypatch.setattr(manager, 'verify_shared_links', lambda *_: None)
    def forbidden(*_args, **_kwargs):
        pytest.fail('Guard bypassed: external service call or version switch attempted')
    monkeypatch.setattr(manager, 'systemctl', forbidden)
    monkeypatch.setattr(manager, 'switch', forbidden)
    with pytest.raises(RuntimeError, match='pending analysis/work'):
        manager.main(['activate', '--root', str(root), '--release-id', release.name])
    assert (root / 'current').read_text(encoding='utf-8') == 'original current placeholder'


def test_prune_keeps_only_the_new_release_and_its_files(tmp_path: Path) -> None:
    cleanup_spec = importlib.util.spec_from_file_location('cleanup_under_test', SCRIPTS / 'release_cleanup.py')
    cleanup = importlib.util.module_from_spec(cleanup_spec)
    cleanup_spec.loader.exec_module(cleanup)
    root = tmp_path / 'oopz'
    new, old = 'v0.12.1-' + 'a' * 12, 'v0.12.0-' + 'b' * 12
    for release in (new, old, '.preparing-' + 'v0.12.2-' + 'c' * 12, 'notes'):
        (root / 'releases' / release).mkdir(parents=True)
        (root / 'releases' / release / 'file').write_text('x')
    try:
        (root / 'releases' / old / 'logs').symlink_to(tmp_path)
        (root / 'current').symlink_to(root / 'releases' / new)
    except OSError:
        pass                       # no symlink privilege here: the rest of the rules are still checked
    art = root / 'artifacts'
    (art / 'bootstrap-v0.12.0').mkdir(parents=True)
    (art / 'bootstrap-v0.12.1' / 'scripts').mkdir(parents=True)
    for name in (f'oopz-capture-{old}.zip', f'oopz-capture-{old}.zip.sha256', f'oopz-capture-{new}.zip',
                 'prepare-v0.12.0b.log', 'update-v0.12.1.log', 'keep-me.txt'):
        (art / name).write_text('x')
    removed = cleanup.prune_superseded(root, new, running_from=art / 'bootstrap-v0.12.1' / 'scripts')
    assert sorted(p.name for p in (root / 'releases').iterdir()) == ['.preparing-v0.12.2-' + 'c' * 12, 'notes', new]
    assert sorted(p.name for p in art.iterdir()) == ['bootstrap-v0.12.1', 'keep-me.txt', f'oopz-capture-{new}.zip', 'update-v0.12.1.log']
    assert tmp_path.is_dir() and 'releases/' + old in removed       # a symlink inside the old release was not followed
