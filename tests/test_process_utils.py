from __future__ import annotations

import os

from oopz_capture.process_utils import pid_is_running


def test_pid_probe_is_read_only_for_current_process() -> None:
    assert pid_is_running(os.getpid()) is True


def test_invalid_pid_is_not_running() -> None:
    assert pid_is_running(0) is False
