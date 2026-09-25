"""研究风格必须真正改变输出，而不只是换一段说明文字。

背景（1.5.2）：三种风格的提示词块只占 system prompt 的 ~5%，被 3000 字的通用
人设（含固定四维模板）压平，用户切风格几乎感知不到差别。1.5.2 修了骨架；1.6.1
的实测又发现语气/情绪温度仍高度趋同（均衡 623 字是三块里最薄的、oneshot 口吻
指令只有 18~51 字、均衡几乎"没有作者"），于是把风格升级成**人设规格**：我是谁 /
怎么说话 / 情绪怎么出来 / 专属词汇 / 禁用词表 / 承压与认错。这里的守卫锁四件事：

1. 风格块必须给出**可区分**的输出骨架（结构不同，不是形容词不同）；
2. 风格必须贯穿所有 AI 出口（chat / oneshot / 快讯 / 复盘报告）；data_coverage
   与聚合要点按登记的口径不吃风格；
3. 语域指纹：三种风格在块厚、口吻指令长度、情绪温度、专属词汇上必须**可量化**
   地不同——否则风格改好了只是主观感受，下一轮又会腐化回三句同义话；
4. 纪律不随情绪放松：数字来自工具、免责声明保留、无确定性承诺词。
"""

from __future__ import annotations

import re

