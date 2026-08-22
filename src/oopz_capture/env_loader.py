"""Minimal project .env loader.

The project stores credentials and machine-specific settings in a
gitignored ``.env`` file at the project root.  This module reads it
without requiring an extra dependency.  Variables already present in the
process environment are never overwritten, so a value set in the shell
always wins over the file.
"""

from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"


def load_project_env(env_path: Path | None = None) -> Path | None:
    """Load KEY=VALUE pairs from env_path (default: <project root>/.env).

    Returns the loaded path, or None when the file does not exist.
    Blank lines and ``#`` comments are ignored; values may be wrapped in
    matching single or double quotes.
    """
    path = Path(env_path) if env_path is not None else _DEFAULT_ENV_PATH
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value
    return path