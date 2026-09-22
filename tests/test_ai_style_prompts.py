"""研究风格必须真正改变输出，而不只是换一段说明文字。

背景（1.5.2）：三种风格的提示词块只占 system prompt 的 ~5%，被 3000 字的通用
人设（含固定四维模板）压平，用户切风格几乎感知不到差别。这里的守卫锁三件事：

1. 风格块本身必须给出**可区分**的输出骨架（结构不同，不是形容词不同）；
2. 风格必须贯穿所有 AI 出口（chat / oneshot / 复盘报告），不只是对话；
3. 风格与长期记忆冲突时，风格优先（口子在 facade 的优先级段落里）。
"""

from __future__ import annotations

import re

from astock_backtester.ai.prompts import (
    CORE_RULES,
    ONESHOT_PROMPTS,
    ONESHOT_STYLE_DIRECTIVES,
    REVIEW_STYLE_SECTIONS,
    STYLE_PROMPTS,
    build_oneshot_messages,
    build_review_prompt,
    build_system_prompt,
)

STYLES = ("conservative", "balanced", "aggressive")

# 各风格必须出现的结构锚点（骨架代号，不是修饰词）
STYLE_ANCHORS = {
    "conservative": ("结论：回避 / 观察 / 谨慎参与", "下行风险", "止损位"),
    "balanced": ("多头逻辑", "主要风险", "关键价位"),
    "aggressive": ("情绪周期定位", "梯队结构", "龙头辨识度", "断板预案"),
}


def test_every_style_has_a_distinct_output_skeleton():
    for style, anchors in STYLE_ANCHORS.items():
        block = STYLE_PROMPTS[style]
        for anchor in anchors:
            assert anchor in block, f"{style} 缺少结构锚点：{anchor}"


def test_style_blocks_are_no_longer_dominated_by_the_core_persona():
    """风格块占比必须显著超过旧实现的 ~5%，否则结构会被通用模板压平。"""
    for style in STYLES:
        prompt = build_system_prompt(True, style)
        share = len(STYLE_PROMPTS[style]) / len(prompt)
        assert share > 0.15, f"{style} 风格块只占 {share:.1%}，仍会被核心人设压平"


def test_aggressive_carries_the_dragon_head_playbook():
    """激进风格必须是龙头选手口径，而不是"带点进攻形容词的均衡风格"。"""
    block = STYLE_PROMPTS["aggressive"]
    for term in ("总龙头", "连板", "晋级率", "炸板", "卡位", "补涨", "打板", "低吸", "断板"):
        assert term in block, f"激进风格缺少龙头战法术语：{term}"
    # 龙头选手的关键纪律：不用基本面尺子裁剪妖股
    assert "基本面" in block and "否定" in block


def test_conservative_explicitly_refuses_the_offensive_playbook():
    block = STYLE_PROMPTS["conservative"]
    assert "止损" in block
    assert "打板" in block  # 必须显式说明与本风格不匹配
    assert "不追高" in block or "不建议" in block


def test_oneshot_scenes_receive_the_style_directive():
    for style in STYLES:
        directive = ONESHOT_STYLE_DIRECTIVES[style]
        assert directive
        for scene in ("results_overview", "risk_alerts"):
            text = build_oneshot_messages(scene, "ctx", style)[0]["content"]
            assert directive in text, f"{scene} 未注入 {style} 的风格指令"


def test_data_coverage_oneshot_is_style_free_by_design():
    """覆盖诊断讲的是数据缺口怎么补，与交易风格无关：模板里没有该占位符。"""
    assert "{style_directive}" not in ONESHOT_PROMPTS["data_coverage"]
    text = build_oneshot_messages("data_coverage", "ctx", "aggressive")[0]["content"]
    assert "视角：" not in text


def test_review_report_skeleton_switches_with_style():
    outputs = {style: build_review_prompt(style, date_text="2026-09-22", data_text="<data>") for style in STYLES}
    assert "风险与防守" in outputs["conservative"]
    assert "多空对照" in outputs["balanced"]
    assert "情绪周期与梯队" in outputs["aggressive"]
    # 三种骨架互不串味
    assert "情绪周期与梯队" not in outputs["conservative"]
    assert "风险与防守" not in outputs["aggressive"]
    for style in STYLES:
        assert REVIEW_STYLE_SECTIONS[style].strip()


def test_review_prompt_leaves_no_unformatted_placeholder():
    for style in STYLES:
        text = build_review_prompt(style, date_text="2026-09-22", data_text="<data>")
        leftovers = re.findall(r"\{[a-z_]+\}", text)
        assert not leftovers, f"{style} 复盘提示词残留占位符：{leftovers}"


def test_core_rules_no_longer_hardcode_one_output_skeleton():
    """通用人设里的固定四维/评股模板必须移除：那正是把三种风格压平的东西。"""
    assert "分析框架默认四维" not in CORE_RULES
    assert "评股报告结构" not in CORE_RULES
    # 但必须明确"骨架由风格决定"，否则模型会退回默认习惯
    assert "输出骨架由当前研究风格决定" in CORE_RULES
