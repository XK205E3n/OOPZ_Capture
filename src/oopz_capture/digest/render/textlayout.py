"""Font loading, glyph coverage, text sanitising, line breaking and measuring.

Everything here is deterministic and offline. Measuring happens with the same
font objects (at the same supersampled pixel size) that the painter later uses,
so a line that was measured to fit is drawn exactly as measured.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont


class RenderError(Exception):
    """Explicit, machine-readable render failure. Never carries model text."""

    def __init__(self, code: str, message: str = "", **details):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.details = details


class MissingResource(RenderError):
    pass


class MissingGlyph(RenderError):
    pass


# --------------------------------------------------------------------------- text hygiene

_BIDI = {chr(c) for c in (0x200E, 0x200F, 0x061C, *range(0x202A, 0x202F), *range(0x2066, 0x206A))}
_ZERO_WIDTH = {chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, *range(0xFE00, 0xFE10))}


def sanitize_text(value: object) -> str:
    """Return display text with no controls, bidi overrides or zero-width marks.

    Untrusted text is only ever treated as text. Line breaks and tabs become one
    space; other control/format characters are dropped. The same sanitised string
    feeds both the PNG and the Markdown, so both stay identical.
    """
    if not isinstance(value, str):
        raise RenderError("text:not_a_string")
    out = []
    for ch in value:
        if ch in _BIDI or ch in _ZERO_WIDTH:
            continue
        cat = unicodedata.category(ch)
        if ch in "\n\r\t\v\f\u00a0\u0085\u2028\u2029":
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return re.sub(r" {2,}", " ", "".join(out)).strip()


# --------------------------------------------------------------------------- fonts


@dataclass(frozen=True)
class FontSpec:
    regular: Path
    bold: Path
    fallback_regular: Path
    fallback_bold: Path
    extra: tuple = ()          # further fallback faces (monochrome emoji, symbols), tried in order
    strict: bool = False       # True: an undrawable character raises MissingGlyph instead of being replaced

    @classmethod
    def from_tokens(cls, tokens: dict, font_dir: Path, regular=None, bold=None) -> "FontSpec":
        f = tokens["fonts"]
        base = Path(font_dir)
        return cls(
            Path(regular) if regular else base / f["regular"],
            Path(bold) if bold else base / f["bold"],
            base / f["fallback_regular"],
            base / f["fallback_bold"],
            tuple(base / n for n in f.get("extra", [])),
            f.get("missing_glyph", "replace") == "fail",
        )


class FontBook:
    """Primary CJK face with a per-character fallback; strict about missing glyphs."""

    def __init__(self, spec: FontSpec, scale: int):
        self.spec, self.scale = spec, scale
        self._fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
        self._cov: dict[tuple[str, str], bool] = {}
        self._notdef: dict[str, bytes] = {}
        self.substituted: dict[int, int] = {}   # codepoint -> times replaced by U+FFFD
        for path in (spec.regular, spec.bold, spec.fallback_regular, spec.fallback_bold, *spec.extra):
            if not Path(path).is_file():
                raise MissingResource("font:missing", Path(path).name)

    def font(self, face: str, size: float) -> ImageFont.FreeTypeFont:
        px = max(1, round(size * self.scale))
        key = (face, px)
        if key not in self._fonts:
            try:
                self._fonts[key] = ImageFont.truetype(
                    str(self._path(face)), px, layout_engine=ImageFont.Layout.BASIC)
            except OSError as exc:  # unreadable / corrupt font
                raise MissingResource("font:unreadable", Path(self._path(face)).name) from exc
        return self._fonts[key]

    def _path(self, face: str) -> Path:
        if face.startswith("extra"):
            return self.spec.extra[int(face[5:])]
        return getattr(self.spec, face)

    @staticmethod
    def _probe(font: ImageFont.FreeTypeFont, ch: str) -> bytes:
        im = Image.new("L", (96, 96), 0)
        ImageDraw.Draw(im).text((8, 8), ch, font=font, fill=255)
        return im.tobytes()

    def has_glyph(self, face: str, ch: str) -> bool:
        key = (face, ch)
        if key in self._cov:
            return self._cov[key]
        font = self.font(face, 32 / self.scale)
        if face not in self._notdef:
            self._notdef[face] = self._probe(font, "\U0010ffff")  # never mapped: draws .notdef
        ok = self._probe(font, ch) != self._notdef[face]
        self._cov[key] = ok
        return ok

    def face_for(self, ch: str, bold: bool) -> str:
        primary = "bold" if bold else "regular"
        if self.has_glyph(primary, ch):
            return primary
        fb = "fallback_bold" if bold else "fallback_regular"
        if self.has_glyph(fb, ch):
            return fb
        for i in range(len(self.spec.extra)):
            if self.has_glyph(f"extra{i}", ch):
                return f"extra{i}"
        raise MissingGlyph("glyph:missing", f"U+{ord(ch):04X}", codepoint=ord(ch))

    def drawable(self, ch: str) -> bool:
        try:
            self.face_for(ch, False)
            self.face_for(ch, True)
            return True
        except MissingGlyph:
            return False

    def fix(self, text: str) -> str:
        """Make ``text`` fully drawable. Characters no bundled face has become U+FFFD
        (and are counted in ``substituted``); with ``strict`` they raise MissingGlyph."""
        out = []
        for ch in text:
            if self.drawable(ch):
                out.append(ch)
            elif self.spec.strict:
                raise MissingGlyph("glyph:missing", f"U+{ord(ch):04X}", codepoint=ord(ch))
            else:
                self.substituted[ord(ch)] = self.substituted.get(ord(ch), 0) + 1
                out.append("�")
        return "".join(out)

    def runs(self, text: str, bold: bool) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for ch in text:
            face = self.face_for(ch, bold)
            if out and out[-1][1] == face:
                out[-1] = (out[-1][0] + ch, face)
            else:
                out.append((ch, face))
        return out

    def width(self, text: str, bold: bool, size: float) -> float:
        """Advance width in 1x pixels."""
        return sum(self.font(face, size).getlength(run) for run, face in self.runs(text, bold)) / self.scale

    def metrics(self, bold: bool, size: float) -> tuple[float, float]:
        asc, desc = self.font("bold" if bold else "regular", size).getmetrics()
        return asc / self.scale, desc / self.scale


# --------------------------------------------------------------------------- line breaking

NON_STARTERS = set("，。、；：！？）》」』】〕〉”’％‰°…—·,.;:!?)]}%")
OPENERS = set("（《「『【〔〈“‘([{")
_PAIR_CHARS = "—…"


def _is_latin_char(ch: str) -> bool:
    o = ord(ch)
    if ch.isspace() or o >= 0x2E80:
        return False
    if 0x2000 <= o <= 0x206F:  # general punctuation: quotes, dashes, ellipsis stand alone
        return False
    if 0x2190 <= o <= 0x2BFF:  # arrows, math, symbols
        return False
    return True


def units(text: str) -> list[str]:
    """Break opportunities: whole Latin/digit words, single CJK chars, '——'/'……' pairs."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            out.append(" ")
            i = j
        elif _is_latin_char(ch):
            j = i
            while j < n and _is_latin_char(text[j]):
                j += 1
            out.append(text[i:j])
            i = j
        elif ch in _PAIR_CHARS:
            j = i
            while j < n and text[j] == ch:
                j += 1
            out.append(text[i:j])
            i = j
        else:
            out.append(ch)
            i += 1
    return out


