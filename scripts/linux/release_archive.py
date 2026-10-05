"""Validate the pinned archive before extracting or executing any release code."""
from __future__ import annotations
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

REQUIRED = {
    'RELEASE_MANIFEST.json', 'pyproject.toml', '.env.example',
    'scripts/linux/install_release.sh', 'scripts/linux/update_release.sh',
    'scripts/linux/rollback_release.sh', 'scripts/linux/manage_release.py',
    'scripts/linux/release_archive.py', 'scripts/linux/release_cleanup.py', 'scripts/linux/release_locks.py',
    'scripts/linux/release_transaction.py', 'scripts/linux/prepare_dependencies.sh',
    'scripts/linux/oopz-capture.service', 'scripts/linux/oopz-capture.logrotate',
    'scripts/download_sensevoice_model.py',
}
BLOCKED = {'.env', '.venv', 'models', 'output', 'feishu_state', 'logs',
           'node_modules', 'artifacts', '.git', '.prepared.json', '.preparing.json'}


def validate(artifact: Path, digest: str) -> tuple[str, zipfile.ZipFile]:
    if not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise ValueError('A trusted, independently obtained SHA-256 is required')
    # Keep the same file descriptor for hashing and extraction (no path TOCTOU).
    stream = artifact.open('rb')
    try:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual.lower() != digest.lower():
            raise ValueError('Release SHA-256 mismatch')
        stream.seek(0)
        archive = zipfile.ZipFile(stream)
        names = set()
        total = 0
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (not entry.filename or '\\' in entry.filename or path.is_absolute()
                    or any(p in ('', '.', '..') or ':' in p for p in entry.filename.rstrip('/').split('/'))
                    or any(p in BLOCKED for p in path.parts)
                    or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                    or entry.filename in names):
                raise ValueError('Unsafe or duplicate archive entry')
            names.add(entry.filename)
            total += entry.file_size
            if total > 256 * 1024 * 1024:
                raise ValueError('Release archive is unexpectedly large')
        if not REQUIRED <= names:
            raise ValueError('Release management helper set is incomplete: ' + ', '.join(sorted(REQUIRED - names)))
        manifest = json.loads(archive.read('RELEASE_MANIFEST.json').decode('utf-8-sig'))
        release_id = manifest.get('release_id', '')
        if not re.fullmatch(r'v[0-9][A-Za-z0-9._-]*-[0-9a-f]{12}', release_id):
            raise ValueError('Invalid release ID')
        if not re.fullmatch(r'[0-9a-f]{40}', manifest.get('git_commit', '')):
            raise ValueError('Invalid release commit')
        if release_id.rsplit('-', 1)[1] != manifest['git_commit'][:12]:
            raise ValueError('Release ID and commit disagree')
        archive._oopz_stream = stream  # closed explicitly with archive below
        return release_id, archive
    except BaseException:
        stream.close()
        raise


def extract(archive: zipfile.ZipFile, destination: Path) -> None:
    try:
        archive.extractall(destination)
    finally:
        archive.close()
        archive._oopz_stream.close()
