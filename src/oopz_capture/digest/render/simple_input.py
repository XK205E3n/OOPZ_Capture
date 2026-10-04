"""Fill-in-the-blanks input: plain text fields in, a finished digest out.

For people (or a very small script) who just want to type the words. No evidence
ids, no anchors, no analysis: you provide the text, the template does the rest -
layout, colours, icons (picked by a fixed keyword table unless you name one),
the equalizer silhouette, the left/right alternation, the Markdown twin.

This is a *different front door* to the same renderer. It does not replace the
model contract ``oopz.digest.content.v2`` (which keeps its evidence checks for
model output); it simply builds the same structure from human-written text.
"""
from __future__ import annotations

from typing import Any, Mapping

from .icons import CATEGORY_IDS


class FillInError(ValueError):
    """A readable (Chinese) message about what to fix in the fill-in file."""


# Fixed, deterministic keyword -> icon table. First match wins; no match -> neutral "other".
_ICON_KEYWORDS = (
    ("food_seafood", ("海鲜", "螃蟹", "龙虾", "小龙虾", "鱼", "虾")),
    ("food", ("点餐", "吃", "饭", "披萨", "外卖", "火锅", "烧烤", "奶茶", "咖啡", "夜宵", "菜")),
    ("voice", ("麦克风", "麦", "语音", "声音", "音量", "耳机", "连麦", "电流", "回声")),
    ("gaming", ("游戏", "闯关", "开黑", "副本", "组队", "装备", "段位", "关卡", "手柄", "匹配", "赛季", "上分")),
    ("ai", ("AI", "ai", "模型", "智能", "机器人", "算法", "代码", "编程", "程序")),
    ("music", ("音乐", "歌", "唱", "演唱会", "乐队", "耳虫")),
    ("media", ("电影", "视频", "直播", "剧", "番", "动漫", "综艺", "看片")),
    ("shopping", ("买", "购物", "下单", "快递", "折扣", "优惠", "价格", "剁手")),
    ("travel", ("旅行", "旅游", "出差", "机票", "高铁", "路线", "城市", "出发", "自驾")),
    ("planning", ("计划", "安排", "时间", "周末", "约", "日程", "改期", "预定")),
    ("work_learning", ("工作", "学习", "考试", "论文", "项目", "加班", "上班", "课", "作业")),
    ("conversation", ("聊", "讨论", "吐槽", "八卦", "话题")),
)


def icon_for(text: str) -> str:
    for cat, words in _ICON_KEYWORDS:
        if any(w in text for w in words):
            return cat
    return "other"


def _req(d: Mapping, key: str, where: str) -> str:
    v = d.get(key) if isinstance(d, Mapping) else None
    if not isinstance(v, str) or not v.strip():
        raise FillInError(f"请填写「{where}」（字段 {key}）。")
    return v


def _icon(item: Mapping, text: str) -> str:
    given = item.get("icon")
    if isinstance(given, str) and given in CATEGORY_IDS:
        return given
    return icon_for(text)


def _items(data: Mapping, key: str, label: str, limit: int) -> list:
    v = data.get(key) or []
    if not isinstance(v, list):
        raise FillInError(f"「{label}」应该是一个列表（字段 {key}）。")
    if len(v) > limit:
        raise FillInError(f"「{label}」最多 {limit} 条，现在有 {len(v)} 条。")
    return v


