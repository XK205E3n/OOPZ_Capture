from __future__ import annotations

import unittest

from oopz_capture.main import main


class MainTests(unittest.TestCase):
    def test_probe_requires_explicit_participant_awareness_confirmation(self) -> None:
        result = main(["probe", "--area", "area", "--channel", "channel"])

        self.assertEqual(result, 3)


if __name__ == "__main__":
    unittest.main()
