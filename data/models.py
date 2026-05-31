"""数据模型定义"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class Note:
    """小红书笔记"""
    note_id: str
    title: str
    author_id: str
    author_name: str
    url: str
    keyword: str          # 通过哪个关键词搜到的
    collected_at: str = field(default_factory=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


@dataclass
class Comment:
    """笔记评论"""
    comment_id: str
    note_id: str
    content: str
    user_id: str
    user_name: str
    created_at: str           # 评论发布时间
    note_title: str
    note_url: str
    keyword: str              # 搜索关键词
    is_intent: bool = False   # 是否为意向评论
    collected_at: str = field(default_factory=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
