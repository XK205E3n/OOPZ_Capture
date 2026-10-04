"""System prompts.  The editorial rules are the V7 NARRATIVE_RULES verbatim; the rest only adds the
mode (one window / many windows) and the formatting limits the validator enforces."""
from __future__ import annotations

from pathlib import Path

RULES = Path(__file__).with_name("narrative_rules.txt").read_text(encoding="utf-8").strip()

FORMAT_GUARD = """【格式约束（程序会拒绝违反者，请严格遵守）】
- 只输出一个JSON对象，不要代码围栏，不要任何解释。
- title、text 里不要出现任何引号（包括「」“”‘’）、尖括号、网址，不要用 Markdown。
- 不要出现这些词：最（“最后、最近、最初、最终”除外）、唯一、全员、所有人、冠军、第一名、百分之。
- 不要写任何数字、日期、时间、次数，除非该数字原样出现在你所引用的 evidence 文本里。
- 人物条目的 evidence_ids 只能是该人自己的发言（speaker_id 相同的 asr_excerpt），合计至少16个字。
- odd_topic 的 title、text 里不要出现任何人的昵称或ID。
- anchor 必须是你所引用的某一条 evidence 的 text 里原样连续的4到80个字，逐字复制，不要改标点或空格。
- speaker_id 只能用输入 people 里给出的值，nickname 必须与之完全一致。
- people.profiles 的每一项不要写 icon_category（只有 topics、moments 及其 stages 可以选填）。"""

WINDOW_MODE = """【窗口模式】这不是整场回顾，只是整场录音中的一个时间窗口（输入里的 window 是第几段，of 是共几段）。输入 evidence 是该窗口内的全部发言片段，按时间排序，没有遗漏，kind 全部是 asr_excerpt。
- 按上面的结构输出同样的JSON：summary 是本窗口的概述（80到160字）；moments 和 next_hooks 固定为空数组；topics 最多3项；odd_topic 可以是 none；profiles 最多5条，只写本窗口内有具体表现的人。
- 本窗口发言很少或内容琐碎时，topics、profiles 可以为空，summary 照实写一句，不要凑内容。
- 所有 evidence_ids 只能是本窗口 evidence 里的 id。"""

SECTION_MODE = """【合并模式】输入 windows 是同一场录音中相邻几个时间段的分析结果（每条都已由程序核对过证据），evidence 是它们引用的原始发言片段（kind 为 asr_excerpt）和各段概述（kind 为 window_summary）。
- 把它们合并成一份同样结构的JSON，代表这几个时间段合起来的内容：summary 为合并后的概述（100到220字）；moments 和 next_hooks 固定为空数组；topics 最多3项；profiles 最多5条。
- 同一件事跨段出现时合并，不要逐段罗列。
- evidence_ids 和 anchor 只能取自输入 evidence。summary 与 topics 可以引用 window_summary；人物条目只能引用该人自己的 asr_excerpt。"""

FINAL_MODE = """【汇总模式】输入 windows 是同一场录音按时间顺序的分段分析结果（每条都已由程序核对过证据），evidence 是它们引用的原始发言片段（kind 为 asr_excerpt）和各段概述（kind 为 window_summary），coverage 说明哪些时间没有分析到。请整合成整场回顾，输出完整的 content/people 结构。
- 从各段候选中挑选最有信息量且不重复的内容，整场叙事连贯；同一件事跨段出现时合并，不要逐段罗列；同一个人在不同段的不同表现可以分别成条，但不要对同一行为换标题重复点评。
- 标题和总览要覆盖整场的主要走向，而不是只写某一段。
- evidence_ids 和 anchor 只能取自输入 evidence。summary、topics、moments、next_hooks、odd_topic 可以引用 window_summary；人物条目只能引用该人自己的 asr_excerpt。
- coverage 里有缺失的时间段时，总览措辞要谨慎，不要把缺失的时间当作什么都没发生。"""

MODES = {"window": WINDOW_MODE, "section": SECTION_MODE, "final": FINAL_MODE}


def system_prompt(mode: str) -> str:
    return "\n\n".join((RULES, MODES[mode], FORMAT_GUARD))
