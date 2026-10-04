"""Rasterise a finished display list with Pillow (supersampled, then Lanczos).

The painter makes no layout decisions: positions, sizes and text lines are all
already fixed by ``layout.py``. It records every string it actually draws, so the
tests can compare the pixels' source text with the Markdown.
"""
from __future__ import annotations

import hashlib
from typing import Mapping

import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from .icons import draw_icon
from .layout import LayoutResult
from .textlayout import FontBook


def rgb(hexstr: str) -> tuple[int, int, int]:
    h = hexstr.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgba(hexstr: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
    r, g, b = rgb(hexstr)
    return r, g, b, max(0, min(255, round(alpha * 255)))


def _blend(a: str, b: str, t: float) -> tuple[int, int, int]:
    ra, rb = rgb(a), rgb(b)
    return tuple(round(x * (1 - t) + y * t) for x, y in zip(ra, rb))  # type: ignore[return-value]


def identicon_cells(speaker_id: str) -> list[list[bool]]:
    """5x5 mirrored abstract pattern, a pure function of the stable id (never a face)."""
    counter = 0
    while True:
        seed = hashlib.sha256(f"oopz-avatar:{counter}:{speaker_id}".encode("utf-8")).digest()
        bits = [bool(seed[k // 8] >> (k % 8) & 1) for k in range(15)]
        cells = [[False] * 5 for _ in range(5)]
        for r in range(5):
            for c in range(3):
                cells[r][c] = cells[r][4 - c] = bits[r * 3 + c]
        if 9 <= sum(map(sum, cells)) <= 19:  # not a blank tile, not a solid block
            return cells
        counter += 1


class Painter:
    def __init__(self, layout: LayoutResult, tokens: Mapping, fonts: FontBook,
                 avatars: Mapping[str, Image.Image] | None = None):
        self.L, self.t, self.f = layout, tokens, fonts
        self.S = int(tokens["canvas"]["scale"])
        self.col = tokens["colors"]
        self.avatars = avatars or {}
        self.drawn: list[dict] = []

    # ------------------------------------------------------------------ helpers
    def _S(self, v):
        return v * self.S

    def _paste_mask(self, color, box, mask):
        x0, y0, x1, y1 = (round(v) for v in box)
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            return
        solid = Image.new("RGB", (w, h), color)
        self.img.paste(solid, (x0, y0), mask.resize((w, h), Image.Resampling.BICUBIC) if mask.size != (w, h) else mask)

    def glow(self, op):
        r = op["r"] * self.S
        size = max(2, round(2 * r))
        # radial_gradient only reaches ~181/255 at the edge midpoints (255 is in the corners);
        # rescale so the falloff hits exactly zero at radius r and never leaves a visible box edge
        grad = Image.radial_gradient("L").point(lambda v: min(255, round(v * 255 / 181)))
        base = ImageOps.invert(grad).resize((size, size), Image.Resampling.BICUBIC)
        a = op["alpha"]
        mask = base.point(lambda v: round(255 * a * (v / 255) ** 2))
        self._paste_mask(rgb(op["color"]), (op["cx"] * self.S - size / 2, op["cy"] * self.S - size / 2,
                                            op["cx"] * self.S + size / 2, op["cy"] * self.S + size / 2), mask)

    def band(self, op):
        S = self.S
        x0, y0, x1, y1 = (round(v * S) for v in op["box"])
        w, h = x1 - x0, y1 - y0
        row = bytes(round(255 * (op["alpha_l"] + (op["alpha_r"] - op["alpha_l"]) * i / max(1, w - 1))) for i in range(w))
        mask = Image.frombytes("L", (w, 1), row).resize((w, h))
        self.img.paste(Image.new("RGB", (w, h), rgb(op["color"])), (x0, y0), mask)
        for yy in (y0, y1 - 2 * S):
            self.d.rectangle((x0, yy, x1, yy + 2 * S - 1), fill=rgba(op["edge"], 0.55))

    def rect(self, op):
        S = self.S
        box = [v * S for v in op["box"]]
        self.d.rounded_rectangle(box, radius=op.get("r", 0) * S,
                                 fill=rgba(op["fill"], op["alpha"]) if op.get("fill") else None,
                                 outline=rgba(op["outline"], op.get("oalpha", 1.0)) if op.get("outline") else None,
                                 width=round(op.get("ow", 0) * S))

    def disc(self, op):
        S, r = self.S, op["r"]
        box = ((op["cx"] - r) * S, (op["cy"] - r) * S, (op["cx"] + r) * S, (op["cy"] + r) * S)
        self.d.ellipse(box, fill=rgba(op["fill"], op["alpha"]) if op.get("fill") else None,
                       outline=rgba(op["outline"], op.get("oalpha", 1.0)) if op.get("outline") else None,
                       width=round(op.get("ow", 0) * S))

    def _dashed(self, p0, p1, on, off, fill, width, cap=False):
        (x0, y0), (x1, y1) = p0, p1
        L = math.hypot(x1 - x0, y1 - y0)
        if L == 0:
            return
        ux, uy = (x1 - x0) / L, (y1 - y0) / L
        t = 0.0
        while t < L:
            e = min(L, t + on)
            a, b = (x0 + ux * t, y0 + uy * t), (x0 + ux * e, y0 + uy * e)
            self.d.line([a, b], fill=fill, width=width)
            if cap:
                r = width / 2
                for (cx, cy) in (a, b):
                    self.d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
            t += on + off

    def hline(self, op):
        S = self.S
        y = op["y"] * S
        fill = rgba(op["color"], op["alpha"])
        if op.get("dash"):
            on, off = op["dash"]
            self._dashed((op["x0"] * S, y), (op["x1"] * S, y), on * S, off * S, fill, round(op["w"] * S))
            return
        self.d.rectangle((op["x0"] * S, y - op["w"] * S / 2, op["x1"] * S, y + op["w"] * S / 2 - 1), fill=fill)

    def line(self, op):
        S = self.S
        pts = [(x * S, y * S) for x, y in op["pts"]]
        fill = rgba(op["color"], op["alpha"])
        if op.get("dash"):
            on, off = op["dash"]
            for p0, p1 in zip(pts, pts[1:]):
                self._dashed(p0, p1, on * S, off * S, fill, round(op["w"] * S), cap=op.get("cap") == "round")
            return
        self.d.line(pts, fill=fill, width=round(op["w"] * S))

    def wave(self, op):
        S = self.S
        pts = []
        n = max(2, int((op["x1"] - op["x0"]) * 2))
        for i in range(n + 1):
            x = op["x0"] + (op["x1"] - op["x0"]) * i / n
            pts.append((x, op["y"] + op["amp"] * math.sin((x - op["x0"]) / op["length"] * 2 * math.pi)))
        for i, (p0, p1) in enumerate(zip(pts, pts[1:])):
            t = i / (len(pts) - 1)
            fade = min(1.0, t * 5, (1 - t) * 5)           # soft ends
            self.d.line([(p0[0] * S, p0[1] * S), (p1[0] * S, p1[1] * S)],
                        fill=rgba(op["color"], op["alpha"] * fade), width=round(op["w"] * S))

    def rings(self, op):
        S = self.S
        for r in op["radii"]:
            self.d.ellipse(((op["cx"] - r) * S, (op["cy"] - r) * S, (op["cx"] + r) * S, (op["cy"] + r) * S),
                           outline=rgba(op["color"], op["alpha"]), width=round(op["w"] * S))

    def dots(self, op):
        S = self.S
        y = op["y0"]
        row = 0
        while y <= op["y1"]:
            x = op["x0"] + (op["step"] / 2 if row % 2 else 0)
            while x <= op["x1"]:
                r = op["r"]
                self.d.ellipse(((x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S), fill=rgba(op["color"], op["alpha"]))
                x += op["step"]
            y += op["step"] * 0.866
            row += 1

    def slashes(self, op):
        S, h = self.S, op["h"]
        for i, a in enumerate((1.0, 0.62, 0.32)):
            x = op["x"] + i * 14
            pts = [((x + 9) * S, op["y"] * S), ((x + 16) * S, op["y"] * S), ((x + 7) * S, (op["y"] + h) * S),
                   (x * S, (op["y"] + h) * S)]
            self.d.polygon(pts, fill=rgba(op["color"], a))

    def bars(self, op):
        S = self.S
        n = len(op["hs"])
        for i, hf in enumerate(op["hs"]):
            t = i / max(1, n - 1)
            color = tuple(round(a * (1 - t) + b * t) for a, b in zip(rgb(op["c0"]), rgb(op["c1"])))
            x = op["x0"] + i * op["pitch"]
            h = op["hmax"] * hf
            fade = min(1.0, 0.35 + t * 4, 0.35 + (1 - t) * 4)
            self.d.rounded_rectangle((x * S, (op["cy"] - h / 2) * S, (x + op["bw"]) * S, (op["cy"] + h / 2) * S),
                                     radius=op["bw"] / 2 * S, fill=color + (round(255 * op["alpha"] * fade),))

    def shape(self, op):
        """Rounded rects / polygons as one mask: horizontal alpha gradient fill + optional inner outline."""
        parts = op["parts"]
        if not parts:
            return
        S = self.S
        xs, ys = [], []
        for part in parts:
            if part[0] == "rect":
                b = part[1]
                xs += [b[0], b[2]]
                ys += [b[1], b[3]]
            else:
                xs += [p[0] for p in part[1]]
                ys += [p[1] for p in part[1]]
        pad = 6
        ox, oy = math.floor(min(xs) - pad), math.floor(min(ys) - pad)
        w, h = math.ceil(max(xs) + pad) - ox, math.ceil(max(ys) + pad) - oy
        mask = Image.new("L", (w * S, h * S), 0)
        md = ImageDraw.Draw(mask)
        for part in parts:
            if part[0] == "rect":
                b = part[1]
                md.rounded_rectangle([(b[0] - ox) * S, (b[1] - oy) * S, (b[2] - ox) * S - 1, (b[3] - oy) * S - 1],
                                     radius=part[2] * S, fill=255)
            else:
                md.polygon([((x - ox) * S, (y - oy) * S) for x, y in part[1]], fill=255)
        if op.get("fill"):
            a0, a1 = op["alpha"], op["alpha_r"]
            x_lo, x_hi = (min(xs) - ox) * S, (max(xs) - ox) * S
            row = bytes(round(255 * (a0 + (a1 - a0) * min(1, max(0, (i - x_lo) / max(1, x_hi - x_lo)))))
                        for i in range(w * S))
            ramp = Image.frombytes("L", (w * S, 1), row).resize((w * S, h * S))
            fm = ImageChops.multiply(mask, ramp)
            self.img.paste(Image.new("RGB", (w * S, h * S), rgb(op["fill"])), (ox * S, oy * S), fm)
        if op.get("outline") and op.get("ow"):
            k = 2 * round(op["ow"] * S) + 1
            ring = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(k)))
            ring = ring.point(lambda v: round(v * op["oalpha"]))
            self.img.paste(Image.new("RGB", (w * S, h * S), rgb(op["outline"])), (ox * S, oy * S), ring)

    def chevron(self, op):
        S, s = self.S, op["size"]
        x, y = op["x"], op["y"]
        pts = [((x - s * 0.55) * S, (y - s * 0.65) * S), (x * S, y * S), ((x - s * 0.55) * S, (y + s * 0.65) * S)]
        self.d.line(pts, fill=rgba(op["color"], op["alpha"]), width=round(3 * S), joint="curve")

    def brandmark(self, op):
        S, s = self.S, op["size"]
        heights = (0.34, 0.62, 1.0, 0.5, 0.26)
        bar = s * 0.12
        gap = (s - bar * 5) / 4
        for i, hf in enumerate(heights):
            x = op["x"] + i * (bar + gap)
            cy = op["y"] + s / 2
            self.d.rounded_rectangle(((x) * S, (cy - s * hf / 2) * S, (x + bar) * S, (cy + s * hf / 2) * S),
                                     radius=bar / 2 * S, fill=rgba(op["color"], op.get("alpha", 1.0)))

    def icon(self, op):
        weight = self.t["decor"]["stroke"] / 3
        bg = _blend(self.col["background"], op["color"], self.t["decor"]["disc_alpha"])
        draw_icon(self.d, op["x"], op["y"], op["size"], op["cat"], rgba(op["color"]), bg, self.S, weight)

    def avatar(self, op):
        S, size = self.S, op["size"]
        x, y = op["x"] * S, op["y"] * S
        px = round(size * S)
        self.d.ellipse((x, y, x + px, y + px), fill=rgba(self.col["surface"]))
        image = self.avatars.get(op["speaker_id"])
        if image is not None:
            tile = image.convert("RGB").resize((px, px), Image.Resampling.LANCZOS)
            mask = Image.new("L", (px * 2, px * 2), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, px * 2 - 1, px * 2 - 1), fill=255)
            mask = mask.resize((px, px), Image.Resampling.LANCZOS)
            self.img.paste(tile, (round(x), round(y)), mask)
        else:
            cells = identicon_cells(op["speaker_id"])
            cell = size * 0.092
            gx = op["x"] + (size - cell * 5 - cell * 0.45 * 4) / 2
            gy = op["y"] + (size - cell * 5 - cell * 0.45 * 4) / 2
            step = cell * 1.45
            for r in range(5):
                for c in range(5):
                    if cells[r][c]:
                        cx0, cy0 = gx + c * step, gy + r * step
                        self.d.rounded_rectangle((cx0 * S, cy0 * S, (cx0 + cell) * S, (cy0 + cell) * S),
                                                 radius=cell * 0.28 * S, fill=rgba(op["accent"], 0.92))
        self.d.ellipse((x, y, x + px, y + px), outline=rgba(op["accent"], 0.9), width=round(3 * S))

    def text(self, op):
        S = self.S
        x = op["x"]
        color = rgba(op["color"])
        for run, face in self.f.runs(op["s"], op["bold"]):
            font = self.f.font(face, op["size"])
            self.d.text((x * S, op["base"] * S), run, font=font, fill=color, anchor="ls")
            x += font.getlength(run) / S
        self.drawn.append({"block": op["block"], "text": op["s"], "size": op["size"], "bbox": op["bbox"]})

    # ------------------------------------------------------------------ main
    def paint(self) -> Image.Image:
        S = self.S
        W, H = self.L.width, self.L.height
        self.img = Image.new("RGB", (W * S, H * S), rgb(self.col["background"]))
        self.d = ImageDraw.Draw(self.img, "RGBA")
        # text last: nothing can cover glyphs, whatever order decor was emitted in
        for op in (o for o in self.L.ops if o["op"] != "text"):
            getattr(self, op["op"])(op)
        for op in (o for o in self.L.ops if o["op"] == "text"):
            self.text(op)
        return self.img.resize((W, H), Image.Resampling.LANCZOS)
