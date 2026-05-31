"""评论筛选模块"""
from typing import List
import config


def match_intent(text: str) -> bool:
    """检查评论文本是否匹配意向关键词"""
    if not text:
        return False
    text_lower = text.lower()
    for kw in config.INTENT_KEYWORDS:
        if kw in text_lower:
            return True
    return False


def filter_by_date(comments: List[dict], start_date: str, end_date: str) -> List[dict]:
    """按时间段筛选评论列表（内存筛选）"""
    result = []
    for c in comments:
        date_str = (c.get('created_at') or '')[:10]
        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue
        result.append(c)
    return result


def filter_latest(comments: List[dict], n: int) -> List[dict]:
    """取最新 N 条（按采集时间倒序）"""
    sorted_comments = sorted(comments, key=lambda c: c.get('collected_at', ''), reverse=True)
    return sorted_comments[:n]


def filter_intent_only(comments: List[dict]) -> List[dict]:
    """只保留意向评论"""
    return [c for c in comments if c.get('is_intent')]
