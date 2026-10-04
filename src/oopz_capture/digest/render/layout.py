"""Measure-first layout: view -> display list + ordered visible-text blocks.

Nothing is drawn here. Every text run is measured with the real fonts, every
container height is derived from those measurements, and the final canvas height
is known *before* a single pixel is allocated. If the result would exceed the
height limit, layout raises ``RenderError('height_exceeded')`` and nothing is
dropped, shrunk or paginated.

The ordered ``blocks`` list is the one source of truth for visible text: the
Markdown exporter and the text-consistency tests both read it.

Decoration (equalizer strips, slanted band, hex frames, speech bubbles, wave
dividers, rings ...) is *fixed template geometry*. It never needs extra input:
the only per-digest variation is a seed derived from the headline, so every
digest gets its own equalizer silhouette without anybody choosing anything.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from typing import Mapping

from .textlayout import FontBook, RenderError, TextLayout, layout_text


@dataclass
class LayoutResult:
    width: int
    height: int
    ops: list[dict]
    blocks: list[dict]          # ordered visible text: {id, role, text, md}
    modules: list[dict]         # content modules in reading order: {kind, top, bottom}
    header_height: int
    min_font_px: float
    warnings: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        raw = json.dumps([self.width, self.height, self.ops, self.blocks], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def person_accent_key(speaker_id: str, accents: list[str]) -> str:
    """Stable per-person accent: a pure function of the stable id, never of array order."""
    return accents[hashlib.sha256(speaker_id.encode("utf-8")).digest()[0] % len(accents)]


def eq_heights(seed: int, n: int) -> list[float]:
    """Deterministic 'voice equalizer' silhouette in [0.12, 1]."""
    rnd = random.Random(seed)
    ph = [rnd.uniform(0, 6.28) for _ in range(3)]
    out = []
    for i in range(n):
        t = i / max(1, n - 1)
        env = math.sin(math.pi * t) ** 0.7
        wob = abs(math.sin(ph[0] + t * 13) * 0.55 + math.sin(ph[1] + t * 27) * 0.45)
        h = 0.12 + 0.88 * env * (0.3 + 0.7 * wob) * (0.82 + 0.18 * rnd.random())
        out.append(round(min(1.0, h), 3))
    return out


def hexagon(cx: float, cy: float, r: float) -> list[list[float]]:
    return [[round(cx + r * math.cos(math.radians(a)), 2), round(cy + r * math.sin(math.radians(a)), 2)]
            for a in (-90, -30, 30, 90, 150, 210)]


def diamond(cx: float, cy: float, r: float) -> list[list[float]]:
    return [[cx, cy - r], [cx + r, cy], [cx, cy + r], [cx - r, cy]]


class Builder:
    def __init__(self, view: Mapping, tokens: Mapping, fonts: FontBook):
        self.v, self.t, self.f = view, tokens, fonts
        c = tokens["canvas"]
        self.W = int(c["width"])
        self.mx = int(c["margin_x"])
        self.cw = self.W - 2 * self.mx
        self.hang = float(c.get("hang_em", 1.5))
        self.sp = tokens["space"]
        self.col = tokens["colors"]
        self.ops: list[dict] = []
        self.blocks: list[dict] = []
        self.modules: list[dict] = []
        self.min_font = 1e9
        self.seed = int(hashlib.sha256(str(view.get("headline", "")).encode("utf-8")).hexdigest()[:8], 16)
        self.gutter: dict[int, tuple[float, float]] = {}   # module index -> (icon_top, icon_bottom)

    # ------------------------------------------------------------------ primitives
    def style(self, name: str) -> dict:
        s = self.t["type"][name]
        if s["size"] < self.t["type_floor_px"]:
            raise RenderError("tokens:below_type_floor", name)
        return s

    def measure(self, text: str, style: str, x: float, w: float, align: str = "left") -> TextLayout:
        s = self.style(style)
        return layout_text(self.f, text, x=x, w=w, size=s["size"], bold=s["bold"], lh=s["lh"],
                           align=align, hang_em=self.hang)

    def put(self, role: str, lay: TextLayout, top: float, color: str, *, md: str | None = None) -> float:
        """Register ``lay`` as the next visible block and emit its line ops. Returns bottom y."""
        bid = len(self.blocks)
        self.blocks.append({"id": bid, "role": role, "text": lay.source, "md": md or role})
        self.min_font = min(self.min_font, lay.size)
        for i, line in enumerate(lay.lines):
            base = lay.baseline(top, i)
            self.ops.append({"op": "text", "block": bid, "x": line.x, "base": base, "s": line.text,
                             "size": lay.size, "bold": lay.bold, "color": color,
                             # approximate ink box (CJK glyphs sit ~0.86em above / ~0.2em below the baseline)
                             "bbox": [line.x, base - 0.86 * lay.size, line.x + line.width, base + 0.2 * lay.size]})
        return top + lay.height

    def text(self, role, text, style, x, top, w, color, align="left", md=None) -> float:
        return self.put(role, self.measure(text, style, x, w, align), top, color, md=md)

    def add(self, **op) -> int:
        self.ops.append(op)
        return len(self.ops) - 1

    def hline(self, x0, x1, y, color="hairline", alpha=1.0, w=2, dash=None):
        self.add(op="hline", x0=x0, x1=x1, y=y, color=self.col[color], alpha=alpha, w=w, dash=dash)

    def shape(self, parts, *, fill=None, alpha=0.1, alpha_r=None, outline=None, ow=2, oalpha=0.6):
        """Filled outline shape from rounded rects / polygons (all geometry is template-fixed)."""
        return self.add(op="shape", parts=parts, fill=fill, alpha=alpha, alpha_r=alpha if alpha_r is None else alpha_r,
                        outline=outline, ow=ow, oalpha=oalpha)

    def equalizer(self, y_center, hmax, c0, c1, alpha, seed_shift=0, x0=None, x1=None):
        x0 = self.mx if x0 is None else x0
        x1 = self.W - self.mx if x1 is None else x1
        pitch = 17
        n = int((x1 - x0 + 9) // pitch)
        self.add(op="bars", x0=x0, cy=y_center, hmax=hmax, pitch=pitch, bw=8, c0=c0, c1=c1, alpha=alpha,
                 hs=eq_heights(self.seed + seed_shift, n))

    # ------------------------------------------------------------------ page
    def build(self) -> LayoutResult:
        v, sp, col = self.v, self.sp, self.col
        W, mx, cw = self.W, self.mx, self.cw
        self.add(op="glow", cx=mx + 80, cy=40, r=560, color=col["mint"], alpha=self.t["decor"]["glow_alpha"])
        self.add(op="glow", cx=W - 40, cy=230, r=480, color=col["violet"], alpha=self.t["decor"]["glow_alpha"] * 0.7)
        bg_index = len(self.ops)    # page-edge rings are inserted here once the height is known

        y = self.t["canvas"]["margin_top"]
        # --- masthead: brand row
        mark = 46
        self.add(op="brandmark", x=mx, y=y, size=mark, color=col["mint"])
        bl = self.measure(v["brand"], "brand", mx + mark + 18, self.cw - mark - 18)
        self.put("brand", bl, y + (mark - bl.height) / 2, col["ink"], md="p")
        if v.get("badge"):
            bs = self.style("badge")
            tw = self.f.width(v["badge"], bs["bold"], bs["size"])
            pw, ph = tw + 56, 52
            x1 = W - mx
            top = y + (mark - ph) / 2
            self.shape([("poly", [[x1 - pw + 16, top], [x1, top], [x1 - 16, top + ph], [x1 - pw, top + ph]])],
                       fill=col["violet"], alpha=0.12, outline=col["violet"], ow=2, oalpha=0.75)
            bl2 = self.measure(v["badge"], "badge", x1 - pw, pw, "center")
            self.put("badge", bl2, y + (mark - bl2.height) / 2, col["violet"], md="p")
        y += mark + sp["after_brand"]
        hl = self.measure(v["headline"], "headline", mx, cw)
        y = self.put("headline", hl, y, col["ink"], md="h1")
        y += sp["after_headline"]
        y = self.text("meta", v["meta"], "meta", mx, y, cw, col["muted"], md="p")
        self.header_height = int(y)
        # signature element: a voice equalizer strip (pure decoration, seeded by the headline)
        y += 44
        self.equalizer(y + 40, 80, col["mint"], col["violet"], 0.85)
        y += 80 + 56

        # --- section 1: content
        y = self.section("section_content", v["sections"]["content"], y, col["mint"])
        mods = v["modules"]
        for i, m in enumerate(mods):
            top = y
            y = getattr(self, "module_" + m["kind"])(m, y, i)
            self.modules.append({"kind": m["kind"], "index": i, "top": top, "bottom": y})
            if i < len(mods) - 1:
                a, b = m["kind"], mods[i + 1]["kind"]
                gap_mid = y + sp["module_gap"] / 2
                if a in ("topic", "next_hook") and b in ("topic", "next_hook"):
                    self.hline(mx + sp["gutter"], W - mx, gap_mid, alpha=1.0, dash=[10, 9])
                elif "odd_topic" not in (a, b):
                    self.add(op="wave", x0=W / 2 - 70, x1=W / 2 + 70, y=gap_mid, amp=6, length=46,
                             color=col["mint"], alpha=0.5, w=3)
                y += sp["module_gap"]
        # dotted guide joining consecutive topic icons
        idx = sorted(self.gutter)
        for a, b in zip(idx, idx[1:]):
            if b == a + 1:
                self.add(op="line", pts=[[mx + sp["gutter_icon"] / 2, self.gutter[a][1] + 10],
                                         [mx + sp["gutter_icon"] / 2, self.gutter[b][0] - 10]],
                         color=col["sky"], alpha=0.5, w=3, dash=[2, 10], cap="round")
        y += 64

        # --- chapter break: a second, shorter equalizer in the other accent colours
        self.equalizer(y + 26, 52, col["amber"], col["mint"], 0.6, seed_shift=7)
        y += 52 + 74

        # --- section 2: people
        y = self.section("section_people", v["sections"]["people"], y, col["amber"])
        y = self.people(y)
        y = self.stats(y)

        # --- footer
        if v.get("footer"):
            y += sp["footer_gap"] + 6
            self.add(op="wave", x0=mx, x1=W - mx, y=y, amp=5, length=60, color=col["muted"], alpha=0.35, w=2)
            y += sp["footer_gap"]
            y = self.text("footer", v["footer"], "note", mx, y, cw, col["muted"], md="p")
        height = int(round(y + self.t["canvas"]["margin_bottom"]))
        limit = int(self.t["canvas"]["height_limit"])
        if height > limit:
            raise RenderError("height_exceeded", f"{height}px > {limit}px; nothing was dropped or shrunk",
                              height=height, limit=limit)
        # page-edge rings, behind everything: alternate sides every ~1500 px
        rings = []
        k, yy = 0, 820
        while yy < height - 200:
            cx = -50 if k % 2 == 0 else W + 50
            rings.append({"op": "rings", "cx": cx, "cy": yy, "radii": [210, 280, 350], "color": col["sky"],
                          "alpha": 0.06, "w": 2})
            k, yy = k + 1, yy + 1500
        self.ops[bg_index:bg_index] = rings
        return LayoutResult(W, height, self.ops, self.blocks, self.modules, self.header_height, self.min_font)

    # ------------------------------------------------------------------ pieces
    def section(self, role, title, y, color) -> float:
        mx, W = self.mx, self.W
        lay = self.measure(title, "section", mx + 56, self.cw - 56)
        top = y
        cy = top + lay.lh / 2
        self.add(op="slashes", x=mx, y=cy - 22, h=44, color=color)
        bottom = self.put(role, lay, top, self.t["colors"]["ink"], md="h2")
        if len(lay.lines) == 1:
            x0 = mx + 56 + lay.width + 30
            if x0 < W - mx - 60:
                self.hline(x0, W - mx - 22, cy, alpha=1.0, dash=[14, 8])
                self.add(op="shape", parts=[("poly", diamond(W - mx - 8, cy, 8))], fill=color, alpha=1.0,
                         alpha_r=1.0, outline=None, ow=0, oalpha=0)
        return bottom + self.sp["after_section_heading"]

    def kicker(self, text, x, y, w, color, role="kicker") -> float:
        return self.text(role, text, "kicker", x, y, w, color, md="p") + self.sp["kicker_gap"]

    def module_summary(self, m, y, i):
        y = self.kicker(m["kicker"], self.mx, y, self.cw, self.col["mint"])
        return self.text("summary", m["text"], "lede", self.mx, y, self.cw, self.col["ink"], md="p")

    def module_odd_topic(self, m, y, i):
        sp, col, W, mx = self.sp, self.col, self.W, self.mx
        icon = sp["odd_icon"]
        x = mx + icon + sp["odd_icon_gap"]
        w = W - mx - x
        top = y
        pad, s = sp["odd_pad_y"], 28                  # s = slant of the band (px of rise across the page)
        inner = top + s + pad
        none = m["status"] == "none"
        accent = col["muted"] if none else col["violet"]
        band_idx = self.add(op="shape", parts=[])      # filled below, once the height is known
        cy = inner + icon / 2
        cx = mx + icon / 2
        # radar rings + glow behind the icon
        if not none:
            self.add(op="glow", cx=cx, cy=cy, r=icon * 1.5, color=col["violet"], alpha=0.35)
        for r, a in ((icon * 0.80, 0.55), (icon * 0.98, 0.28)):
            self.add(op="rings", cx=cx, cy=cy, radii=[r], color=accent, alpha=a * 0.6, w=2)
        self.add(op="shape", parts=[("poly", hexagon(cx, cy, icon / 2 + 2))], fill=accent, alpha=0.16,
                 alpha_r=0.16, outline=accent, ow=3, oalpha=0.9)
        self.add(op="icon", x=cx - icon * 0.25, y=cy - icon * 0.25, size=icon * 0.5, cat=m["icon"], color=accent)
        yy = self.kicker(m["kicker"], x, inner, w, accent)
        t = self.measure(m["title"], "odd_title", x, w)
        yy = self.put("odd_title", t, yy, col["ink"], md="h3") + self.sp["title_gap"]
        yy = self.text("odd_text", m["text"], "body", x, yy, w, col["body"], md="p")
        content_bottom = max(yy, inner + icon)
        bottom = content_bottom + pad + s
        a0 = self.t["decor"]["odd_band_alpha"]
        self.ops[band_idx] = {
            "op": "shape", "parts": [("poly", [[0, top + s], [W, top], [W, bottom - s], [0, bottom]])],
            "fill": accent, "alpha": a0 * (0.5 if none else 1.8), "alpha_r": a0 * (0.3 if none else 0.5),
            "outline": None, "ow": 0, "oalpha": 0}
        # slanted edge lines (double line on top, single below) + a dot-grid texture on the right
        self.add(op="line", pts=[[0, top + s], [W, top]], color=accent, alpha=0.75, w=2)
        self.add(op="line", pts=[[0, top + s + 12], [W, top + 12]], color=accent, alpha=0.30, w=2)
        self.add(op="line", pts=[[0, bottom], [W, bottom - s]], color=accent, alpha=0.75, w=2)
        self.add(op="dots", x0=W - 300, y0=top + s + 14, x1=W - 40, y1=bottom - s - 14, step=22, r=2.2,
                 color=accent, alpha=0.09)
        return bottom

    def module_moment(self, m, y, i):
        sp, col, mx, cw = self.sp, self.col, self.mx, self.cw
        y = self.kicker(m["kicker"], mx, y, cw, col["mint"])
        if m.get("title"):
            y = self.text("moment_title", m["title"], "topic_title", mx, y, cw, col["ink"], md="h3") + sp["title_gap"] + 14
        stages = m["stages"]
        if stages:
            n = len(stages)
            cols = min(int(sp["stage_cols"]), n)
            cell = cw / int(sp["stage_cols"])
            x_off = mx + (cw - cell * cols) / 2
            ic = sp["stage_icon"]
            for r0 in range(0, n, cols):
                row = stages[r0:r0 + cols]
                lays = [self.measure(st["label"], "stage_label", x_off + cell * j + 14, cell - 28, "center")
                        for j, st in enumerate(row)]
                label_h = max(l.height for l in lays)
                cy = y + ic / 2
                for j in range(len(row) - 1):
                    a = x_off + cell * (j + 0.5) + ic / 2 + 18
                    b = x_off + cell * (j + 1.5) - ic / 2 - 18
                    self.add(op="line", pts=[[a, cy], [b - 10, cy]], color=col["mint"], alpha=0.55, w=3,
                             dash=[3, 9], cap="round")
                    self.add(op="chevron", x=b, y=cy, size=14, color=col["mint"], alpha=0.8)
                for j, st in enumerate(row):
                    cxm = x_off + cell * (j + 0.5)
                    self.add(op="shape", parts=[("poly", hexagon(cxm, cy, ic / 2 + 4))], fill=col["mint"],
                             alpha=self.t["decor"]["disc_alpha"], alpha_r=self.t["decor"]["disc_alpha"],
                             outline=col["mint"], ow=3, oalpha=0.8)
                    self.add(op="icon", x=cxm - ic * 0.27, y=cy - ic * 0.27, size=ic * 0.54, cat=st["icon"], color=col["mint"])
                    self.put("stage", lays[j], y + ic + sp["stage_icon_gap"] + 6, col["ink"], md="p")
                y += ic + sp["stage_icon_gap"] + 6 + label_h + sp["stage_row_gap"]
        return self.text("moment_text", m["text"], "body", mx, y, cw, col["body"], md="p")

    def _gutter_item(self, m, y, i, accent_key, role_prefix, shape="circle"):
        sp, col, mx = self.sp, self.col, self.mx
        x = mx + sp["gutter"]
        w = self.W - mx - x
        ic = sp["gutter_icon"]
        accent = col[accent_key]
        cx, cy = mx + ic / 2, y + ic / 2
        part = ("poly", diamond(cx, cy, ic / 2 + 5)) if shape == "diamond" else ("poly", hexagon(cx, cy, ic / 2 + 3))
        if shape == "circle":
            self.add(op="disc", cx=cx, cy=cy, r=ic / 2, fill=accent, alpha=self.t["decor"]["disc_alpha"],
                     outline=accent, ow=3, oalpha=0.75)
        else:
            self.add(op="shape", parts=[part], fill=accent, alpha=self.t["decor"]["disc_alpha"],
                     alpha_r=self.t["decor"]["disc_alpha"], outline=accent, ow=3, oalpha=0.8)
        self.add(op="icon", x=mx + ic * 0.25, y=y + ic * 0.25, size=ic * 0.5, cat=m["icon"], color=accent)
        self.gutter[i] = (y, y + ic)
        yy = self.kicker(m["kicker"], x, y, w, accent)
        yy = self.text(role_prefix + "_title", m["title"], "topic_title", x, yy, w, col["ink"], md="h3") + sp["title_gap"]
        yy = self.text(role_prefix + "_text", m["text"], "body", x, yy, w, col["body"], md="p")
        return max(yy, y + ic)

    def module_topic(self, m, y, i):
        keys = self.t["topic_accents"]
        n = sum(1 for k in self.modules if k["kind"] == "topic")
        return self._gutter_item(m, y, i, keys[n % len(keys)], "topic")

    def module_next_hook(self, m, y, i):
        return self._gutter_item(m, y, i, "amber", "hook", shape="diamond")

    def module_flow(self, m, y, i):
        sp, col, mx, cw = self.sp, self.col, self.mx, self.cw
        y = self.text("flow_head", m["heading"], "flow_head", mx, y, cw, col["mint"], md="h3") + sp["title_gap"] + 8
        x = mx + sp["flow_dot_col"]
        w = self.W - mx - x
        dots = []
        for k, it in enumerate(m["items"]):
            t1 = self.measure(it["title"], "flow_title", x, w)
            dots.append(y + t1.lh / 2)
            y = self.put("flow_title", t1, y, col["ink"], md="p")
            y = self.text("flow_text", it["text"], "flow_text", x, y + 4, w, col["muted"], md="p")
            if k < len(m["items"]) - 1:
                y += sp["flow_item_gap"]
        dx = mx + 14
        if len(dots) > 1:
            self.add(op="line", pts=[[dx, dots[0]], [dx, dots[-1]]], color=col["mint"], alpha=0.45, w=3, dash=[2, 9],
                     cap="round")
        for dy in dots:
            self.add(op="shape", parts=[("poly", diamond(dx, dy, 12))], fill=col["background"], alpha=1.0,
                     alpha_r=1.0, outline=col["mint"], ow=3, oalpha=0.95)
            self.add(op="disc", cx=dx, cy=dy, r=4, fill=col["mint"], alpha=1.0)
        return y

    # ------------------------------------------------------------------ people
    def people(self, y) -> float:
        sp, col, mx, W = self.sp, self.col, self.mx, self.W
        groups = self.v["people"]["groups"]
        if not groups:
            return self.text("people_empty", self.v["people"]["empty_text"], "empty", mx, y, self.cw, col["muted"], md="p")
        rail, av = sp["rail"], sp["avatar"]
        bw = self.cw - rail - sp["rail_gap"]          # speech-bubble width
        px, py = 30, 28                                # bubble padding
        w = bw - 2 * px
        accents = self.t["person_accents"]
        for gi, g in enumerate(groups):
            left = gi % 2 == 0                         # people alternate sides: left, right, left ...
            rail_x = mx if left else W - mx - rail
            bx = mx + rail + sp["rail_gap"] if left else mx
            x0 = bx + px
            accent = col[person_accent_key(g["speaker_id"], accents)]
            top = y
            self.add(op="avatar", x=rail_x + (rail - av) / 2, y=top, size=av, speaker_id=g["speaker_id"],
                     accent=accent, side="left" if left else "right")
            nm = self.measure(g["nickname"], "person_name", rail_x + 4, rail - 8, "center")
            name_bottom = self.put("person_name", nm, top + av + sp["name_gap"], col["ink"], md="h3")
            bubble_idx = self.add(op="shape", parts=[])       # sized once the comments are placed
            yy = top + py
            for ci, c in enumerate(g["comments"]):
                t = self.measure(c["title"], "person_title", x0, w)
                yy = self.put("comment_title", t, yy, accent, md="h4") + 8
                yy = self.text("comment_text", c["text"], "body", x0, yy, w, col["body"], md="p")
                if ci < len(g["comments"]) - 1:
                    self.add(op="hline", x0=x0, x1=x0 + w, y=yy + sp["comment_gap"] / 2, color=accent, alpha=0.28,
                             w=2, dash=[8, 8])
                    yy += sp["comment_gap"]
            bh = max(yy + py - top, av)
            tail_y = top + av / 2
            tip, half = 20, 15
            if left:
                tail = [[bx, tail_y - half], [bx - tip, tail_y], [bx, tail_y + half]]
            else:
                tail = [[bx + bw, tail_y - half], [bx + bw + tip, tail_y], [bx + bw, tail_y + half]]
            self.ops[bubble_idx] = {
                "op": "shape", "parts": [("rect", [bx, top, bx + bw, top + bh], 30), ("poly", tail)],
                "fill": accent, "alpha": 0.14 if left else 0.04, "alpha_r": 0.04 if left else 0.14,
                "outline": accent, "ow": 2, "oalpha": 0.38}
            y = max(top + bh, name_bottom)
            if gi < len(groups) - 1:
                y += sp["person_gap"]
        return y

    def stats(self, y) -> float:
        s = self.v.get("stats")
        sp, col, mx, W, cw = self.sp, self.col, self.mx, self.W, self.cw
        if not s:
            return y
        y += sp["section_gap"] * 0.7
        self.hline(mx, W - mx, y - sp["section_gap"] * 0.35, alpha=1.0, dash=[14, 8])
        if s["status"] != "available":
            return self.text("stats_unavailable", s["text"], "note", mx, y, cw, col["muted"], md="p")
        y = self.text("stats_heading", self.v["stats_heading"], "flow_head", mx, y, cw, col["amber"], md="h3") + 18
        if s.get("notice"):
            y = self.text("stats_notice", s["notice"], "kicker", mx, y, cw, col["violet"], md="p") + 8
        if s.get("scope_note"):
            y = self.text("stats_scope", s["scope_note"], "note", mx, y, cw, col["muted"], md="p")
        y += 30
        gap = sp["stat_gap"]
        colw = (cw - gap) / 2
        pad = 30
        iw = colw - 2 * pad - 26
        cards = []
        for k, row in enumerate(s["rows"]):
            x = mx + k * (colw + gap)
            accent = col["mint"] if k == 0 else col["sky"]
            ly = [("stats_label", self.measure(row["label"], "kicker", x + pad, iw, "left"), accent, "h4", 8),
                  ("stats_name", self.measure(row["names"], "stat_name", x + pad, colw - 2 * pad), col["ink"], "p", 6),
                  ("stats_value", self.measure(row["value"], "stat_value", x + pad, colw - 2 * pad), accent, "p", 4),
                  ("stats_detail", self.measure(row["detail"], "note", x + pad, colw - 2 * pad), col["muted"], "p", 6)]
            if row.get("tag"):
                ly.append(("stats_tag", self.measure(row["tag"], "kicker", x + pad, colw - 2 * pad), col["violet"], "p", 0))
            h = 2 * pad + sum(l.height + after for _, l, _, _, after in ly) - ly[-1][4]
            cards.append((x, accent, ly, h))
        hmax = max(c[3] for c in cards)
        for k, (x, accent, ly, h) in enumerate(cards):
            cut = 30
            x1, y1 = x + colw, y + hmax
            if k == 0:      # cut top-right and bottom-left
                poly = [[x, y], [x1 - cut, y], [x1, y + cut], [x1, y1], [x + cut, y1], [x, y1 - cut]]
            else:           # mirrored
                poly = [[x + cut, y], [x1, y], [x1, y1 - cut], [x1 - cut, y1], [x, y1], [x, y + cut]]
            self.shape([("poly", poly)], fill=accent, alpha=0.13 if k == 0 else 0.03, alpha_r=0.03 if k == 0 else 0.13,
                       outline=accent, ow=2, oalpha=0.5)
            self.add(op="brandmark", x=x1 - pad - 38, y=y + pad - 2, size=34, color=accent, alpha=0.5)
            yy = y + pad
            for role, lay, color, md, after in ly:
                yy = self.put(role, lay, yy, color, md=md) + after
        y += hmax
        if s.get("method_note"):
            y = self.text("stats_method", s["method_note"], "note", mx, y + 34, cw, col["muted"], md="p")
        return y


def build_layout(view: Mapping, tokens: Mapping, fonts: FontBook) -> LayoutResult:
    return Builder(view, tokens, fonts).build()
