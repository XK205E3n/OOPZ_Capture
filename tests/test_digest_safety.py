"""Untrusted input, resource safety, hard limits, offline behaviour, schemas, text breaking."""
import copy
import json
import os
import random
import shutil
import socket
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from digest_support import ROOT, base_case, build_stats, generate, load, run, tmpdir

from oopz_capture.digest.render import RenderError, render_digest_card
from oopz_capture.digest.render.icons import CATEGORY_IDS, draw_icon, normalize_category
from oopz_capture.digest.render.markdown import blocks_to_markdown, escape_literal, markdown_to_blocks
from oopz_capture.digest.render.textlayout import FontBook, FontSpec, MissingGlyph, MissingResource, sanitize_text, wrap
from oopz_capture.digest.render.tokens import FONT_DIR, load_tokens, validate_tokens
from oopz_capture.digest.render.view import build_view, check_module_order, load_labels
from oopz_capture.digest.contract import DigestValidationError, validate_content
from oopz_capture.digest.markdown_literal import visible_text_markdown

TOKENS = load_tokens()
FONTS = FontBook(FontSpec.from_tokens(TOKENS, FONT_DIR), TOKENS["canvas"]["scale"])


def render(model, meta, **kw):
    return render_digest_card(model, meta, **kw)


class UntrustedInput(unittest.TestCase):
    EVIL = ["<svg onload=alert(1)>", "![x](http://example.invalid/a.png)", "[a](javascript:1)", "../../etc/passwd",
            "C:\\Windows\\win.ini", "# heading *bold* `code` | table |", "a\u202eb\u200bc\x00d", "x" * 120]

    def test_evil_nicknames_are_text_everywhere(self):
        model, bundle, meta = base_case()
        for evil in self.EVIL:
            m = copy.deepcopy(model)
            for p in m["people"]["profiles"]:
                if p["speaker_id"] == "demo-a":
                    p["nickname"] = evil
            r = render(m, meta)
            shown = sanitize_text(evil)
            self.assertIn(shown, [b["text"] for b in r.blocks])
            parsed = [t for _, t in markdown_to_blocks(r.markdown)]
            self.assertIn(shown, parsed)
            for line in r.markdown.split("\n"):    # no live markup/HTML survives unescaped
                if line.startswith("### ") and shown in line.replace("\\", ""):
                    self.assertNotIn("<", line)
                    self.assertNotRegex(line, r"(?<!\\)[\[\]`*|]")

    def test_control_and_bidi_characters_removed_not_executed(self):
        self.assertEqual(sanitize_text("a\u202eb\u200bc\x00d\n e"), "abcd e")

    def test_markdown_escape_matches_upstream_exporter(self):
        for s in ["普通文字", "a_b*c [x](y) #1. - + ! | ~ = : / @ \\ `", "<b>&amp;</b>", "2000.01.01 · 合成示例"]:
            ours = escape_literal(s) + "\n"
            theirs = visible_text_markdown([s]).decode("utf-8")
            self.assertEqual(ours, theirs)
            self.assertEqual(markdown_to_blocks(ours)[0][1], s)

    def test_icon_category_strings_never_become_resources(self):
        for bad in [None, {}, [], "", False, 7, "unknown", "../../x.svg", "<svg/>", "http://x/y.png", "other\x00", "A" * 200]:
            self.assertEqual(normalize_category(bad), "other")

    def test_unknown_icon_pixel_identical_to_neutral(self):
        def pixels(cat):
            im = Image.new("RGB", (96, 96), "#080E1C")
            draw_icon(ImageDraw.Draw(im, "RGBA"), 10, 10, 72, cat, (123, 225, 199, 255))
            return im.tobytes()
        for bad in [None, "", "nope", "../../x", "<svg>", 12]:
            self.assertEqual(pixels(bad), pixels("other"))
        self.assertEqual(len(CATEGORY_IDS), 13)
        for cid in CATEGORY_IDS:   # every finite category draws something
            self.assertNotEqual(pixels(cid), Image.new("RGB", (96, 96), "#080E1C").tobytes())

    def test_unknown_icon_case_renders_all_content(self):
        with tmpdir() as t:
            m = run("unknown_icon", Path(t) / "o")
            self.assertEqual(m["warnings"], [])
            md = (Path(t) / "o" / "digest.md").read_text(encoding="utf-8")
            self.assertIn("合成陌生话题", md)
            self.assertIn("背包也能当随身货架", md)

    def test_model_cannot_add_fields_or_unknown_people(self):
        model, bundle, _ = base_case()
        bad = copy.deepcopy(model)
        bad["statistics"] = {"winner": "forged"}
        with self.assertRaises(DigestValidationError):
            validate_content(bad, bundle)
        bad = copy.deepcopy(model)
        bad["people"]["profiles"][0]["speaker_id"] = "not-in-roster"
        with self.assertRaises(DigestValidationError):
            validate_content(bad, bundle)
        bad = copy.deepcopy(model)
        bad["people"]["profiles"][0]["avatar"] = "C:/secret.png"
        with self.assertRaises(DigestValidationError):
            validate_content(bad, bundle)

    def test_renderer_itself_rejects_one_id_two_identities(self):
        model, _, meta = base_case()
        bad = copy.deepcopy(model)
        bad["people"]["profiles"][5]["nickname"] = "另一个名字"
        with self.assertRaises(RenderError) as cm:
            render(bad, meta)
        self.assertEqual(cm.exception.code, "view:person_identity_conflict")

    def test_similar_nicknames_for_different_ids_stay_separate(self):
        model, _, meta = base_case()
        m = copy.deepcopy(model)
        m["people"]["profiles"][1]["nickname"] = "演示甲"       # demo-b now looks like demo-a
        r = render(m, meta)
        names = [b["text"] for b in r.blocks if b["role"] == "person_name"]
        self.assertEqual(names.count("演示甲"), 2)
        self.assertEqual(len(names), 5)

    def test_avatar_mapping_is_by_id_and_bad_files_fall_back(self):
        model, _, meta = base_case()
        with tmpdir() as t:
            t = Path(t)
            good = t / "good.png"
            Image.new("RGB", (64, 64), (40, 90, 140)).save(good)
            junk = t / "junk.png"
            junk.write_bytes(b"not an image")
            huge = t / "huge.png"
            Image.new("RGB", (5000, 5000), (1, 2, 3)).save(huge)
            svg = t / "evil.svg"
            svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
            r = render(model, meta, avatars={"demo-a": good, "demo-b": junk, "demo-c": huge, "demo-d": svg,
                                            "demo-e": t / "missing.png", "nobody": good})
        self.assertEqual(sorted(w.split(":")[1] for w in r.warnings), ["demo-b", "demo-c", "demo-d", "demo-e"])
        self.assertEqual(len(r.blocks), len(render(model, meta).blocks))   # text unaffected

    def test_extreme_text_fails_validation_explicitly(self):
        model, bundle, _ = base_case()
        model["content"]["topics"][0]["text"] = "合成测试" * 1000
        with self.assertRaises(DigestValidationError):
            validate_content(model, bundle)


