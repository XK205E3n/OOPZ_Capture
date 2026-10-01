#!/usr/bin/env python3
"""Gate the PDF/Node toolchain on the runtime required by pnpm-lock.yaml.

pnpm-lock.yaml's importer chain pins puppeteer 25.7.0 whose engines field is
node >=22.12.0; a distro Node 18 passes md-to-pdf's own metadata but breaks
the frozen-lockfile install.  The default minimum mirrors that contract; bump
it together with the lockfile, never independently.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

DEFAULT_MINIMUM = "22.12.0"


def _parse(version_text: str) -> tuple[int, int, int] | None:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", version_text.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def check(node_bin: str, minimum: str) -> tuple[bool, str]:
    min_parts = _parse(minimum)
    if min_parts is None:
        return False, f"invalid --minimum value: {minimum}"
    command = [node_bin, "--version"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except OSError:
        # The target is a shebang script the OS cannot exec directly (only
        # happens in behavior-test sandboxes on Windows); retry through bash.
        import shutil

        bash = shutil.which("bash")
        if bash is None:
            return False, f"could not run {node_bin} --version"
        try:
            result = subprocess.run([bash, node_bin, "--version"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            return False, f"could not run {node_bin} --version: {error}"
    except subprocess.SubprocessError as error:
        return False, f"could not run {node_bin} --version: {error}"
    if result.returncode != 0:
        return False, f"{node_bin} --version failed: {result.stderr.strip()[:200]}"
    version_text = result.stdout.strip()
    parts = _parse(version_text)
    if parts is None:
        return False, f"cannot parse Node version output: {version_text!r}"
    if parts >= min_parts:
        return True, f"Node {version_text} satisfies >={minimum} (pnpm-lock contract)"
    return False, (f"Node {version_text} is too old: pnpm-lock.yaml requires >={minimum} "
                   "(puppeteer 25.x). Install the pinned Node 22 LTS runtime via "
                   "scripts/linux/install_prerequisites.sh or point the shared tools/node "
                   "link at a compatible runtime.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_node.py", description=__doc__.splitlines()[0])
    parser.add_argument("--node-bin", default="node", help="Node executable to check (default: PATH node)")
    parser.add_argument("--minimum", default=DEFAULT_MINIMUM, help=f"minimum version (default {DEFAULT_MINIMUM})")
    args = parser.parse_args(argv)
    ok, message = check(args.node_bin, args.minimum)
    print(("NODE-OK " if ok else "NODE-FAIL ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