def wrap(fonts: FontBook, text: str, bold: bool, size: float, max_w: float, hang_em: float = 1.5) -> list[str]:
    """Greedy wrap that never truncates.

    * CJK breaks between any two characters; Latin/number words stay whole.
    * A word wider than a whole line (long IDs) is split at the character level,
      preferably after ``_ - . /``.
    * Kinsoku: a line never starts with closing punctuation and never ends with an
      opening one. A single closing mark may hang into the margin (at most
      ``hang_em`` em, which is why ``margin_x`` is larger than that); otherwise the
      character before it moves down together with it.
    """
    text = fonts.fix(sanitize_text(text))
    if not text:
        return [""]
    hang = hang_em * size
    lines: list[str] = []
    cur: list[str] = []

    def w(s: str) -> float:
        return fonts.width(s, bold, size)

    def push_split(unit: str, base: str) -> str:
        """Place an over-wide word character by character; return the open remainder."""
        buf = base
        for ch in unit:
            if buf and w(buf + ch) > max_w:
                cut = max(buf.rfind(sep) for sep in "_-./")  # prefer a natural seam inside long IDs
                if cut >= 3 and w(buf[:cut + 1]) > max_w * 0.5:
                    lines.append(buf[:cut + 1])
                    buf = buf[cut + 1:]
                else:
                    lines.append(buf.rstrip())
                    buf = ""
            buf += ch
        return buf

    for u in units(text):
        if u == " ":
            if cur:
                cur.append(" ")
            continue
        cand = "".join(cur + [u]).rstrip()
        if w(cand) <= max_w:
            cur.append(u)
            continue
        nonstart = u[0] in NON_STARTERS
        if "".join(cur).strip() and nonstart and w(u) <= hang and w(cand) <= max_w + hang                 and (len(cur) < 2 or cur[-1][0] not in NON_STARTERS):
            cur.append(u)  # one hanging closing mark
            continue
        if not "".join(cur).strip():
            cur = [push_split(u, "")] if len(u) > 1 else [u]
            continue
        while cur and cur[-1] == " ":
            cur.pop()
        carry: list[str] = []
        if nonstart:  # never begin a line with a non-starter: bring the preceding character down too
            while len(cur) > 1:
                last = cur.pop()
                carry.insert(0, last)
                if last != " " and last[0] not in NON_STARTERS:
                    break
        while len(cur) > 1 and cur[-1][-1] in OPENERS:  # never end a line with an opener
            carry.insert(0, cur.pop())
        lines.append("".join(cur).rstrip())
        while carry and carry[0] == " ":
            carry.pop(0)
        cur = carry + [u]
        if w("".join(cur)) > max_w:
            cur = [push_split(u, "".join(carry))] if len(u) > 1 or carry else cur
    if "".join(cur).strip():
        lines.append("".join(cur).rstrip())
    return _avoid_widow(lines or [""])


