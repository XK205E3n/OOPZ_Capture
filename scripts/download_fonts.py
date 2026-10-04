#!/usr/bin/env python3
"""Download the fonts used by the digest renderer into assets/fonts (not stored in Git).

Every file is pinned to an immutable upstream URL (a commit or a release tag) and
verified by SHA-256; existing correct files are kept.  All fonts are SIL OFL or the
DejaVu licence and may be redistributed.

    python scripts/download_fonts.py                 # -> assets/fonts
    python scripts/download_fonts.py --target DIR    # e.g. the shared directory on a server
    python scripts/download_fonts.py --check         # verify only, download nothing
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

CJK = "https://raw.githubusercontent.com/notofonts/noto-cjk/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/OTF/SimplifiedChinese/"
GFONTS = "https://raw.githubusercontent.com/google/fonts/"
DEJAVU_TAR = ("https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.tar.bz2",
              "fa9ca4d13871dd122f61258a80d01751d603b4d3ee14095d65453b4e846e17d7")

# name -> (url, sha256)
DIRECT = {
    "NotoSansCJKsc-Regular.otf": (CJK + "NotoSansCJKsc-Regular.otf", "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"),
    "NotoSansCJKsc-Bold.otf": (CJK + "NotoSansCJKsc-Bold.otf", "b5f0d1a190a7f9b43c310a8850630af12553df32c4c050543f9059732d9b4c0a"),
    "NotoEmoji-Variable.ttf": (GFONTS + "b979dba422e445492b0eb9951ac52ee0b4d648c3/ofl/notoemoji/NotoEmoji%5Bwght%5D.ttf",
                               "de6c18832938afc99caf132b39d6a30a19bac7f2e812e28db2535b4608d27551"),
    "NotoSansSymbols2-Regular.ttf": (GFONTS + "7b6724ac7ececc713e9ba93af309f7520c9a80a3/ofl/notosanssymbols2/NotoSansSymbols2-Regular.ttf",
                                     "7d5fb73b7ca67a6798101741f5d280a3d016a56a197afcd4199dbb57b4b82a21"),
}
# files taken out of the DejaVu tarball: name -> sha256
FROM_TAR = {
    "DejaVuSans.ttf": "7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954",
    "DejaVuSans-Bold.ttf": "e6476c1b80502924294eed40894c5b18e06c181444ca953e5334262df9c27724",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "oopz-capture-fonts/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def install(path: Path, data: bytes, expected: str) -> None:
    if sha256(data) != expected:
        raise SystemExit(f"checksum mismatch for {path.name}; refusing to install")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(handle, "wb") as stream:
        stream.write(data)
    os.replace(temp, path)


def is_good(path: Path, expected: str) -> bool:
    return path.is_file() and sha256(path.read_bytes()) == expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", type=Path, default=Path(__file__).resolve().parents[1] / "assets" / "fonts")
    parser.add_argument("--check", action="store_true", help="verify the files only")
    args = parser.parse_args()
    expected = {name: digest for name, (_, digest) in DIRECT.items()} | FROM_TAR
    missing = [name for name, digest in expected.items() if not is_good(args.target / name, digest)]
    if args.check:
        print("OK" if not missing else "missing or damaged: " + ", ".join(missing))
        return 1 if missing else 0
    for name in [n for n in DIRECT if n in missing]:
        url, digest = DIRECT[name]
        install(args.target / name, fetch(url), digest)
        print("installed", name)
    if any(n in missing for n in FROM_TAR):
        url, digest = DEJAVU_TAR
        archive = fetch(url)
        if sha256(archive) != digest:
            raise SystemExit("checksum mismatch for the DejaVu archive")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:bz2") as tar:
            for name in [n for n in FROM_TAR if n in missing]:
                install(args.target / name, tar.extractfile(f"dejavu-fonts-ttf-2.37/ttf/{name}").read(), FROM_TAR[name])
                print("installed", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