def from_fill_in(data: Mapping[str, Any]) -> tuple[dict, dict]:
    """Return ``(content, metadata)`` ready for ``generate(content, None, metadata, None, out)``."""
    if not isinstance(data, Mapping):
        raise FillInError("填写文件的最外层应该是一个 JSON 对象。")
    title = _req(data, "title", "本场标题")
    summary = _req(data, "summary", "总览")
    date, time_ = _req(data, "date", "日期"), _req(data, "time", "时间")

    odd_in = data.get("odd")
    if odd_in:
        ot, ox = _req(odd_in, "title", "最离奇话题的标题"), _req(odd_in, "text", "最离奇话题的说明")
        odd = {"status": "supported", "title": ot, "text": ox, "icon_category": _icon(odd_in, ot + ox),
               "evidence_ids": [], "anchor": "", "participant_ids": []}
    else:
        odd = {"status": "none", "title": "没有明显候选", "text": "本次可用记录中，没有可确认的明显离奇话题或概念。",
               "evidence_ids": [], "anchor": "", "participant_ids": []}

    topics, topic_labels = [], []
    for i, t in enumerate(_items(data, "topics", "话题", 12)):
        tt, tx = _req(t, "title", f"第 {i + 1} 个话题的标题"), _req(t, "text", f"第 {i + 1} 个话题的内容")
        topics.append({"title": tt, "text": tx, "icon_category": _icon(t, tt + tx)})
        topic_labels.append(t.get("label") or "话题摘要")

    moments, moment_labels = [], []
    for i, m in enumerate(_items(data, "moments", "转场", 8)):
        steps = m.get("steps")
        if not isinstance(steps, list) or not steps:
            raise FillInError(f"第 {i + 1} 个转场需要 steps（例如 [\"点餐\", \"麦克风\", \"闯关\"]）。")
        stages = []
        for s in steps[:12]:
            label = s if isinstance(s, str) else (s or {}).get("label")
            if not isinstance(label, str) or not label.strip():
                raise FillInError(f"第 {i + 1} 个转场的 steps 里有空的步骤。")
            stages.append({"label": label, "icon_category": _icon(s if isinstance(s, Mapping) else {}, label)})
        moments.append({"title": " → ".join(st["label"] for st in stages),
                        "text": _req(m, "text", f"第 {i + 1} 个转场的说明"), "stages": stages})
        moment_labels.append(m.get("label") or "话题片段")

    hooks = []
    for i, h in enumerate(_items(data, "hooks", "还没聊完的线索", 12)):
        ht, hx = _req(h, "title", f"第 {i + 1} 条线索的标题"), _req(h, "text", f"第 {i + 1} 条线索的内容")
        hooks.append({"title": ht, "text": hx, "icon_category": _icon(h, ht + hx)})

    profiles, seen = [], {}
    for i, p in enumerate(_items(data, "people", "人物", 12)):
        name = _req(p, "name", f"第 {i + 1} 位人物的昵称或 ID")
        sid = p.get("id") if isinstance(p.get("id"), str) and p.get("id").strip() else name
        if sid in seen and seen[sid] != name:
            raise FillInError(f"人物 id「{sid}」同时对应了两个名字（{seen[sid]} / {name}）。")
        seen[sid] = name
        comments = p.get("comments")
        if not isinstance(comments, list) or not comments:
            raise FillInError(f"「{name}」至少需要一条点评（comments）。")
        for j, c in enumerate(comments):
            profiles.append({"speaker_id": sid, "nickname": name,
                             "title": _req(c, "title", f"「{name}」第 {j + 1} 条点评的标题"),
                             "text": _req(c, "text", f"「{name}」第 {j + 1} 条点评的内容")})

    content = {"content": {"summary": {"title": title, "text": summary}, "odd_topic": odd, "topics": topics,
                           "moments": moments, "next_hooks": hooks},
               "people": {"profiles": profiles}}
    metadata: dict[str, Any] = {"synthetic": bool(data.get("demo", False)),
                                "session": {"date_label": date, "time_label": time_},
                                "topic_labels": topic_labels, "moment_labels": moment_labels,
                                "hide_stats_when_unavailable": True}
    if data.get("summary_label"):
        metadata["summary_label"] = data["summary_label"]
    if data.get("timeline"):
        metadata["timeline"] = [{"title": _req(t, "title", "时间线标题"), "text": _req(t, "text", "时间线内容")}
                                for t in _items(data, "timeline", "时间线", 12)]
    if data.get("footer"):
        metadata["source_note"] = data["footer"]
    return content, metadata
