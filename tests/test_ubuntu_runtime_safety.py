from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

import pytest

from oopz_capture import process_utils
from oopz_capture.controller import ControllerService
from oopz_capture.workflow import _resolved_direct_child, _run_transcription_process
from test_controller import controller_config


@pytest.mark.parametrize('code,expected', [(errno.EPERM, True), (errno.EACCES, True),
                                          (errno.EIO, True), (errno.ESRCH, False)])
@pytest.mark.skipif(os.name == 'nt', reason='POSIX probe')
def test_only_missing_process_proves_stale(monkeypatch, code, expected):
    def probe(pid, sig):
        assert sig == 0
        raise OSError(code, 'test')
    monkeypatch.setattr(process_utils.os, 'kill', probe)
    assert process_utils.pid_is_running(123) is expected


def test_shared_root_links_work_but_session_links_do_not(tmp_path):
    shared = tmp_path / '共享 output'
    session = shared / 'session'
    session.mkdir(parents=True)
    alias = tmp_path / 'release output'
    try:
        alias.symlink_to(shared, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    root, resolved = _resolved_direct_child(alias, alias / 'session')
    assert root == shared and resolved == session
    session_alias = alias / 'session-alias'
    session_alias.symlink_to(session, target_is_directory=True)
    with pytest.raises(ValueError, match='linked session'):
        _resolved_direct_child(alias, session_alias)


@pytest.mark.parametrize('phase', ['recording', 'transcribing', 'api'])
def test_controller_shutdown_drains_owned_work(tmp_path, phase):
    service = ControllerService(controller_config(tmp_path))
    session_id = '2026-10-01_12-00-00_BJT'
    session = service.output_root / session_id
    session.mkdir()
    (session / 'lifecycle.json').write_text(json.dumps({
        'managed_by': 'oopz-worker-v1', 'mode': 'continuous', 'status': phase,
    }))
    service._state['active'] = {'session_id': session_id, 'status': phase}
    finished = []

    async def run():
        finish_gate = asyncio.Event()
        if phase == 'api':
            started = threading.Event()
            release = threading.Event()
            def api():
                started.set()
                assert release.wait(3)
                return 'cached-result'
            async def analysis():
                finished.append(await asyncio.to_thread(api))
            task = asyncio.create_task(analysis())
            service._background_tasks.add(task)
            while not started.is_set():
                await asyncio.sleep(.001)
        else:
            async def capture():
                if phase == 'recording':
                    while not (session / 'control/stop.json').exists():
                        await asyncio.sleep(.001)
                await finish_gate.wait()  # controlled final transcription flush
                (session / 'transcript.jsonl').write_text('completed chunk\n')
                finished.append('transcribed')
            task = asyncio.create_task(capture())
            service._active_task = task
        shutdown = asyncio.create_task(service.shutdown())
        while not service._stopping:
            await asyncio.sleep(0)
        assert service._stopping
        assert not shutdown.done()
        assert not task.cancelled()
        assert not service._start_analysis_and_deliver(session)
        if phase == 'api':
            release.set()
        else:
            finish_gate.set()
        await asyncio.wait_for(shutdown, 3)
        assert task.done() and not task.cancelled()
    asyncio.run(run())
    assert len(finished) == 1
    if phase == 'recording':
        assert json.loads((session / 'control/stop.json').read_text())['reason'] == 'service_shutdown'


@pytest.mark.skipif(os.name == 'nt', reason='real Unix SIGTERM')
def test_real_sigterm_finishes_inflight_send_then_disconnects():
    # Isolated child: never signal the pytest process or any external process.
    script = r'''
import asyncio, os, signal
from oopz_capture.feishu_cli import serve_gateway
seen = []
stopped = asyncio.Event()
class Controller:
    def request_shutdown(self):
        seen.append('requested')
        stopped.set()
    async def shutdown(self): seen.append('flushed')
class Channel:
    async def connect_until_ready(self): pass
    async def disconnect(self): seen.append('disconnected')
class Gateway:
    controller = Controller()
    async def drain_outbox(self):
        seen.append('send')
        os.kill(os.getpid(), signal.SIGTERM)
        await stopped.wait()
        seen.append('acknowledged')
    async def reconcile_publications(self): raise AssertionError('shutdown reconciliation')
    async def cleanup_expired_sessions(self): raise AssertionError('shutdown cleanup')
asyncio.run(serve_gateway(Channel(), Gateway(), lifecycle=None))
assert seen == ['send', 'requested', 'acknowledged', 'flushed', 'disconnected'], seen
print('SHUTDOWN_OK')
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    assert result.returncode == 0, result.stderr
    assert 'SHUTDOWN_OK' in result.stdout


def test_cancel_transcription_reaps_only_owned_process(tmp_path, monkeypatch):
    from oopz_capture.workflow import WorkflowRequest
    from oopz_capture.vad import VADConfig
    created = []
    original = asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        child = await original(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        created.append(child)
        return child
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    async def run():
        task = asyncio.create_task(_run_transcription_process(
            tmp_path, WorkflowRequest('request', 'area', 'channel', 5, True), VADConfig(), 'cpu', 60,
        ))
        while not created:
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert created[0].returncode is not None
    asyncio.run(run())


@pytest.mark.skipif(os.name == 'nt', reason='real Unix SIGTERM')
def test_real_sigterm_while_gateway_connects():
    script = r'''
import asyncio, os, signal
from oopz_capture.feishu_cli import serve_gateway
seen = []
class Controller:
    def request_shutdown(self): seen.append('requested')
    async def shutdown(self): seen.append('flushed')
class Channel:
    async def connect_until_ready(self):
        os.kill(os.getpid(), signal.SIGTERM)
        try: await asyncio.sleep(60)
        finally: seen.append('connect_cancelled')
    async def disconnect(self): seen.append('disconnected')
class Gateway:
    controller = Controller()
asyncio.run(serve_gateway(Channel(), Gateway(), lifecycle=None))
assert seen == ['requested', 'connect_cancelled', 'flushed', 'disconnected'], seen
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    assert result.returncode == 0, result.stderr
