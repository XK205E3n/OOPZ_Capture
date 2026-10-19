import re
from pathlib import Path

import oopz_capture


def test_package_version_matches_pyproject():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
    assert oopz_capture.__version__ == declared