def _avoid_widow(lines: list[str]) -> list[str]:
    """Never leave a 1-2 character last line: pull characters down from the line above.

    Only plain CJK/punctuation tails are touched (no Latin/number word is ever split),
    and kinsoku is kept: the moved run never starts with a non-starter and the line
    above never ends with an opener.
    """
    if len(lines) < 2:
        return lines
    last, prev = lines[-1], lines[-2]
    if not last or len(last.rstrip("".join(NON_STARTERS))) > 2 or any(ord(c) < 0x2E80 and c.isalnum() for c in last):
        return lines
    for k in (4 - len(last), 5 - len(last)):
        if k < 1:
            continue
        moved, rest = prev[-k:], prev[:-k]
        if (len(rest) >= 6 and moved[0] not in NON_STARTERS and rest[-1] not in OPENERS
                and not any(ord(c) < 0x2E80 and (c.isalnum() or c == " ") for c in moved)):
            return lines[:-2] + [rest, moved + last]
    return lines


@dataclass(frozen=True)
class Line:
    text: str
    x: float          # left edge in 1x px (absolute)
    width: float


@dataclass(frozen=True)
class TextLayout:
    lines: tuple[Line, ...]
    size: float
    bold: bool
    lh: float          # line height, 1x px
    ascent: float
    descent: float
    height: float
    width: float       # widest line
    source: str = ""  # the sanitised paragraph these lines were wrapped from

    def baseline(self, top: float, i: int) -> float:
        pad = (self.lh - (self.ascent + self.descent)) / 2
        return top + i * self.lh + pad + self.ascent


def layout_text(fonts: FontBook, text: str, *, x: float, w: float, size: float, bold: bool,
                lh: float, align: str = "left", hang_em: float = 1.5) -> TextLayout:
    lines = wrap(fonts, text, bold, size, w, hang_em)
    asc, desc = fonts.metrics(bold, size)
    out = []
    for s in lines:
        lw = fonts.width(s, bold, size)
        if align == "center":
            lx = x + (w - lw) / 2
        elif align == "right":
            lx = x + w - lw
        else:
            lx = x
        out.append(Line(s, lx, lw))
    lh_px = round(size * lh)
    return TextLayout(tuple(out), size, bold, lh_px, asc, desc, lh_px * len(out),
                      max((l.width for l in out), default=0.0), fonts.fix(sanitize_text(text)))


def check_coverage(fonts: FontBook, texts: Iterable[str]) -> None:
    """Raise MissingGlyph for the first character no bundled face can draw."""
    seen: set[str] = set()
    for t in texts:
        for ch in t:
            if ch not in seen:
                seen.add(ch)
                fonts.face_for(ch, False)
                fonts.face_for(ch, True)
