"""Conservative pre-stop guard; unknown job state is never considered idle."""
from __future__ import annotations
import json
import os
import stat
from pathlib import Path


def check_idle(shared: Path) -> None:
    for directory in ('output', 'feishu_state'):
        root = shared / directory
        if root.is_symlink():
            raise RuntimeError('Shared task root must not be a symlink')
        if not root.exists():
            continue
        if not root.is_dir():
            raise RuntimeError('Shared task root must be a directory')
        def unreadable(error):
            raise RuntimeError('Cannot inspect task tree; refusing switch') from error
        for folder, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
            for name in dirs + files:
                path = Path(folder) / name
                if path.is_symlink():
                    raise RuntimeError('Cannot establish idle state through a symlink')
                if name in ('controller.json', 'lifecycle.json'):
                    if not stat.S_ISREG(path.lstat().st_mode):
                        raise RuntimeError('Non-regular task state; refusing switch')
                    try:
                        value = json.loads(path.read_text(encoding='utf-8'))
                        if not isinstance(value, dict):
                            raise ValueError('not an object')
                        if name == 'controller.json' and value.get('active') is not None:
                            raise RuntimeError('Controller has an active job; refusing switch')
                        if value.get('status') in {'starting', 'connecting', 'recording', 'reconnecting', 'stopping', 'transcribing', 'analyzing', 'running'}:
                            raise RuntimeError('Session is still active; refusing switch')
                    except (ValueError, OSError, UnicodeError) as error:
                        raise RuntimeError('Cannot establish task state; refusing switch') from error
                if not (name.endswith('.lock') or name.endswith('.pid')):
                    continue
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise RuntimeError('Non-regular job lock; refusing switch')
                try:
                    value = json.loads(path.read_text(encoding='utf-8'))
                    pid = value.get('pid') if isinstance(value, dict) else None
                    if type(pid) is not int or pid <= 0 or pid > 2**31 - 1:
                        raise ValueError('invalid PID')
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        continue  # Only a valid, demonstrably dead PID is stale.
                    except PermissionError:
                        pass
                    raise RuntimeError('A job is still active; refusing switch')
                except (ValueError, OSError, UnicodeError) as error:
                    raise RuntimeError('Unreadable or invalid job lock; refusing switch') from error
