from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from oopz_capture.models import IdentityMapping, ProbeSnapshot
from oopz_capture.output import write_probe_output


class OutputTests(unittest.TestCase):
    def test_probe_output_contains_only_sanitized_diagnostics(self) -> None:
        mapping = IdentityMapping(
            oopz_uid="oopz-a",
            nickname="Alice",
            agora_uid=123,
            oopz_pid="123",
            is_bot=False,
            status="verified_remote_user_pid",
            evidence=["Person PID is present as an Agora UID"],
        )
        snapshot = ProbeSnapshot(
            events=[{"at": "2026-08-12T00:00:00Z", "type": "remote_user_joined", "uid": "123"}]
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_probe_output(root, {"session_id": "test"}, [mapping], snapshot)
            combined = "\n".join(path.read_text("utf-8") for path in root.rglob("*.*"))

        self.assertIn("Alice", combined)
        self.assertNotIn("jwt_token", combined.lower())
        self.assertNotIn("supplierSign", combined)


if __name__ == "__main__":
    unittest.main()
