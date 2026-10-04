"""Digest rendering needs the fonts that scripts/download_fonts.py fetches (they are not stored in Git)."""
import pytest

from oopz_capture.digest.render.tokens import FONT_DIR

FONT_FILES = ("NotoSansCJKsc-Regular.otf", "NotoSansCJKsc-Bold.otf", "DejaVuSans.ttf", "DejaVuSans-Bold.ttf",
              "NotoEmoji-Variable.ttf", "NotoSansSymbols2-Regular.ttf")
RENDER_TESTS = {"test_digest_render.py", "test_digest_safety.py"}


def pytest_collection_modifyitems(config, items):
    if all((FONT_DIR / name).is_file() for name in FONT_FILES):
        return
    skip = pytest.mark.skip(reason="fonts missing: run python scripts/download_fonts.py")
    for item in items:
        if item.path.name in RENDER_TESTS or "needs_fonts" in item.keywords:
            item.add_marker(skip)
