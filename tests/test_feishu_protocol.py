from oopz_capture.feishu_protocol import display_intent, normalize_intent, synthetic_controller_id


def test_natural_language_intents_are_bounded() -> None:
    assert normalize_intent("开始录音") == "/oopz 开始"
    assert normalize_intent("开始录音 1小时") == "/oopz 开始 1h"
    assert normalize_intent("开始录音 45 分钟") == "/oopz 开始 45m"
    assert normalize_intent("帮我录音 45 分钟") is None
    assert normalize_intent("现在情况怎么样") is None
    assert normalize_intent("停止") == "/oopz 离开"
    assert normalize_intent("@OOPZ 管理机器人 停止") == "/oopz 离开"
    assert normalize_intent("删除所有文件") is None
    assert normalize_intent("最近报告") == "/oopz 最近报告"
    assert normalize_intent("详细报告") == "/oopz 详细报告"
    assert normalize_intent("待分析") == "/oopz 待分析"
    assert normalize_intent("删除会话 2026-08-22_10-51-32_BJT") == "/oopz 删除会话 2026-08-22_10-51-32_BJT"
    assert normalize_intent("/oopz 设置 OOPZ_DEVICE=cuda:0") == "/oopz 设置 OOPZ_DEVICE=cuda:0"
    assert normalize_intent("设置状态") == "/oopz 设置状态"
    assert normalize_intent("/oopz 增加管理员 123456") is None
    assert normalize_intent("/oopz 开始 45m") == "/oopz 开始 45m"
    assert normalize_intent("开始分析") == "是"


def test_every_advertised_feishu_command_has_a_mapping() -> None:
    expected = {
        "帮助": "/oopz 帮助",
        "开始录音": "/oopz 开始",
        "状态": "/oopz 状态",
        "停止": "/oopz 离开",
        "待分析": "/oopz 待分析",
        "最近报告": "/oopz 最近报告",
        "详细报告": "/oopz 详细报告",
        "删除会话": "/oopz 删除会话",
        "设置状态": "/oopz 设置状态",
        "设置 OOPZ_LANGUAGE=zh": "/oopz 设置 OOPZ_LANGUAGE=zh",
    }
    assert {text: normalize_intent(text) for text in expected} == expected


def test_display_intent_hides_internal_controller_prefix() -> None:
    assert display_intent("/oopz 离开") == "停止"
    assert display_intent("/oopz 状态") == "状态"
    assert display_intent("/oopz 开始 1h") == "开始录音 1h"
    assert display_intent("3") == "3"


def test_synthetic_controller_id_is_stable_and_opaque() -> None:
    result = synthetic_controller_id("ou_example_admin")
    assert result == synthetic_controller_id("ou_example_admin")
    assert result.startswith("feishu-")
