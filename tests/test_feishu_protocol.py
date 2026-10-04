import pytest

from oopz_capture.feishu_protocol import display_intent, normalize_intent, synthetic_controller_id


@pytest.mark.parametrize("text,expected", [
    ("开始录音", "/oopz 开始"), ("录音", "/oopz 开始"), ("开始", "/oopz 开始"),
    ("开始录音 1小时", "/oopz 开始 1h"), ("录音 45 分钟", "/oopz 开始 45m"), ("开始录音 90", "/oopz 开始 90"),
    ("结束录音", "/oopz 离开"), ("结束", "/oopz 离开"), ("停止", "/oopz 离开"), ("停止录音", "/oopz 离开"),
    ("@OOPZ 管理机器人 结束录音", "/oopz 离开"), ("@bot 停止", "/oopz 离开"),
    ("状态", "/oopz 状态"), ("进度", "/oopz 状态"),
    ("重新出图", "/oopz 重新出图"), ("重新分析", "/oopz 重新出图"), ("待分析", "/oopz 重新出图"),
    ("重发图片", "/oopz 重发图片"), ("补发图片", "/oopz 重发图片"), ("最近图片", "/oopz 重发图片"),
    ("删除录音", "/oopz 删除录音"), ("删除会话", "/oopz 删除录音"),
    ("删除录音 2026-08-22_10-51-32_BJT", "/oopz 删除录音 2026-08-22_10-51-32_BJT"),
    ("设置", "/oopz 设置状态"), ("设置状态", "/oopz 设置状态"),
    ("设置 OOPZ_LANGUAGE=zh", "/oopz 设置 OOPZ_LANGUAGE=zh"), ("/oopz 设置 OOPZ_DEVICE=cuda:0", "/oopz 设置 OOPZ_DEVICE=cuda:0"),
    ("帮助", "/oopz 帮助"), ("指令", "/oopz 帮助"), ("/oopz 开始 45m", "/oopz 开始 45m"),
    ("3", "3"), ("取消", "取消"),
])
def test_commands_people_type_map_to_internal_commands(text, expected) -> None:
    assert normalize_intent(text) == expected


@pytest.mark.parametrize("text", ["帮我录音 45 分钟", "现在情况怎么样", "删除所有文件", "最近报告", "详细报告", "开始分析", "/oopz 增加管理员 123456", ""])
def test_everything_else_is_not_a_command(text) -> None:
    assert normalize_intent(text) is None


def test_display_intent_hides_internal_controller_prefix() -> None:
    assert display_intent("/oopz 离开") == "结束录音"
    assert display_intent("/oopz 开始 1h") == "开始录音 1h"
    assert display_intent("/oopz 重新出图") == "重新出图"
    assert display_intent("3") == "3"


def test_synthetic_controller_id_is_stable_and_opaque() -> None:
    result = synthetic_controller_id("ou_example_admin")
    assert result == synthetic_controller_id("ou_example_admin")
    assert result.startswith("feishu-")
