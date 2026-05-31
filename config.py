"""小红书团建线索挖掘工具 - 配置文件"""
import os
import sys

# 基础路径（兼容 PyInstaller 打包）
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 数据目录
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'leads.db')
BROWSER_PROFILE_DIR = os.path.join(DATA_DIR, 'browser_profile')

# Flask 配置
FLASK_HOST = '127.0.0.1'
FLASK_PORT = 5000

# ============================================================
# 团建搜索关键词（预设，后续可在界面增删）
# ============================================================
DEFAULT_KEYWORDS = [
    '团建',
    '公司团建',
    '部门团建',
    '团建方案',
    '团建推荐',
    '团建活动',
    '上海团建',
    '杭州团建',
    '南京团建',
    '苏州团建',
    '江浙沪团建',
    '周边团建',
    '团建策划',
    '团建去哪',
    '团建好去处',
    '户外团建',
    '室内团建',
    '年会团建',
    '小型团建',
    '一日团建',
]

# ============================================================
# 意向评论关键词 —— 匹配这些关键词的评论标记为"意向线索"
# ============================================================
INTENT_KEYWORDS = [
    '求推荐', '求方案', '求地址', '求分享',
    '怎么安排', '怎么收费', '怎么预约', '怎么报名', '怎么去',
    '有推荐吗', '有没有', '推荐一下',
    '多少钱', '人均多少', '大概多少',
    '联系方式', '私信', '了解一下', '请问',
    '价格', '费用', '咨询',
]

# ============================================================
# 小红书 URL
# ============================================================
XHS_BASE = 'https://www.xiaohongshu.com'
XHS_SEARCH = 'https://www.xiaohongshu.com/search_result?keyword={}&source=web_search_result_notes'
XHS_EXPLORE = 'https://www.xiaohongshu.com/explore'

# ============================================================
# 采集参数
# ============================================================
MAX_NOTES_PER_KEYWORD = 20   # 每个关键词最多采集笔记数
MAX_COMMENTS_PER_NOTE = 50   # 每篇笔记最多采集评论数
CRAWL_PAGE_DELAY = 2.0       # 翻页间隔（秒）
SCROLL_WAIT = 1.5            # 滚动等待加载时间
REQUEST_TIMEOUT = 30000      # 请求超时（毫秒）
