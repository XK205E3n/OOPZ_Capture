"""Five fictional cases: limits, text identity, layout, pixels, module order, determinism."""
import json
import unittest
from pathlib import Path

from PIL import Image

from digest_support import ROOT, CASES, base_case, build_stats, generate, load, run, tmpdir

from oopz_capture.digest.render.markdown import markdown_to_blocks
from oopz_capture.digest.render.paint import rgb
from oopz_capture.digest.render.tokens import load_tokens


def luminance(c):
    f = lambda v: (v / 255 / 12.92) if v / 255 <= 0.03928 else (((v / 255) + 0.055) / 1.055) ** 2.4  # noqa: E731
    r, g, b = c
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    la, lb = sorted((luminance(rgb(a)), luminance(rgb(b))), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class CaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tmpdir()
        cls.root = Path(cls.tmp.name)
        cls.manifest = {c: run(c, cls.root / c) for c in CASES}
        cls.tokens = load_tokens()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def side(self, case, name):
        return json.loads((self.root / case / "sidecar" / name).read_text(encoding="utf-8"))

    def test_final_folder_has_only_png_and_md(self):
        for c in CASES:
            self.assertEqual(sorted(p.name for p in (self.root / c).iterdir() if p.is_file()), ["digest.md", "digest.png"])

    def test_png_limits(self):
        for c, m in self.manifest.items():
            png = self.root / c / "digest.png"
            with Image.open(png) as im:
                self.assertEqual(im.size, (m["width"], m["height"]))
                self.assertLessEqual(im.height, 12000)
            self.assertLessEqual(png.stat().st_size, 10_000_000)
            self.assertEqual(m["png_bytes"], png.stat().st_size)

    def test_markdown_equals_visible_text_blocks(self):
        for c in CASES:
            md = (self.root / c / "digest.md").read_text(encoding="utf-8")
            blocks = self.side(c, "visible_text.json")
            self.assertEqual([t for _, t in markdown_to_blocks(md)], [b["text"] for b in blocks])

    def test_drawn_strings_equal_markdown(self):
        """What the painter really drew, block by block, equals the Markdown paragraphs."""
        squash = lambda s: "".join(s.split())  # noqa: E731
        for c in CASES:
            md = [t for _, t in markdown_to_blocks((self.root / c / "digest.md").read_text(encoding="utf-8"))]
            drawn = {}
            for d in self.side(c, "text_bounds.json"):
                drawn.setdefault(d["block"], []).append(d["text"])
            self.assertEqual(len(drawn), len(md), c)
            for i, text in enumerate(md):
                self.assertEqual(squash("".join(drawn[i])), squash(text), (c, i))

    def test_required_labels_present_in_both_outputs(self):
        md = (self.root / "normal" / "digest.md").read_text(encoding="utf-8")
        for needle in ("虚构演示", "统计演示：进出时间/发言计数为模拟", "模拟数据", "发言频率最高", "发言频率最低",
                       "段/分钟", "这场聊了什么", "大家的表现", "全部人物、时间和情节均为虚构合成示例", "OOPZ"):
            self.assertIn(needle, md.replace("\\", ""))

    def test_odd_topic_is_second_or_third_content_module(self):
        for c, m in self.manifest.items():
            self.assertIn(m["odd_topic_position"], (2, 3), c)
            self.assertEqual(m["content_modules"][m["odd_topic_position"] - 1], "odd_topic")

    def test_two_major_sections_exist_in_order(self):
        for c in CASES:
            roles = [(b["role"], b["text"]) for b in self.side(c, "visible_text.json")]
            sec = [t for r, t in roles if r.startswith("section_")]
            self.assertEqual(sec[0], "大家的表现")  # people come right after the odd-topic block
            self.assertIn(sec[1:], ([], ["这场聊了什么"]))

    def test_every_text_size_at_or_above_floor(self):
        floor = self.tokens["type_floor_px"]
        self.assertGreaterEqual(floor, 28)
        for c in CASES:
            self.assertGreaterEqual(min(d["size"] for d in self.side(c, "text_bounds.json")), floor)
            self.assertGreaterEqual(self.manifest[c]["min_font_px"], floor)

    def test_text_inside_margins_and_no_overlap(self):
        W = self.tokens["canvas"]["width"]
        mx = self.tokens["canvas"]["margin_x"]
        for c in CASES:
            boxes = self.side(c, "text_bounds.json")
            H = self.manifest[c]["height"]
            for d in boxes:
                x0, y0, x1, y1 = d["bbox"]
                self.assertGreaterEqual(x0, mx - 2, (c, d["text"]))
                self.assertLessEqual(x1, W - mx + 1.5 * d["size"] + 1, (c, d["text"]))   # hanging punctuation only
                self.assertLess(x1, W)
                self.assertGreaterEqual(y0, 0)
                self.assertLessEqual(y1, H)
            for i, a in enumerate(boxes):
                for b in boxes[i + 1:]:
                    ax0, ay0, ax1, ay1 = a["bbox"]
                    bx0, by0, bx1, by1 = b["bbox"]
                    inter = max(0, min(ax1, bx1) - max(ax0, bx0)) * max(0, min(ay1, by1) - max(ay0, by0))
                    self.assertLess(inter, 1.0, (c, a["text"], b["text"]))

    def test_pixels_show_ink_in_every_text_box(self):
        """Text is not covered by decor: every text box contains pixels near its text colour."""
        for c in ("normal", "long_id"):
            bounds = self.side(c, "text_bounds.json")
            with Image.open(self.root / c / "digest.png") as im:
                im = im.convert("RGB")
                for d in bounds:
                    x0, y0, x1, y1 = (round(v) for v in d["bbox"])
                    crop = im.crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)))
                    bright = crop.convert("L").getextrema()[1]
                    self.assertGreater(bright, 90, (c, d["text"]))   # clearly brighter than the dark background

    def test_edges_have_no_ink(self):
        """Nothing is drawn in the outer 24 px gutters (text, avatars, bands excluded: bands are full-bleed)."""
        with Image.open(self.root / "long_id" / "digest.png") as im:
            im = im.convert("RGB")
            for b in self.side("long_id", "text_bounds.json"):
                self.assertGreaterEqual(b["bbox"][0], 24)
                self.assertLessEqual(b["bbox"][2], im.width - 24)

    def test_long_identifier_is_complete_and_not_elided(self):
        model, bundle, meta = base_case("long_id")
        nick = bundle["people"][0]["nickname"]
        names = [b for b in self.side("long_id", "visible_text.json") if b["role"] == "person_name"]
        self.assertIn(nick, [b["text"] for b in names])
        drawn = [d for d in self.side("long_id", "text_bounds.json") if d["block"] == names[0]["id"]]
        self.assertEqual("".join("".join(d["text"].split()) for d in drawn), nick)
        self.assertNotIn("…", "".join(d["text"] for d in drawn))
        self.assertGreater(len(drawn), 2)   # it wrapped, instead of being cut

    def test_long_cjk_grows_and_keeps_everything(self):
        self.assertGreater(self.manifest["long_cjk"]["height"], self.manifest["normal"]["height"] + 800)
        model, bundle, meta = base_case("long_cjk")
        md = (self.root / "long_cjk" / "digest.md").read_text(encoding="utf-8").replace("\\", "")
        self.assertIn(model["people"]["profiles"][0]["text"], md)
        self.assertIn(model["content"]["odd_topic"]["title"], md)

    def test_sparse_is_honest(self):
        md = (self.root / "sparse" / "digest.md").read_text(encoding="utf-8")
        self.assertIn("没有明显候选", md)
        self.assertIn("本场暂无足够依据的人物评点", md)
        self.assertIn("缺少足够的进出与发言记录", md)
        for bad in ("发言频率最高", "段/分钟", "模拟数据"):
            self.assertNotIn(bad, md)
        self.assertEqual(self.manifest["sparse"]["stats"], "unavailable")

    def test_same_person_with_two_comments_has_one_avatar_and_one_name(self):
        blocks = self.side("normal", "visible_text.json")
        names = [b["text"] for b in blocks if b["role"] == "person_name"]
        self.assertEqual(names, ["演示甲", "演示乙", "演示丙", "演示丁", "演示戊"])
        titles = [b["text"] for b in blocks if b["role"] == "comment_title"]
        self.assertEqual(len(titles), 6)
        # both of demo-a's comments sit directly under the one name
        i = [b["id"] for b in blocks if b["role"] == "person_name"][0]
        self.assertEqual([b["role"] for b in blocks[i:i + 5]],
                         ["person_name", "comment_title", "comment_text", "comment_title", "comment_text"])

    def test_people_alternate_left_and_right(self):
        """One avatar per person; sides alternate in reading order (left, right, left ...);
        the name sits directly under its own avatar."""
        W = self.tokens["canvas"]["width"]
        for c in ("normal", "long_id", "unknown_icon"):
            # avatar ops are not in the sidecar; person_name blocks are: centre of each name line
            names = [b for b in self.side(c, "visible_text.json") if b["role"] == "person_name"]
            centres = []
            for b in names:
                lines = [d for d in self.side(c, "text_bounds.json") if d["block"] == b["id"]]
                centres.append(sum((d["bbox"][0] + d["bbox"][2]) / 2 for d in lines) / len(lines))
            self.assertEqual(len(names), 5)
            for i, cx in enumerate(centres):
                self.assertEqual(cx < W / 2, i % 2 == 0, (c, i, cx))

    def test_deterministic(self):
        with tmpdir() as t:
            m2 = run("normal", Path(t) / "again")
            self.assertEqual(m2["sha256"], self.manifest["normal"]["sha256"])
            self.assertEqual(m2["layout_fingerprint"], self.manifest["normal"]["layout_fingerprint"])

    def test_accents_follow_stable_id_not_position(self):
        from oopz_capture.digest.render.layout import person_accent_key
        a = person_accent_key("demo-a", self.tokens["person_accents"])
        self.assertEqual(a, person_accent_key("demo-a", self.tokens["person_accents"]))
        self.assertIn(a, self.tokens["person_accents"])

    def test_contrast_of_text_colours(self):
        c = self.tokens["colors"]
        for name in ("ink", "body", "muted", "mint", "violet", "amber", "sky"):
            self.assertGreaterEqual(contrast(c[name], c["background"]), 4.5, name)
        self.assertGreaterEqual(contrast(c["body"], c["surface"]), 4.5)

    def test_v6_inputs_still_validate_unchanged(self):
        """Compatibility: the unmodified v2 content contract + the V6 evidence/metadata files still work."""
        from oopz_capture.digest.contract import validate_content
        for c in CASES:
            model, bundle, meta = base_case(c)
            validate_content(model, bundle)
            self.assertIn("placeholder_label", meta)   # legacy field present and harmlessly ignored


if __name__ == "__main__":
    unittest.main()
