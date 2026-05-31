"""小红书内容采集模块（Playwright 浏览器自动化）"""
import time
import re
import random
import threading
import json
import os as _os
from typing import List
from data.models import Note, Comment
from data.database import Database
from core.filter import match_intent
from playwright.sync_api import sync_playwright
import config


class Crawler:
    """小红书搜索 + 评论采集"""

    def __init__(self, auth_manager, database: Database):
        self.auth = auth_manager
        self.db = database
        self._status = {
            'running': False,
            'keyword': '',
            'phase': 'idle',          # idle | searching | notes | comments | captcha | done
            'notes_found': 0,
            'notes_total': 0,
            'comments_found': 0,
            'current_note': '',
            'error': None,
            'needs_manual_action': False,  # 需要手动处理（如验证码）
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

        self._reset_status()
        self._status['running'] = True
        thread = threading.Thread(target=self._run, args=(keywords,), daemon=True)
        thread.start()

    def _reset_status(self):
        with self._lock:
            self._status.update({
                'running': False, 'keyword': '', 'phase': 'idle',
                'notes_found': 0, 'notes_total': 0, 'comments_found': 0,
                'current_note': '', 'error': None, 'needs_manual_action': False,
            })

    # ==================== 主循环 ====================

    def _run(self, keywords: List[str]):
        """后台采集主循环"""
        pw = None
        browser = None
        context = None
        page = None

        try:
            # ---- 启动浏览器 ----
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=False,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--window-size=1366,768',
                ],
            )
            context = browser.new_context(
                viewport={'width': 1366, 'height': 768},
                locale='zh-CN',
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/120.0.0.0 Safari/537.36'
                ),
            )

            # 恢复 Cookie
            cookie_file = _os.path.join(config.DATA_DIR, 'cookies.json')
            if _os.path.exists(cookie_file):
                try:
                    with open(cookie_file, 'r', encoding='utf-8') as f:
                        cookies = json.load(f)
                    if cookies:
                        context.add_cookies(cookies)
                        self._log('已加载 Cookie')
                except Exception:
                    pass

            page = context.new_page()

            # 反检测
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
                Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
                delete navigator.__proto__.webdriver;
                window.chrome = { runtime: {} };
            """)

            # ---- 先验证登录态 ----
            if not self._verify_login(page):
                self._set_error('登录态已失效，请退出后重新登录再采集')
                return

            all_notes = []

            # ---- 阶段 1: 搜索笔记 ----
            for ki, kw in enumerate(keywords):
                if not self._status['running']:
                    return

                self._set_phase('notes', kw)
                self._log(f'搜索关键词 ({ki+1}/{len(keywords)}): {kw}')

                # 检测验证码
                if self._check_captcha(page):
                    if not self._wait_for_captcha_resolved(page):
                        return

                notes = self._search_keyword(page, kw)
                for note in notes:
                    if not self.db.note_exists(note.note_id):
                        self.db.save_note(note)
                all_notes.extend(notes)

                with self._lock:
                    self._status['notes_found'] += len(notes)

                # 随机延迟
                self._human_delay(0.8, 2.5)

            self._log(f'共找到 {len(all_notes)} 条笔记（去重前）')

            # ---- 阶段 2: 采集评论 ----
            all_notes_dedup = list({n.note_id: n for n in all_notes}.values())
            total = len(all_notes_dedup)

            with self._lock:
                self._status['notes_total'] = total

            self._log(f'去重后 {total} 条笔记，开始采集评论')

            for i, note in enumerate(all_notes_dedup):
                if not self._status['running']:
                    return

                self._set_phase('comments', note.keyword)
                with self._lock:
                    self._status['current_note'] = note.title[:40]

                # 检测验证码
                if self._check_captcha(page):
                    if not self._wait_for_captcha_resolved(page):
                        return

                comments = self._get_comments(page, note)
                for c in comments:
                    if not self.db.comment_exists(c.comment_id):
                        c.is_intent = match_intent(c.content)
                        self.db.save_comment(c)
                        with self._lock:
                            self._status['comments_found'] += 1

                self._log(f'笔记 {i+1}/{total}: {note.title[:30]} → {len(comments)} 条评论')
                self._human_delay(0.5, 1.5)

        except Exception as e:
            self._set_error(f'采集异常: {e}')
            import traceback
            traceback.print_exc()
        finally:
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
                if self._status['phase'] != 'error':
                    self._status['phase'] = 'done'

    # ==================== 登录验证 ====================

    def _verify_login(self, page) -> bool:
        """访问首页确认登录态有效"""
        try:
            page.goto(config.XHS_EXPLORE, wait_until='domcontentloaded', timeout=15000)
            page.wait_for_timeout(2000)

            url = page.url
            if '/website-login/error' in url:
                self._log('登录态失效（风控拦截）')
                return False

            body = page.inner_text('body')[:2000]
            # 有登录弹窗说明未登录
            if '手机号登录' in body and '扫码登录' in body:
                self._log('登录态失效（需要重新登录）')
                return False

            self._log('登录态有效')
            return True
        except Exception as e:
            self._log(f'登录验证异常: {e}')
            return False

    # ==================== 搜索笔记 ====================

    def _search_keyword(self, page, keyword: str) -> List[Note]:
        """搜索关键词并提取笔记列表"""
        notes = []
        try:
            search_url = config.XHS_SEARCH.format(keyword)
            page.goto(search_url, wait_until='domcontentloaded', timeout=config.REQUEST_TIMEOUT)
            # 等待搜索结果动态加载
            self._wait_for_content(page, timeout=5000)

            # 滚动加载更多
            for i in range(5):
                page.evaluate('window.scrollBy(0, window.innerHeight * 0.7)')
                page.wait_for_timeout(int(config.SCROLL_WAIT * 1000))

            # 小红书搜索结果的笔记卡片——使用多种策略匹配
            # 小红书实际结构: section.note-item 或 div[class*="note"] 包含 a[href*="explore"]
            note_elements = page.query_selector_all(
                'a[href*="/explore/"][href*="/notes/"], '       # 笔记详情链接
                'section.note-item a[href*="/explore/"], '       # section 内的笔记链接
                '.feeds-page a[href*="/explore/"], '             # feeds 容器内的
                '[class*="search"] a[href*="/explore/"], '       # 搜索结果区的链接
                'a[href*="/search_result/"]'                     # 可能的备用格式
            )
            if not note_elements:
                # 回退：查找所有 explore 链接
                note_elements = page.query_selector_all('a[href*="/explore/"]')

            seen_ids = set()
            for el in note_elements:
                if len(notes) >= config.MAX_NOTES_PER_KEYWORD:
                    break
                try:
                    href = el.get_attribute('href') or ''
                    if '/explore/' not in href:
                        continue

                    note_id = self._extract_note_id(href)
                    if not note_id or note_id in seen_ids:
                        continue
                    seen_ids.add(note_id)

                    # 标题——从父容器往上找
                    title = self._find_title(el, page)

                    url = f'{config.XHS_BASE}{href}' if href.startswith('/') else href

                    notes.append(Note(
                        note_id=note_id,
                        title=title[:100],
                        author_id='',
                        author_name='',
                        url=url,
                        keyword=keyword,
                    ))
                except Exception:
                    continue

            if not notes:
                self._log(f'  关键词 "{keyword}" 未找到笔记（检查搜索页结构是否变化）')

        except Exception as e:
            self._log(f'  搜索 "{keyword}" 失败: {e}')
            # 截图供调试
            try:
                page.screenshot(
                    path=_os.path.join(config.DATA_DIR, f'debug_search_{keyword[:10]}.png'))
                self._log(f'  已保存调试图到 data/debug_search_{keyword[:10]}.png')
            except Exception:
                pass

        return notes

    def _find_title(self, link_el, page) -> str:
        """从笔记链接元素向上查找标题"""
        # 先看链接自身文本
        text = (link_el.inner_text() or '').strip()
        if len(text) >= 4:
            return text

        # 向上查找父容器中的标题元素
        parent = link_el
        for _ in range(5):  # 最多上溯 5 层
            try:
                parent = parent.query_selector('xpath=..')
                if not parent:
                    break
                # 在父容器中找标题
                title_el = parent.query_selector(
                    '.title, [class*="title"], '
                    'span:not([class*="icon"]):not([class*="tag"]), '
                    'a[class*="title"]'
                )
                if title_el:
                    t = (title_el.inner_text() or '').strip()
                    if len(t) >= 4:
                        return t
                # 取整个父容器的文本
                t = (parent.inner_text() or '').strip()
                if 4 <= len(t) <= 150:
                    return t
            except Exception:
                break

        return text[:100] or '无标题'

    # ==================== 采集评论 ====================

    def _get_comments(self, page, note: Note) -> List[Comment]:
        """进入笔记详情页，提取评论"""
        comments = []
        try:
            page.goto(note.url, wait_until='domcontentloaded', timeout=config.REQUEST_TIMEOUT)
            # 等待评论加载
            self._wait_for_content(page, timeout=4000)

            # 滚动评论区
            for i in range(8):
                page.evaluate('window.scrollBy(0, window.innerHeight * 0.5)')
                page.wait_for_timeout(800 + random.randint(0, 400))

            # 评论选择器：小红书实际可能使用的结构
            comment_selectors = [
                '.comment-item',
                '[class*="comment"]:not([class*="header"]):not([class*="footer"])',
                '.note-comment',
                '[class*="commentItem"]',
                '[class*="CommentItem"]',
                '.comments El',
            ]
            comment_els = []
            for sel in comment_selectors:
                els = page.query_selector_all(sel)
                if els:
                    comment_els = els
                    break

            if not comment_els:
                # 回退：找所有包含文字较多的子元素
                comment_els = page.query_selector_all(
                    'div:has(> span):not(:has(div))'
                )

            seen = set()

            for el in comment_els:
                if len(comments) >= config.MAX_COMMENTS_PER_NOTE:
                    break
                try:
                    content = self._find_comment_content(el)
                    if not content or len(content) < 2:
                        continue

                    user_name = self._find_comment_user(el)
                    created_at = self._find_comment_time(el)

                    # 用户 ID
                    user_id = ''
                    user_link = el.query_selector('a[href*="/profile/"]')
                    if user_link:
                        href = user_link.get_attribute('href') or ''
                        parts = href.split('/profile/')
                        if len(parts) > 1:
                            user_id = parts[-1].split('?')[0]

                    # 唯一键
                    cid = f"{note.note_id}_{abs(hash(content + user_name + created_at)) % 1000000:06d}"
                    if cid in seen:
                        continue
                    seen.add(cid)

                    comments.append(Comment(
                        comment_id=cid,
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

        except Exception as e:
            self._log(f'  采集评论失败: {note.title[:20]} - {e}')

        return comments

    def _find_comment_content(self, el) -> str:
        """从评论元素中提取内容"""
        for s in ['[class*="content"]', '[class*="text"]', '[class*="desc"]', 'p', 'span']:
            c = el.query_selector(s)
            if c:
                t = (c.inner_text() or '').strip()
                if len(t) >= 2:
                    return t
        return (el.inner_text() or '').strip()

    def _find_comment_user(self, el) -> str:
        for s in ['[class*="name"]', '[class*="nickname"]', '[class*="user-name"]',
                   '[class*="author"]', 'a[href*="/profile/"]']:
            c = el.query_selector(s)
            if c:
                t = (c.inner_text() or '').strip()
                if t:
                    return t
        return '未知用户'

    def _find_comment_time(self, el) -> str:
        for s in ['[class*="date"]', '[class*="time"]', '[class*="create"]', 'time']:
            c = el.query_selector(s)
            if c:
                t = (c.inner_text() or '').strip()
                if t:
                    return t
        return ''

    # ==================== 验证码处理 ====================

    def _check_captcha(self, page) -> bool:
        """检测是否出现验证码/滑块"""
        try:
            captcha_indicators = [
                '[class*="captcha"]',
                '[class*="verify"]',
                '[class*="slider"]',
                '[class*="slide"]',
                '[class*="puzzle"]',
                'text=请完成验证',
                'text=滑动验证',
                'text=安全验证',
                'text=点击完成验证',
            ]
            for sel in captcha_indicators:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    self._log('检测到验证码！')
                    return True

            # 额外检测：页面标题或 URL 包含验证
            if 'verify' in page.url.lower() or 'captcha' in page.url.lower():
                return True
            body = (page.inner_text('body') or '')[:500]
            if '请完成安全验证' in body or '拖动滑块' in body or '点击验证' in body:
                return True
        except Exception:
            pass
        return False

    def _wait_for_captcha_resolved(self, page) -> bool:
        """暂停采集，等待用户手动完成验证码"""
        with self._lock:
            self._status['phase'] = 'captcha'
            self._status['needs_manual_action'] = True
            self._status['error'] = '请在弹出的浏览器窗口中完成验证码验证，完成后自动继续...'

        self._log('等待用户完成验证码...（每 3 秒检查一次）')

        # 等待用户解决，最多等 5 分钟
        waited = 0
        while self._status['running'] and waited < 300:
            time.sleep(3)
            waited += 3
            if not self._check_captcha(page):
                self._log('验证码已解除，继续采集')
                with self._lock:
                    self._status['needs_manual_action'] = False
                    self._status['error'] = None
                return True

        # 超时
        self._set_error('验证码等待超时，采集已取消。请重新开始。')
        return False

    # ==================== 辅助方法 ====================

    def _extract_note_id(self, href: str) -> str | None:
        match = re.search(r'/explore/([a-zA-Z0-9]+)', href)
        return match.group(1) if match else None

    def _wait_for_content(self, page, timeout: int = 5000):
        """等待页面内容加载"""
        try:
            page.wait_for_load_state('networkidle', timeout=timeout)
        except Exception:
            pass
        page.wait_for_timeout(1500)

    def _human_delay(self, lo: float, hi: float):
        """随机人类延迟"""
        delay = lo + random.random() * (hi - lo)
        time.sleep(delay)

    def _set_phase(self, phase: str, keyword: str):
        with self._lock:
            self._status['phase'] = phase
            self._status['keyword'] = keyword

    def _set_error(self, msg: str):
        with self._lock:
            self._status['error'] = msg
            self._status['running'] = False
            self._status['phase'] = 'error'

    def _log(self, msg: str):
        """输出采集日志"""
        print(f'  [crawl] {msg}')

    def stop(self):
        """停止采集"""
        with self._lock:
            self._status['running'] = False
