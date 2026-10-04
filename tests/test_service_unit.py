"""The systemd unit template must keep the isolation that replaces a separate service account."""
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "scripts" / "linux" / "oopz-capture.service"


def test_unit_is_sandboxed_for_the_shared_admin_account():
    lines = {line.strip() for line in UNIT.read_text(encoding="utf-8").splitlines()}
    for required in ("NoNewPrivileges=yes", "ProtectHome=yes", "PrivateTmp=yes", "UMask=0077"):
        assert required in lines