class Limits(unittest.TestCase):
    def test_height_overflow_fails_clearly_and_writes_nothing(self):
        model, bundle, meta = base_case("long_cjk")
        tk = load_tokens(max_height=1500)
        with tmpdir() as t:
            with self.assertRaises(RenderError) as cm:
                generate(model, bundle, meta, build_stats(), Path(t) / "o", tokens=tk)
            self.assertEqual(cm.exception.code, "height_exceeded")
            self.assertGreater(cm.exception.details["height"], 1500)
            self.assertEqual([p for p in (Path(t) / "o").iterdir() if not p.name.startswith(".build-")], [])

    def test_png_size_overflow_fails_clearly(self):
        model, bundle, meta = base_case()
        tk = load_tokens()
        tk["canvas"]["png_byte_limit"] = 20_000
        with self.assertRaises(RenderError) as cm:
            render(model, meta, tokens=tk)
        self.assertEqual(cm.exception.code, "png_too_large")

    def test_failed_run_keeps_previous_output_untouched(self):
        model, bundle, meta = base_case()
        with tmpdir() as t:
            out = Path(t) / "o"
            generate(model, bundle, meta, build_stats(), out)
            before = {n: (out / n).read_bytes() for n in ("digest.png", "digest.md")}
            with self.assertRaises(RenderError):
                generate(model, bundle, meta, build_stats(), out, tokens=load_tokens(max_height=800))
            self.assertEqual(before, {n: (out / n).read_bytes() for n in ("digest.png", "digest.md")})

    def test_limits_at_exact_boundary(self):
        model, _, meta = base_case("sparse")
        r = render(model, meta)
        tk = load_tokens()
        tk["canvas"]["height_limit"] = r.height              # exactly the limit: allowed
        tk["canvas"]["png_byte_limit"] = r.byte_count
        self.assertEqual(render(model, meta, tokens=tk).height, r.height)
        tk["canvas"]["height_limit"] = r.height - 1          # one pixel over: refused
        with self.assertRaises(RenderError):
            render(model, meta, tokens=tk)

    def test_token_floor_is_enforced(self):
        tk = copy.deepcopy(TOKENS)
        tk["type"]["body"]["size"] = 24
        with self.assertRaises(RenderError):
            validate_tokens(tk)
        tk = copy.deepcopy(TOKENS)
        tk["canvas"]["height_limit"] = 12001
        with self.assertRaises(RenderError):
            validate_tokens(tk)

    def test_all_token_sizes_at_least_28(self):
        self.assertTrue(all(s["size"] >= 28 for s in TOKENS["type"].values()))

    def test_capacity_is_about_twice_the_recommended_length(self):
        """~1700 narrative characters (top of the editorial guidance) fit with ~30% to spare; ~4200 trip the limit explicitly."""
        model, _, meta = base_case()

        def scaled(k):
            m = copy.deepcopy(model)
            for p in m["people"]["profiles"]:
                p["text"] *= k
            for t in m["content"]["topics"]:
                t["text"] *= k
            m["content"]["summary"]["text"] *= k
            return m
        ok = render(scaled(4), meta)           # ~1670 characters of body text
        self.assertLess(ok.height, 9000)
        self.assertLess(ok.byte_count, 5_000_000)
        with self.assertRaises(RenderError) as cm:
            render(scaled(10), meta)           # ~4180 characters
        self.assertEqual(cm.exception.code, "height_exceeded")


