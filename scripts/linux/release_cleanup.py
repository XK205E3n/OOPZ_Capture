"""Remove what a successful switch made obsolete: older release directories and old installer files.

Only names this tooling creates are touched; the current release, the one just activated, symlinks and
anything unrecognised stay.  A rollback therefore means building the old version again from its tag.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

RELEASE = re.compile(r'^v\d+\.\d+\.\d+-[0-9a-f]{12}$')
PACKAGE = re.compile(r'^oopz-capture-(v\d+\.\d+\.\d+-[0-9a-f]{12})\.zip(\.sha256)?$')
KEPT_WITH_VERSION = re.compile(r'^(bootstrap-v[\w.\-]+|(?:prepare|update)-v[\w.\-]+\.log)$')


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def prune_superseded(root: Path, keep_release_id: str, *, running_from: Path | None = None) -> list[str]:
    """Delete releases other than ``keep_release_id`` and the current one, and installer packages, bootstrap
    folders and logs of other versions.  ``running_from`` (the script being executed) is never removed."""
    root = Path(root)
    removed: list[str] = []
    version = keep_release_id.split('-')[0]
    current = (root / 'current').resolve()
    releases = root / 'releases'
    for entry in sorted(releases.iterdir()) if releases.is_dir() else ():
        if (RELEASE.fullmatch(entry.name) and entry.name != keep_release_id and entry.is_dir()
                and not entry.is_symlink() and entry.resolve() != current):
            shutil.rmtree(entry)
            removed.append(f'releases/{entry.name}')
    artifacts = root / 'artifacts'
    for entry in sorted(artifacts.iterdir()) if artifacts.is_dir() else ():
        if entry.is_symlink():
            continue
        package = PACKAGE.fullmatch(entry.name)
        if package and entry.is_file() and package.group(1) != keep_release_id:
            entry.unlink()
        elif (KEPT_WITH_VERSION.fullmatch(entry.name) and version not in entry.name
              and not (running_from is not None and _inside(running_from, entry))):
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        else:
            continue
        removed.append(f'artifacts/{entry.name}')
    return removed
