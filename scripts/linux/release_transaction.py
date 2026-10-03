"""Journaled service switch, restoring actual bytes/links and service state."""
from __future__ import annotations
import base64
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path

SERVICE = 'oopz-capture.service'


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(['systemctl', *args], check=check, text=True, capture_output=True)


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def snapshot(path: Path) -> dict:
    if not os.path.lexists(path):
        return {'kind': 'absent'}
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        result = {'kind': 'link', 'target': os.readlink(path)}
    elif stat.S_ISREG(info.st_mode):
        result = {'kind': 'file', 'data': base64.b64encode(path.read_bytes()).decode(), 'mode': stat.S_IMODE(info.st_mode)}
    else:
        raise RuntimeError(f'Refusing to replace non-file: {path}')
    result.update(uid=info.st_uid, gid=info.st_gid)
    return result


def replace_link(path: Path, target: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.current-', dir=path.parent)
    os.close(fd)
    os.unlink(temporary)
    try:
        os.symlink(target, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def restore(path: Path, item: dict) -> None:
    if item['kind'] == 'absent':
        path.unlink(missing_ok=True)
        return
    if item['kind'] == 'link':
        replace_link(path, item['target'])
    else:
        atomic_write(path, base64.b64decode(item['data']), item['mode'])
    if os.geteuid() == 0:
        os.chown(path, item['uid'], item['gid'], follow_symlinks=False)


def load_state() -> str:
    result = systemctl('show', SERVICE, '--property=LoadState', '--value', check=False)
    value = result.stdout.strip()
    if value not in ('loaded', 'not-found'):
        raise RuntimeError('Cannot establish service unit load state')
    return value


def state() -> dict:
    loaded = load_state()
    active = systemctl('is-active', SERVICE, check=False).stdout.strip()
    enabled = 'not-found' if loaded == 'not-found' else systemctl('is-enabled', SERVICE, check=False).stdout.strip()
    if active not in ('active', 'inactive', 'failed', 'unknown') or enabled not in ('enabled', 'disabled', 'static', 'not-found'):
        raise RuntimeError('Service is changing state, masked, or uninspectable; refusing switch')
    return {'active': active == 'active', 'enabled': enabled}


def recover(root: Path, unit: Path, rotation: Path) -> None:
    journal = root / '.switch-journal.json'
    info = journal.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError('Unsafe recovery journal ownership or permissions')
    data = json.loads(journal.read_text())
    if set(data.get('files', {})) != {str(root / 'current'), str(unit), str(rotation)}:
        raise RuntimeError('Recovery journal paths do not match this installation')
    if data.get('state', {}).get('enabled') not in ('enabled', 'disabled', 'static', 'not-found') or type(data.get('state', {}).get('active')) is not bool:
        raise RuntimeError('Invalid recovery service state')
    try:
        present = load_state() != 'not-found'
        if present:
            systemctl('stop', SERVICE)
            if data['state']['enabled'] != 'enabled':
                systemctl('disable', SERVICE)
        # All files must be restored before an old service can ever start.
        for path, item in data['files'].items():
            restore(Path(path), item)
        systemctl('daemon-reload')
        if data['state']['enabled'] == 'enabled':
            systemctl('enable', SERVICE)
        if data['state']['active']:
            systemctl('start', SERVICE)
            if systemctl('is-active', SERVICE, check=False).stdout.strip() != 'active':
                raise RuntimeError('Old service did not become active')
        restored = state()
        if restored != data['state']:
            raise RuntimeError('Service state restoration could not be verified')
        journal.unlink()
    except BaseException as error:
        systemctl('stop', SERVICE, check=False)
        raise RuntimeError(f'RECOVERY FAILED; service left stopped; preserve {journal} and repair before retry') from error


def switch(root: Path, release: Path, unit: Path, rotation: Path,
           unit_data: bytes, rotation_data: bytes, timeout: float, enable: bool) -> None:
    journal = root / '.switch-journal.json'
    if journal.exists():
        raise RuntimeError('Unfinished transaction exists; run recover first')
    data = {'state': state(), 'files': {str(p): snapshot(p) for p in (root / 'current', unit, rotation)}}
    atomic_write(journal, json.dumps(data).encode(), 0o600)
    previous_handlers = {}
    def interrupted(signum, _frame):
        raise RuntimeError(f'Switch interrupted by signal {signum}')
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.signal(sig, interrupted)
    try:
        if data['state']['enabled'] != 'not-found':
            systemctl('stop', SERVICE)
        atomic_write(unit, unit_data)
        atomic_write(rotation, rotation_data)
        replace_link(root / 'current', str(release))
        systemctl('daemon-reload')
        if enable or data['state']['enabled'] == 'enabled':
            systemctl('enable', SERVICE)
        log = root / 'shared/logs/feishu_runtime.log'
        original = log.stat() if log.exists() else None
        offset = original.st_size if original else 0
        systemctl('start', SERVICE)
        deadline = time.monotonic() + timeout
        while True:
            if systemctl('is-active', SERVICE, check=False).stdout.strip() != 'active':
                raise RuntimeError('New service did not remain active')
            if log.exists():
                with log.open('rb') as stream:
                    current = os.fstat(stream.fileno())
                    if original is None or current.st_ino != original.st_ino or current.st_size < offset:
                        offset = 0
                    stream.seek(offset)
                    if '飞书长连接已就绪'.encode() in stream.read():
                        break
            if time.monotonic() >= deadline:
                raise RuntimeError('Timed out waiting for new gateway readiness')
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        journal.unlink()
    except BaseException:
        # Ignore further termination during restoration; a SIGKILL leaves a journal.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        recover(root, unit, rotation)
        raise
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
