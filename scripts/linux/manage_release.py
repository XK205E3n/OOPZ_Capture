#!/usr/bin/env python3
"""Linux release operations. Run from a trusted checkout/bootstrap only.

A SHA-256 from the same untrusted download is not an authenticity guarantee.
Obtain the release hash through the reviewed release channel independently.
"""
from __future__ import annotations
import argparse
import fcntl
import grp
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from release_archive import extract, validate
from release_locks import check_idle
from release_transaction import atomic_write, recover, switch, systemctl

HERE = Path(__file__).resolve().parent
SHARED_DIRS = ('config', 'models', 'output', 'feishu_state', 'logs', 'home', 'browsers', 'tools')


def real_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise RuntimeError(f'Expected real directory: {path}')
    path.mkdir(parents=True, exist_ok=True)


def account(name: str):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*\$?', name):
        raise ValueError('Invalid service account')
    value = pwd.getpwnam(name)
    if value.pw_uid == 0:
        raise ValueError('Service account must not be root')
    if os.geteuid() not in (0, value.pw_uid):
        raise ValueError('Run as root or the specified service account')
    return value


def own(path: Path, user) -> None:
    if os.geteuid() == 0:
        os.chown(path, user.pw_uid, user.pw_gid, follow_symlinks=False)


def environment(root: Path) -> dict:
    result = os.environ.copy()
    result.update(HOME=str(root / 'shared/home'),
                  OOPZ_ENV_FILE=str(root / 'shared/config/.env'),
                  PLAYWRIGHT_BROWSERS_PATH=str(root / 'shared/browsers'))
    # The tarball runtime is bin/node, not node at its extraction root.
    result['PATH'] = str(root / 'shared/tools/node/bin') + os.pathsep + result.get('PATH', '/usr/bin:/bin')
    node = shutil.which('node', path=result['PATH'])
    if node:
        result['OOPZ_NODE_PATH'] = node
        result['PATH'] = str(Path(node).parent) + os.pathsep + result['PATH']
    return result


def run_as(user, command: list[str], release: Path, root: Path) -> None:
    prefix = ['runuser', '-u', user.pw_name, '--'] if os.geteuid() == 0 else []
    subprocess.run(prefix + command, cwd=release, env=environment(root), check=True)


