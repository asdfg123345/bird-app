# -*- coding: utf-8 -*-
"""
question_parser.py —— 中文问题解析：从问题文本中提取鸟类部位关键词

作者：天咏（明月照今日，风摇过两川。）

设计说明：
1. 仅做关键词子串匹配，不做分词与语义理解，命中即返回对应语义部位；
2. 全部关键词统一按长度降序排列后依次匹配，保证"头顶"先于"头"命中，
   避免短关键词抢占长关键词所属部位；
3. 可用部位提示供引擎在问题未提及部位时引导用户提问。
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import config


@dataclass
class ParseResult:
    """问题解析结果。"""
    part_en: Optional[str] = None   # 语义部位英文名（未命中时为 None）
    part_cn: Optional[str] = None   # 语义部位中文名（未命中时为 None）


# 部位提示展示顺序：按提问习惯排列，与 config.PART_CATEGORIES 顺序不同
_HINT_ORDER = (
    "crown", "beak", "belly", "wing", "tail", "leg",
    "eye", "throat", "back", "nape", "breast", "forehead",
)

# 关键词检索表：全部（关键词, 部位英文名）二元组按关键词长度降序排列，
# 同一部位内部与跨部位统一排序，长词优先命中，避免"头"抢占"头顶"
_KEYWORD_TABLE: List[Tuple[str, str]] = sorted(
    (
        (keyword, part_en)
        for part_en, keywords in config.PART_KEYWORDS.items()
        for keyword in keywords
    ),
    key=lambda item: len(item[0]),
    reverse=True,
)


def parse_question(question: str) -> ParseResult:
    """解析中文问题，命中部位关键词即返回对应部位，未命中返回空结果。"""
    if not question:
        return ParseResult(None, None)
    text = str(question)
    for keyword, part_en in _KEYWORD_TABLE:
        if keyword in text:
            return ParseResult(
                part_en=part_en,
                part_cn=config.PART_CN_NAMES[part_en],
            )
    return ParseResult(None, None)


def available_parts_hint() -> str:
    """返回可用部位中文提示，按预定义顺序以斜杠连接。"""
    return "/".join(config.PART_CN_NAMES[part] for part in _HINT_ORDER)
