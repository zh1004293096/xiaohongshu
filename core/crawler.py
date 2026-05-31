"""小红书内容采集模块（Playwright 浏览器自动化）"""
import time
import re
import threading
from typing import List, Callable
from data.models import Note, Comment
from data.database import Database
from core.filter import match_intent
import config


class Crawler:
    """小红书搜索 + 评论采集"""

    def __init__(self, auth_manager, database: Database):
        self.auth = auth_manager
        self.db = database
        self._status = {
            'running': False,
            'keyword': '',
            'phase': 'idle',          # idle | searching | notes | comments | done
            'notes_found': 0,
            'notes_total': 0,
            'comments_found': 0,
            'current_note': '',
            'error': None,
        }
        self._lock = threading.Lock()

    @property
    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def start(self, keywords: List[str]):
        """启动采集（在后台线程中运行）"""
        if self._status['running']:
            return

        self._status['running'] = True
        self._status['error'] = None
        thread = threading.Thread(target=self._run, args=(keywords,), daemon=True)
        thread.start()

    def _run(self, keywords: List[str]):
        """在后台线程中执行采集（创建独立的 Playwright 实例避免 greenlet 冲突）"""
        pw = None
        browser = None
        context = None
        page = None

        try:
            # 创建独立的 Playwright 实例（不能跨线程共享）
            from playwright.sync_api import sync_playwright
            import config
            import json
            import os as _os

            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=False,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                ],
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                locale='zh-CN',
            )

            # 从文件加载 Cookie（复用登录态）
            cookie_file = _os.path.join(config.DATA_DIR, 'cookies.json')
            if _os.path.exists(cookie_file):
                try:
                    with open(cookie_file, 'r', encoding='utf-8') as f:
                        cookies = json.load(f)
                    if cookies:
                        context.add_cookies(cookies)
                except Exception:
                    pass

            page = context.new_page()

            # 反检测脚本
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
                delete navigator.__proto__.webdriver;
                window.chrome = { runtime: {} };
            """)

            all_notes = []

            # ---- 阶段 1: 搜索笔记 ----
            for kw in keywords:
                if not self._status['running']:
                    return
                self._set_phase('notes', kw)

                notes = self._search_keyword(page, kw)
                for note in notes:
                    if not self.db.note_exists(note.note_id):
                        self.db.save_note(note)
                all_notes.extend(notes)

                with self._lock:
                    self._status['notes_found'] += len(notes)

            # ---- 阶段 2: 采集评论 ----
            all_notes_dedup = {n.note_id: n for n in all_notes}.values()
            total = len(all_notes_dedup)

            with self._lock:
                self._status['notes_total'] = total

            for i, note in enumerate(all_notes_dedup):
                if not self._status['running']:
                    return
                self._set_phase('comments', note.keyword)
                with self._lock:
                    self._status['current_note'] = note.title[:30]

                comments = self._get_comments(page, note)
                for c in comments:
                    if not self.db.comment_exists(c.comment_id):
                        c.is_intent = match_intent(c.content)
                        self.db.save_comment(c)
                        with self._lock:
                            self._status['comments_found'] += 1

                time.sleep(0.5)

        except Exception as e:
            self._set_error(f'采集出错: {e}')
        finally:
            # 清理当前线程的 Playwright 实例
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    pw.stop()
                except Exception:
                    pass
            with self._lock:
                self._status['running'] = False
                self._status['phase'] = 'done'

    # ==================== 搜索笔记 ====================

    def _search_keyword(self, page, keyword: str) -> List[Note]:
        """搜索关键词并提取笔记列表"""
        notes = []
        try:
            search_url = config.XHS_SEARCH.format(keyword)
            page.goto(search_url, wait_until='domcontentloaded', timeout=config.REQUEST_TIMEOUT)
            page.wait_for_timeout(2000)

            # 滚动加载更多
            for _ in range(5):
                page.evaluate('window.scrollBy(0, 800)')
                page.wait_for_timeout(config.SCROLL_WAIT * 1000)

            # 从 DOM 提取笔记卡片
            note_cards = page.query_selector_all('[class*="note-item"], section[class*="note"], a[href*="/explore/"]')
            seen = set()

            for card in note_cards:
                if len(notes) >= config.MAX_NOTES_PER_KEYWORD:
                    break
                try:
                    # 获取笔记链接
                    link_el = card.query_selector('a[href*="/explore/"]')
                    if not link_el:
                        link_el = card  # 卡片本身可能就是 a 标签
                    href = link_el.get_attribute('href') if link_el else None
                    if not href or '/explore/' not in href:
                        continue

                    note_id = self._extract_note_id(href)
                    if not note_id or note_id in seen:
                        continue
                    seen.add(note_id)

                    # 标题
                    title_el = card.query_selector('[class*="title"], .note-title, span')
                    title = title_el.inner_text().strip() if title_el else '无标题'

                    # 作者
                    author_el = card.query_selector('[class*="author"], [class*="name"], [class*="nickname"]')
                    author_name = author_el.inner_text().strip() if author_el else ''
                    author_id = ''

                    url = f'{config.XHS_BASE}{href}' if href.startswith('/') else href

                    notes.append(Note(
                        note_id=note_id,
                        title=title[:100],
                        author_id=author_id,
                        author_name=author_name,
                        url=url,
                        keyword=keyword,
                    ))
                except Exception:
                    continue

        except Exception as e:
            pass  # 单个关键词失败不影响整体

        return notes

    # ==================== 采集评论 ====================

    def _get_comments(self, page, note: Note) -> List[Comment]:
        """进入笔记详情页，提取评论"""
        comments = []
        try:
            page.goto(note.url, wait_until='domcontentloaded', timeout=config.REQUEST_TIMEOUT)
            page.wait_for_timeout(2000)

            # 滚动评论区以加载更多评论
            for _ in range(6):
                page.evaluate('window.scrollBy(0, 600)')
                page.wait_for_timeout(1000)

            # 从 DOM 提取评论
            comment_els = page.query_selector_all(
                '[class*="comment-item"], [class*="comment"], [class*="CommentItem"]'
            )
            seen = set()

            for el in comment_els:
                if len(comments) >= config.MAX_COMMENTS_PER_NOTE:
                    break
                try:
                    # 评论者昵称
                    user_el = el.query_selector('[class*="name"], [class*="nickname"], [class*="user-name"], [class*="author"]')
                    user_name = user_el.inner_text().strip() if user_el else '未知用户'

                    # 评论内容
                    content_el = el.query_selector('[class*="content"], [class*="text"], [class*="desc"]')
                    content = content_el.inner_text().strip() if content_el else ''
                    if not content or len(content) < 2:
                        continue

                    # 评论时间
                    time_el = el.query_selector('[class*="date"], [class*="time"], [class*="createTime"]')
                    created_at = time_el.inner_text().strip() if time_el else ''

                    # 用户 ID（通常在链接中）
                    user_link = el.query_selector('a[href*="/profile/"]')
                    user_id = ''
                    if user_link:
                        href = user_link.get_attribute('href')
                        if href and '/profile/' in href:
                            user_id = href.split('/profile/')[-1].split('?')[0]

                    # 用内容+用户+时间做简易唯一键
                    comment_id = f"{note.note_id}_{hash(content + user_name + created_at) & 0x7FFFFFFF:08x}"
                    if comment_id in seen:
                        continue
                    seen.add(comment_id)

                    comments.append(Comment(
                        comment_id=comment_id,
                        note_id=note.note_id,
                        content=content[:500],
                        user_id=user_id,
                        user_name=user_name,
                        created_at=created_at,
                        note_title=note.title,
                        note_url=note.url,
                        keyword=note.keyword,
                    ))
                except Exception:
                    continue

        except Exception:
            pass

        return comments

    # ==================== 辅助方法 ====================

    def _extract_note_id(self, href: str) -> str | None:
        """从链接中提取笔记 ID"""
        # /explore/abc123 或 /explore/abc123?...
        match = re.search(r'/explore/([a-zA-Z0-9]+)', href)
        return match.group(1) if match else None

    def _set_phase(self, phase: str, keyword: str):
        with self._lock:
            self._status['phase'] = phase
            self._status['keyword'] = keyword

    def _set_error(self, msg: str):
        with self._lock:
            self._status['error'] = msg
            self._status['running'] = False
            self._status['phase'] = 'error'

    def stop(self):
        """停止采集"""
        with self._lock:
            self._status['running'] = False