@contextmanager
def management_lock(root: Path):
    real_directory(root)
    path = root / '.management.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        import stat
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError('Management lock must be a regular file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def release_path(root: Path, release_id: str) -> Path:
    if not re.fullmatch(r'v[0-9][A-Za-z0-9._-]*-[0-9a-f]{12}', release_id):
        raise ValueError('Invalid release ID')
    releases = root / 'releases'
    real_directory(releases)
    target = releases / release_id
    if target.is_symlink():
        raise RuntimeError('Release directory must not be a symlink')
    return target


def verify_shared_links(root: Path, release: Path) -> None:
    expected = {'.env': root / 'shared/config/.env'}
    expected.update({key: root / 'shared' / key for key in ('models', 'output', 'feishu_state', 'logs')})
    for raw in (root / 'shared/config/.env').read_text(encoding='utf-8-sig').splitlines():
        key, separator, value = raw.strip().partition('=')
        key = key.strip()
        if separator and key in ('OOPZ_OUTPUT_ROOT', 'OOPZ_FEISHU_STATE_ROOT'):
            value = value.strip().strip('\"\'')
            if value:
                path = Path(value)
                configured = path if path.is_absolute() else release / path
                wanted = root / 'shared' / ('output' if key == 'OOPZ_OUTPUT_ROOT' else 'feishu_state')
                if configured.resolve() != wanted.resolve():
                    raise RuntimeError('Configured runtime path must use the shared deployment directory')
    for name, destination in expected.items():
        link = release / name
        if not link.is_symlink() or link.resolve() != destination.resolve():
            raise RuntimeError('Release shared data/config link is missing or changed')


def prepare(args, user) -> str:
    root = args.root
    release_id, archive = validate(args.artifact, args.sha256)
    target = release_path(root, release_id)
    staged = root / 'releases' / ('.preparing-' + release_id)
    try:
        if (root / 'current').resolve() == target.resolve():
            raise RuntimeError('Refusing to prepare or clean the current release')
        if target.exists():
            marker = target / '.prepared.json'
            if marker.is_file() and json.loads(marker.read_text())['sha256'] == args.sha256.lower():
                verify_shared_links(root, target)
                return release_id
            pending = target / '.preparing.json'
            if not pending.is_file() or json.loads(pending.read_text()).get('sha256') != args.sha256.lower():
                raise RuntimeError('Existing release is not this verified prepared artifact')
            shutil.rmtree(target)
        if staged.exists() or staged.is_symlink():
            if staged.is_symlink() or (root / 'current').resolve() == staged.resolve():
                raise RuntimeError('Unsafe interrupted preparation directory')
            marker = staged / '.preparing.json'
            if not marker.is_file() or json.loads(marker.read_text()).get('sha256') != args.sha256.lower():
                raise RuntimeError('Cannot safely retry unknown preparation directory')
            shutil.rmtree(staged)
        staged.mkdir()
        atomic_write(staged / '.preparing.json', json.dumps({'sha256': args.sha256.lower()}).encode(), 0o600)
        extract(archive, staged)
    finally:
        archive.close()
        archive._oopz_stream.close()
    # Build the venv at its final path: its entry-point shebangs are absolute.
    os.replace(staged, target)
    staged = target
    shared = root / 'shared'
    real_directory(shared)
    for name in SHARED_DIRS:
        path = shared / name
        fresh = not path.exists()
        real_directory(path)
        if fresh:
            own(path, user)
            path.chmod(0o700)
    config = shared / 'config/.env'
    if config.is_symlink() or (config.exists() and not config.is_file()):
        raise RuntimeError('Shared .env must be a regular file')
    if not config.exists():
        atomic_write(config, (staged / '.env.example').read_bytes(), 0o600)
        own(config, user)
    for name, destination in {'.env': config, **{k: shared / k for k in ('models', 'output', 'feishu_state', 'logs')}}.items():
        (staged / name).symlink_to(destination)
    for folder, dirs, files in os.walk(staged, followlinks=False):
        own(Path(folder), user)
        for name in dirs + files:
            own(Path(folder) / name, user)
    run_as(user, ['bash', str(staged / 'scripts/linux/prepare_dependencies.sh')], staged, root)
    # The runtime cannot mutate release code. Shared symlink targets stay writable.
    if os.geteuid() == 0:
        for folder, dirs, files in os.walk(staged, followlinks=False):
            os.chown(folder, 0, 0)
            for name in dirs + files:
                os.chown(Path(folder) / name, 0, 0, follow_symlinks=False)
    atomic_write(staged / '.prepared.json', json.dumps({'sha256': args.sha256.lower(), 'release_id': release_id, 'node_path': environment(root).get('OOPZ_NODE_PATH', '')}).encode(), 0o644)
    (staged / '.preparing.json').unlink()
    return release_id


def render(path: Path, root: Path, user, *, systemd: bool) -> bytes:
    def quoted(value: str) -> str:
        if any(c in value for c in ('\n', '\r', '\x00')):
            raise ValueError('Invalid deployment path')
        value = value.replace('\\', '\\\\').replace('"', '\\"')
        return value.replace('%', '%%') if systemd else value
    marker = json.loads((path.parents[2] / '.prepared.json').read_text())
    node = Path(marker['node_path'])
    if not node.is_absolute() or not node.is_file():
        raise RuntimeError('Prepared Node runtime is unavailable')
    value = quoted(str(root))
    return (path.read_text().replace('@NODE_PATH@', quoted(str(node)))
            .replace('@NODE_BIN@', quoted(str(node.parent)))
            .replace('@ROOT_EXEC@', value.replace('$', '$$')).replace('@ROOT@', value)
            .replace('@USER@', user.pw_name).replace('@GROUP@', grp.getgrgid(user.pw_gid).gr_name).encode())


def bootstrap_update(args) -> None:
    # Extract the COMPLETE validated archive; never copy an installer alone.
    release_id, archive = validate(args.artifact, args.sha256)
    real_directory(args.root)
    real_directory(args.root / 'artifacts')
    with tempfile.TemporaryDirectory(prefix='.verified-', dir=args.root / 'artifacts') as temporary:
        try:
            extract(archive, Path(temporary))
        finally:
            archive.close()
            archive._oopz_stream.close()
        installer = str(Path(temporary) / 'scripts/linux/install_release.sh')
        common = ['--root', str(args.root), '--user', args.user,
                  '--unit-dir', str(args.unit_dir), '--logrotate-dir', str(args.logrotate_dir)]
        subprocess.run(['bash', installer, 'prepare', '--artifact', str(args.artifact), '--sha256', args.sha256, *common], check=True)
        extra = (['--force'] if args.force else []) + (['--enable'] if args.enable else [])
        subprocess.run(['bash', installer, 'activate', '--release-id', release_id,
                        '--health-timeout', str(args.health_timeout), *extra, *common], check=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'setup', 'activate', 'update', 'recover', 'status', 'start', 'stop', 'restart'])
    parser.add_argument('--root', type=Path, default=Path('/opt/oopz'))
    parser.add_argument('--user', default='oopz')
    parser.add_argument('--artifact', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--release-id')
    parser.add_argument('--unit-dir', type=Path, default=Path('/etc/systemd/system'))
    parser.add_argument('--logrotate-dir', type=Path, default=Path('/etc/logrotate.d'))
    parser.add_argument('--health-timeout', type=float, default=90)
    parser.add_argument('--force', action='store_true', help='Explicitly bypass idle-state guard; may interrupt work')
    parser.add_argument('--enable', action='store_true', help='Enable service at boot (otherwise preserve prior setting)')
    args = parser.parse_args(argv)
    args.root = args.root.absolute()
    args.unit_dir = args.unit_dir.absolute()
    args.logrotate_dir = args.logrotate_dir.absolute()
    if args.artifact:
        args.artifact = args.artifact.absolute()
    if args.command in ('prepare', 'update') and (not args.artifact or not args.sha256):
        parser.error('--artifact and trusted --sha256 are required')
    if args.command in ('setup', 'activate') and not args.release_id:
        parser.error('--release-id is required')
    if args.health_timeout <= 0:
        parser.error('--health-timeout must be positive')
    user = account(args.user)
    if args.command == 'update':
        bootstrap_update(args)
        return 0
    with management_lock(args.root):
        if args.command == 'recover':
            recover(args.root, args.unit_dir / 'oopz-capture.service', args.logrotate_dir / 'oopz-capture')
            return 0
        if os.path.lexists(args.root / '.switch-journal.json'):
            raise RuntimeError('Unfinished switch: inspect and run recover first')
        if args.command in ('status', 'start', 'stop', 'restart'):
            result = systemctl(args.command, 'oopz-capture.service', check=False)
            print(result.stdout, end='')
            return result.returncode
        if args.command == 'prepare':
            print(prepare(args, user))
            return 0
        release = release_path(args.root, args.release_id)
        if not (release / '.prepared.json').is_file():
            raise RuntimeError('Release has not completed preparation')
        verify_shared_links(args.root, release)
        if args.command == 'setup':
            run_as(user, [str(release / '.venv/bin/python'), '-m', 'oopz_capture.feishu_cli', 'setup'], release, args.root)
            return 0
        if not args.force:
            check_idle(args.root / 'shared')
        if not args.unit_dir.is_dir() or not args.logrotate_dir.is_dir():
            raise RuntimeError('Systemd and logrotate directories must already exist')
        switch(args.root, release, args.unit_dir / 'oopz-capture.service',
               args.logrotate_dir / 'oopz-capture',
               render(release / 'scripts/linux/oopz-capture.service', args.root, user, systemd=True),
               render(release / 'scripts/linux/oopz-capture.logrotate', args.root, user, systemd=False),
               args.health_timeout, args.enable)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:
        print(f'Linux release operation failed: {error}', file=sys.stderr)
        raise SystemExit(1)