from astock_backtester.ai.prompts import (
    CORE_RULES,
    DIGEST_PROMPT,
    INSIGHT_PROMPT,
    ONESHOT_PROMPTS,
    ONESHOT_STYLE_DIRECTIVES,
    RESEARCH_STYLES,
    REVIEW_STYLE_SECTIONS,
    STYLE_FREE_ONESHOT_SCENES,
    STYLE_PROMPTS,
    build_insight_messages,
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

# 人设规格的必备小节：缺少任何一节，风格就退化回"一段形容词"。
PERSONA_SECTIONS = ("### 我是谁", "### 怎么说话", "### 情绪怎么出来", "### 专属词汇", "### 禁用词表", "### 承压与认错")

# 情绪表达的四个处境必须逐一写清（情绪服务于判断，不做口号）。
EMOTION_SITUATIONS = ("看多", "看空", "不确定", "遇险")

# 各风格在"禁用词表"之外的专属词汇：出现在别的风格里就是串味。
STYLE_EXCLUSIVE_VOCABULARY = {
    "conservative": ("安全边际", "股息率", "估值分位"),
    "balanced": ("赔率", "对价", "守正出奇"),
    "aggressive": ("总龙头", "炸板", "晋级率", "断板预案"),
}


def _strip_forbidden_section(block: str) -> str:
    """切掉禁用词表小节：列出禁词不等于使用禁词。"""
    return block.split("### 禁用词表", 1)[0]


def test_every_style_has_a_distinct_output_skeleton():
    for style, anchors in STYLE_ANCHORS.items():
        block = STYLE_PROMPTS[style]
        for anchor in anchors:
            assert anchor in block, f"{style} 缺少结构锚点：{anchor}"


def test_style_blocks_are_no_longer_dominated_by_the_core_persona():
    """风格块占比必须显著超过旧实现的 ~5%，否则结构会被核心人设压平。"""
    for style in STYLES:
        prompt = build_system_prompt(True, style)
        share = len(STYLE_PROMPTS[style]) / len(prompt)
        assert share > 0.15, f"{style} 风格块只占 {share:.1%}，仍会被核心人设压平"


def test_style_blocks_are_thickness_balanced():
    """三块厚度必须接近：均衡曾是最薄的（623 字 vs 激进 1487，差 2.4 倍），
    "最薄"本身就是没有风格的表现。"""
    lengths = {style: len(STYLE_PROMPTS[style]) for style in STYLES}
    ratio = max(lengths.values()) / min(lengths.values())
    assert ratio <= 1.6, f"风格块厚度不均（{lengths}，最大/最小 = {ratio:.2f}）"


def test_every_style_is_a_persona_spec_not_a_descriptor():
    """每个人设必须写满：我是谁 / 怎么说话 / 情绪四处境 / 专属词汇 / 禁用词 / 承压认错。"""
    for style, block in STYLE_PROMPTS.items():
        for section in PERSONA_SECTIONS:
            assert section in block, f"{style} 缺少人设小节：{section}"
        emotion = block.split("### 情绪怎么出来", 1)[1].split("###", 1)[0]
        for situation in EMOTION_SITUATIONS:
            assert situation in emotion, f"{style} 情绪段缺少处境：{situation}"
        assert "你是" in block, f"{style} 缺少第一人称身份锚"


def test_oneshot_style_directives_are_thick_enough_to_shape_short_output():
    """短点评的输出上限只有 80~120 字：一段 18 字的指令不可能让 80 字的
    点评长出一张脸（实测旧指令 27/18/51 字）。指令必须写足句式、用词与禁词。"""
    lengths = {}
    for style, directive in ONESHOT_STYLE_DIRECTIVES.items():
        lengths[style] = len(directive)
        assert len(directive) >= 140, f"{style} 的 oneshot 口吻指令只有 {len(directive)} 字，撑不起 80 字点评"
        assert "禁用" in directive, f"{style} 的口吻指令没有禁词约束（防串味最有效的单条约束）"
    assert len(set(ONESHOT_STYLE_DIRECTIVES.values())) == len(STYLES), "三种口吻指令存在完全相同的条目"


def test_emotional_temperature_profiles_differ():
    """情绪温度必须可量化地不同：激进有鲜明人格（戏剧化名词密集 + 强化词最多），
    均衡的辨识度来自对照句式而非情绪强度（最冷），保守居中但远低于激进。
    实测旧文本：均衡三项指标全部居中——"居中"就是没有风格。"""

    def profile(text: str) -> dict[str, int]:
        return {
            "intensifier": len(re.findall("绝不|必须|宁可|永远|禁止", text)),
            "drama": len(re.findall("情绪|高潮|退潮|分歧|妖股|断层|主升|亏钱效应|吃面|上车", text)),
            "exclaim": text.count("！"),
        }

    profiles = {style: profile(STYLE_PROMPTS[style]) for style in STYLES}
    assert profiles["aggressive"]["drama"] >= 10, "激进风格的戏剧化名词密度不足，不是龙头选手口径"
    assert profiles["aggressive"]["intensifier"] >= 6, "激进风格的纪律强度不足"
    assert profiles["aggressive"]["intensifier"] - profiles["balanced"]["intensifier"] >= 3, (
        "均衡与激进的情绪强度没有拉开（均衡靠句式辨识，不该和激进一样热）"
    )
    assert profiles["balanced"]["intensifier"] <= profiles["conservative"]["intensifier"], (
        "均衡的情绪强化词不应超过保守——它的辨识度是对照句式，不是语气"
    )
    assert profiles["conservative"]["exclaim"] == 0 and profiles["balanced"]["exclaim"] == 0, (
        "保守/均衡不允许感叹号（强度叹词属于激进的克制额度）"
    )
    assert profiles["aggressive"]["exclaim"] <= 3, "激进风格也不允许堆感叹号：情绪服务于判断，不做口号"


def test_style_vocabulary_does_not_leak_across_styles():
    """专属词汇不允许串味：保守不出现龙头战法词、激进不出现价值尺子词、
    均衡两套黑话都不沾（禁用词表小节被切除后仍算串味）。"""
    for style in STYLES:
        body = _strip_forbidden_section(STYLE_PROMPTS[style])
        for own in STYLE_EXCLUSIVE_VOCABULARY[style]:
            assert own in body, f"{style} 缺少专属词汇：{own}"
        for other in STYLES:
            if other == style:
                continue
            for term in STYLE_EXCLUSIVE_VOCABULARY[other]:
                assert term not in body, f"{style} 串入了 {other} 的专属词汇：{term}"


def test_no_certainty_promises_in_any_style_surface():
    """情绪化绝不等于放松纪律：任何风格表面都不得出现确定性鼓动词。"""
    banned = ("必涨", "稳赚", "包赚", "肯定涨", "必然")
    for text in (*STYLE_PROMPTS.values(), *ONESHOT_STYLE_DIRECTIVES.values()):
        for word in banned:
            assert word not in text, f"风格表面出现确定性承诺词：{word}"


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
    assert "本风格不匹配" in block


def test_oneshot_scenes_receive_the_style_directive():
    for style in STYLES:
        directive = ONESHOT_STYLE_DIRECTIVES[style]
        assert directive
        for scene in ("results_overview", "risk_alerts"):
            text = build_oneshot_messages(scene, "ctx", style)[0]["content"]
            assert directive in text, f"{scene} 未注入 {style} 的风格指令"


def test_data_coverage_oneshot_is_style_free_by_design():
    """覆盖诊断讲的是数据缺口怎么补，与交易风格无关：模板里没有该占位符，
    且该场景登记在 STYLE_FREE_ONESHOT_SCENES（导入期校验）。"""
    assert STYLE_FREE_ONESHOT_SCENES == frozenset({"data_coverage"})
    assert "{style_directive}" not in ONESHOT_PROMPTS["data_coverage"]
    text = build_oneshot_messages("data_coverage", "ctx", "aggressive")[0]["content"]
    assert "行文口吻" not in text


def test_insight_exit_carries_style_but_digest_stays_neutral():
    """快讯面向用户播报，带轻量人设语气；聚合要点是事实归纳，保持中立。"""
    for style in STYLES:
        directive = ONESHOT_STYLE_DIRECTIVES[style]
        text = build_insight_messages("data", style)[0]["content"]
        assert directive in text, f"AI 快讯未注入 {style} 的口吻指令"
    assert "{style_directive}" not in DIGEST_PROMPT


def test_review_report_skeleton_switches_with_style():
    outputs = {
        style: build_review_prompt(style, date_text="2026-09-22", data_text="<data>")[0]["content"]
        for style in STYLES
    }
    assert "风险与防守" in outputs["conservative"]
    assert "多空对照" in outputs["balanced"]
    assert "情绪周期与梯队" in outputs["aggressive"]
    # 三种骨架互不串味
    assert "情绪周期与梯队" not in outputs["conservative"]
    assert "风险与防守" not in outputs["aggressive"]
    for style in STYLES:
        assert REVIEW_STYLE_SECTIONS[style].strip()


def test_review_prompt_returns_messages_like_the_other_style_exits():
    """三个风格化出口签名必须一致（都返回 messages）——签名不齐会让
    "新增风格要改几处"变成隐性知识（reports 曾因此自己拼 message 列表）。"""
    for style in RESEARCH_STYLES:
        messages = build_review_prompt(style, date_text="2026-09-22", data_text="<data>")
        assert isinstance(messages, list) and messages[0]["role"] == "user"
        assert "{style_sections}" not in messages[0]["content"]


def test_review_prompt_leaves_no_unformatted_placeholder():
    for style in STYLES:
        text = build_review_prompt(style, date_text="2026-09-22", data_text="<data>")[0]["content"]
        leftovers = re.findall(r"\{[a-z_]+\}", text)
        assert not leftovers, f"{style} 复盘提示词残留占位符：{leftovers}"


def test_core_rules_no_longer_hardcode_one_output_skeleton():
    """通用人设里的固定四维/评股模板必须移除：那正是把三种风格压平的东西。"""
    assert "分析框架默认四维" not in CORE_RULES
    assert "评股报告结构" not in CORE_RULES
    # 但必须明确"骨架由风格决定"，否则模型会退回默认习惯
    assert "输出骨架由当前研究风格决定" in CORE_RULES


def test_core_rules_keep_discipline_stricter_than_any_style():
    """情绪化不等于放松纪律：通用人设必须保留数字来源与确定性承诺禁令。"""
    assert "每一个数字都必须来自工具返回结果" in CORE_RULES
    assert "确定性承诺词" in CORE_RULES


def test_insight_template_has_no_unescaped_json_placeholder_trap():
    """提示词含字面 JSON 时必须 {{ }} 转义（str.format 会把 {\"content\": ...}
    当占位符，项目踩过）。这里直接 format 一遍验证不抛 KeyError/IndexError。"""
    INSIGHT_PROMPT.format(style_directive="x", data="y")
    DIGEST_PROMPT.format(data="y")
