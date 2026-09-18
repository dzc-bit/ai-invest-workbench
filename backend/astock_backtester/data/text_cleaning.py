"""爬取文本清洗的唯一归属。

对齐仓库架构纪律（§15-1 符号解析一个家、§15-2 HTTP 策略一个家）的同一精神：
"HTML/爬取正文 → 干净文本"此前分散在 briefing/news/realtime/cls_finance 各处，
实现与质量参差。新的清洗需求先来这里找，不要再私建本地副本。
"""

from __future__ import annotations

import re
from html import unescape

from bs4 import BeautifulSoup, Comment

# 非正文节点：取可见文本前整棵剔除，防止 script 内文随“只删标签”的清洗漏进正文。
_NON_TEXTUAL_SELECTORS = ("script", "style", "noscript", "iframe", "template", "textarea", "form", "button")

TIMESTAMP_PATTERN = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?")
NUMERIC_TOKEN_PATTERN = re.compile(r"^[+-]?\d+(?:\.\d+)?%?$")
SENTENCE_PUNCTUATION_PATTERN = re.compile(r"[，。；、：,.!?！？]")
CN_FIELD_KEYWORDS = (
    "名称",
    "板块",
    "股票数",
    "计算方式",
    "涨幅",
    "涨跌幅",
    "最新",
    "同比指数盈利",
)


def collapse_ws(text: str | None) -> str:
    """把任意空白序列折叠成单个空格。"""
    return re.sub(r"\s+", " ", text or "")


def html_to_plaintext(value: str | None) -> str:
    """HTML → 单行干净正文：剥非正文节点与注释，取可见文本，解码实体并折叠空白。"""
    text = value or ""
    if "<" not in text:
        return collapse_ws(unescape(text)).strip()
    soup = BeautifulSoup(text, "html.parser")
    for element in soup(_NON_TEXTUAL_SELECTORS):
        element.decompose()
    for comment in soup.find_all(string=lambda item: isinstance(item, Comment)):
        comment.extract()
    return collapse_ws(unescape(soup.get_text(" ", strip=True))).strip()


def is_percent_text(value: str) -> bool:
    return bool(re.search(r"[+-]?\d+(?:\.\d+)?%", value))


def _numeric_soup_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.split(r"\s+", text)
        if token and (NUMERIC_TOKEN_PATTERN.match(token) or re.search(r"\d", token))
    ]


def is_noisy_market_line(text: str) -> bool:
    """行情页噪声行判定：时间戳串、纯数字汤、字段关键词拼数字等不是正文。

    从 briefing 迁入；briefing 的 ``_is_noisy_content_line`` 是它的兼容别名。
    """
    cleaned = collapse_ws(unescape(text or "")).strip()
    if not cleaned:
        return True
    compact = re.sub(r"\s+", "", cleaned)
    if re.fullmatch(r"[%％]+", compact):
        return True
    if compact == "同比指数盈利":
        return True
    timestamp_count = len(TIMESTAMP_PATTERN.findall(cleaned))
    without_timestamps = TIMESTAMP_PATTERN.sub("", cleaned).strip()
    if timestamp_count >= 2 and len(without_timestamps) <= 24:
        return True

    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", without_timestamps))
    digit_count = len(re.findall(r"\d", without_timestamps))
    text_length = max(len(re.sub(r"\s+", "", without_timestamps)), 1)
    numeric_tokens = _numeric_soup_tokens(without_timestamps)
    has_field_keywords = sum(1 for keyword in CN_FIELD_KEYWORDS if keyword in without_timestamps) >= 2
    if (
        has_field_keywords
        and len(numeric_tokens) >= 3
        and (is_percent_text(without_timestamps) or timestamp_count > 0)
        and not SENTENCE_PUNCTUATION_PATTERN.search(without_timestamps)
    ):
        return True
    if digit_count >= 8 and cjk_count <= 6 and digit_count / text_length >= 0.35:
        return True
    if len(numeric_tokens) >= 4 and cjk_count <= 8 and not re.search(r"[，。；、：]", without_timestamps):
        return True
    if (
        len(numeric_tokens) >= 4
        and digit_count >= 8
        and digit_count / text_length >= 0.28
        and not re.search(r"[，。；、：]", without_timestamps)
    ):
        return True
    return False