class ResourcesAndOffline(unittest.TestCase):
    def test_missing_font_is_a_clear_error(self):
        model, _, meta = base_case("sparse")
        with self.assertRaises(MissingResource):
            render(model, meta, regular_font=ROOT / "nope.otf")

    def _with_nick(self, nick):
        model, _, meta = base_case()
        m = copy.deepcopy(model)
        for p in m["people"]["profiles"]:
            if p["speaker_id"] == "demo-a":
                p["nickname"] = nick
        return m, meta

    def test_emoji_symbols_kana_hangul_render_with_bundled_fallback_fonts(self):
        nick = "玩家" + chr(0x1F680) + chr(0x1F600) + chr(0x2764) + chr(0x2605) + chr(0x266A) + "한あ"
        r = render(*self._with_nick(nick))
        self.assertIn(nick, [b["text"] for b in r.blocks])
        self.assertEqual(r.warnings, [])          # nothing was substituted: every character has a real glyph

    def test_variation_selectors_and_joiners_are_dropped_not_boxed(self):
        r = render(*self._with_nick("A" + chr(0x2764) + chr(0xFE0F) + chr(0x200D) + "B"))
        self.assertIn("A" + chr(0x2764) + "B", [b["text"] for b in r.blocks])

    def test_undrawable_character_degrades_to_replacement_mark_in_both_outputs(self):
        nick = "乐" + chr(0x1D11E) + "谱"          # musical clef: in none of the bundled fonts
        r = render(*self._with_nick(nick))
        shown = "乐" + chr(0xFFFD) + "谱"
        self.assertIn(shown, [b["text"] for b in r.blocks])
        self.assertIn(shown, [t for _, t in markdown_to_blocks(r.markdown)])
        self.assertEqual(r.warnings, ["glyph_substituted:U+1D11E"])

    def test_strict_mode_still_fails_explicitly(self):
        tk = load_tokens()
        tk["fonts"]["missing_glyph"] = "fail"
        with self.assertRaises(MissingGlyph) as cm:
            render(*self._with_nick("乐" + chr(0x1D11E)), tokens=tk)
        self.assertEqual(cm.exception.details["codepoint"], 0x1D11E)


    def test_mixed_scripts_numbers_and_punctuation_render(self):
        model, _, meta = base_case()
        m = copy.deepcopy(model)
        for p in m["people"]["profiles"]:
            if p["speaker_id"] == "demo-a":
                p["nickname"] = "阿Ming_01·小Ａ（2号）"
        r = render(m, meta)
        self.assertIn("阿Ming_01·小Ａ（2号）", [b["text"] for b in r.blocks])

    def test_rendering_and_delivery_need_no_network(self):
        def boom(*a, **k):
            raise AssertionError("network access attempted")
        with mock.patch.object(socket, "socket", boom), mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(socket, "getaddrinfo", boom):
            with tmpdir() as t:
                m = run("normal", Path(t) / "o")
        self.assertEqual(m["case"], "normal")

    def test_source_contains_no_network_or_provider_code(self):
        import re
        pat = re.compile(r"(requests|urllib|http\.client|socket|aiohttp|httpx|openai|anthropic|subprocess)")
        import oopz_capture.digest.render as render_pkg
        files = list(Path(render_pkg.__file__).parent.glob("*.py"))
        for p in files:
            code = re.sub(r"#.*", "", re.sub(r'"""[\s\S]*?"""', "", p.read_text(encoding="utf-8")))
            self.assertEqual(pat.findall(code), [], p.name)


class ModuleOrder(unittest.TestCase):
    def test_check_module_order(self):
        base = {"modules": [{"kind": "summary"}, {"kind": "odd_topic"}, {"kind": "topic"}]}
        check_module_order(base)
        check_module_order({"modules": [{"kind": "summary"}, {"kind": "topic"}, {"kind": "odd_topic"}]})
        check_module_order({"modules": [{"kind": "odd_topic"}, {"kind": "topic"}]})     # the poster has no overview
        for bad in ([{"kind": "summary"}, {"kind": "topic"}, {"kind": "topic"}, {"kind": "odd_topic"}],
                    [{"kind": "summary"}, {"kind": "topic"}]):
            with self.assertRaises(RenderError):
                check_module_order({"modules": bad})

    def test_odd_topic_second_with_many_modules(self):
        model, _, meta = base_case("long_cjk")
        m = copy.deepcopy(model)
        m["content"]["topics"] = m["content"]["topics"] * 1 + copy.deepcopy(m["content"]["topics"])
        view = build_view(m, meta)
        kinds = [x["kind"] for x in view["modules"]]
        self.assertEqual(kinds[1], "odd_topic")
        self.assertGreaterEqual(len(kinds), 6)

    def test_missing_odd_topic_still_gets_an_honest_module(self):
        model, _, meta = base_case()
        m = copy.deepcopy(model)
        del m["content"]["odd_topic"]
        view = build_view(m, meta)
        self.assertEqual(view["modules"][1]["kind"], "odd_topic")
        self.assertEqual(view["modules"][1]["status"], "none")


class TextBreaking(unittest.TestCase):
    def lines(self, text, w=300, size=32, bold=False):
        return wrap(FONTS, text, bold, size, w, 1.5)

    def test_nothing_is_ever_lost(self):
        rnd = random.Random(7)
        alphabet = "你好世界。，！？（）《》“”abcXYZ_-.012 3456789——……的了是"
        for _ in range(300):
            s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 160))).strip() or "x"
            s = sanitize_text(s)
            got = self.lines(s, w=rnd.choice([160, 260, 420]))
            self.assertEqual("".join("".join(got).split()), "".join(s.split()), s)

    def test_lines_fit_width_except_hanging_punctuation(self):
        s = "这是一段很长的中文，里面混合了English words、123456 数字以及标点符号！还有——破折号……和“引号”。" * 3
        for w in (260, 400, 700):
            for line in self.lines(s, w=w):
                body = line.rstrip("，。、；：！？）》」』】”’")
                self.assertLessEqual(FONTS.width(body, False, 32), w + 0.5, line)
                self.assertLessEqual(FONTS.width(line, False, 32), w + 1.5 * 32 + 0.5, line)

    def test_closing_punctuation_never_starts_a_line_and_openers_never_end_one(self):
        s = "我们今天先聊点餐，再排查麦克风音量；接着比较装备（速度和耐久），最后进入协作闯关。" * 4
        for w in range(200, 520, 23):
            ls = self.lines(s, w=w)
            for line in ls:
                self.assertNotIn(line[0], "，。、；：！？）》」』】”’", (w, line))
                self.assertNotIn(line[-1], "（《「『【“‘", (w, line))

    def test_long_ascii_id_is_split_not_cut(self):
        ident = "Synthetic_Player_With_A_Very_Long_Identifier_ABCDEFGHIJKLMNOPQRSTUVWXYZ_1234567890"
        ls = self.lines(ident, w=200, size=28, bold=True)
        self.assertGreater(len(ls), 3)
        self.assertEqual("".join(ls), ident)
        self.assertTrue(all(FONTS.width(l, True, 28) <= 200 for l in ls))

    def test_no_one_or_two_character_last_line(self):
        for w in range(240, 420, 7):
            ls = self.lines("把整理虚拟道具讲成了给随身货架补货这个比方提供了另一种理解游戏准备工作的方式", w=w)
            if len(ls) > 1:
                self.assertGreaterEqual(len(ls[-1].rstrip("。")), 3, (w, ls))


if __name__ == "__main__":
    unittest.main()
