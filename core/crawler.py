"""小红书内容采集模块（Playwright 浏览器自动化）

采集策略：
  1. 搜索阶段 — 拦截 XHS 搜索 API 响应获取结构化笔记数据
  2. 评论阶段 — 拦截笔记详情 API 响应获取评论数据
  比解析 DOM 更可靠，不受 CSS Modules 动态类名影响。
"""
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
        self._lock = threading.Lock()
        self._reset_status()

    def _reset_status(self):
        self._status = {
            'running': False, 'keyword': '', 'phase': 'idle',
            'notes_found': 0, 'notes_total': 0, 'comments_found': 0,
            'current_note': '', 'error': None, 'needs_manual_action': False,
            'log': [],
        }

    @property
    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def start(self, keywords: List[str]):
        if self._status['running']:
            return
        self._reset_status()
        self._status['running'] = True
        threading.Thread(target=self._run, args=(keywords,), daemon=True).start()

    # ==================== 主循环 ====================

    def _run(self, keywords: List[str]):
        pw = browser = context = page = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=False,
                args=['--disable-blink-features=AutomationControlled',
                      '--disable-dev-shm-usage', '--no-sandbox'],
            )
            context = browser.new_context(
                viewport={'width': 1366, 'height': 768}, locale='zh-CN',
                user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                            'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'),
            )
            cf = _os.path.join(config.DATA_DIR, 'cookies.json')
            if _os.path.exists(cf):
                try:
                    with open(cf, 'r', encoding='utf-8') as f:
                        ck = json.load(f)
                    if ck:
                        context.add_cookies(ck)
                        self._log('Cookie 已加载')
                except Exception:
                    pass
            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                delete navigator.__proto__.webdriver;
                window.chrome = { runtime: {} };
            """)

            if not self._check_login(page):
                self._set_error('登录态失效，请退出后重新登录')
                return

            all_notes = []

            # === 阶段 1: 搜索笔记（拦截 API） ===
            for ki, kw in enumerate(keywords):
                if not self._status['running']:
                    return
                self._set_phase('notes', kw)
                self._log(f'搜索 ({ki+1}/{len(keywords)}): {kw}')

                if self._check_captcha(page):
                    if not self._wait_captcha(page):
                        return

                notes = self._search_via_api(page, kw)
                for n in notes:
                    if not self.db.note_exists(n.note_id):
                        self.db.save_note(n)
                all_notes.extend(notes)
                with self._lock:
                    self._status['notes_found'] += len(notes)
                self._human_delay(1.0, 3.0)

            dedup = list({n.note_id: n for n in all_notes}.values())
            with self._lock:
                self._status['notes_total'] = len(dedup)
            self._log(f'去重后 {len(dedup)} 篇笔记')

            if not dedup:
                self._log('没有找到任何笔记！请检查搜索关键词是否合理。')
                return

            # === 阶段 2: 采集评论 ===
            for i, note in enumerate(dedup):
                if not self._status['running']:
                    return
                self._set_phase('comments', note.keyword)
                with self._lock:
                    self._status['current_note'] = note.title[:40]

                if self._check_captcha(page):
                    if not self._wait_captcha(page):
                        return

                comments = self._comments_via_api(page, note)
                for c in comments:
                    if not self.db.comment_exists(c.comment_id):
                        c.is_intent = match_intent(c.content)
                        self.db.save_comment(c)
                        with self._lock:
                            self._status['comments_found'] += 1

                self._log(f'{i+1}/{len(dedup)}: {note.title[:30]} → {len(comments)} 评')
                self._human_delay(0.5, 1.5)

            self._log('采集完成！')

        except Exception as e:
            self._set_error(f'采集异常: {e}')
            import traceback
            traceback.print_exc()
        finally:
            for obj in (context, browser):
                if obj:
                    try: obj.close()
                    except Exception: pass
            if pw:
                try: pw.stop()
                except Exception: pass
            with self._lock:
                self._status['running'] = False
                if self._status['phase'] not in ('error',):
                    self._status['phase'] = 'done'

    # ==================== 登录验证 ====================

    def _check_login(self, page) -> bool:
        try:
            page.goto(config.XHS_EXPLORE, wait_until='domcontentloaded', timeout=15000)
            page.wait_for_timeout(2000)
            if '/website-login/error' in page.url:
                self._log('登录态失效(风控)')
                return False
            body = page.inner_text('body')[:2000]
            if '手机号登录' in body and '扫码登录' in body:
                self._log('登录态失效(需重新登录)')
                return False
            self._log('登录态有效')
            return True
        except Exception as e:
            self._log(f'登录验证异常: {e}')
            return False

    # ==================== 搜索（API 拦截） ====================

    def _search_via_api(self, page, keyword: str) -> List[Note]:
        """通过拦截 XHS 搜索 API 获取笔记"""
        notes = []
        captured_data = []
        api_urls_seen = set()
        is_first_search = (self._status['notes_found'] == 0)

        def on_response(response):
            """拦截搜索 API 响应"""
            try:
                url = response.url
                # 记录所有 API 请求的 URL（调试用）
                if is_first_search and '/api/' in url and url not in api_urls_seen:
                    api_urls_seen.add(url)
                    # 筛选有意义的 API（非静态资源）
                    if any(k in url for k in ['search', 'note', 'feed', 'recommend', 'homefeed']):
                        self._log(f'  API 请求: ...{url[-80:]}')

                if response.status != 200:
                    return

                # 搜索 API 可能有多种路径格式
                is_search_api = (
                    '/api/sns/web/v1/search' in url or
                    '/api/sns/web/v2/search' in url or
                    ('/api/' in url and 'search' in url and 'note' in url)
                )
                if not is_search_api:
                    return

                body = response.json()
                if not body:
                    return

                # 兼容不同响应格式：success/code + data
                ok = body.get('success') or body.get('code') == 0
                data = body.get('data') or {}
                if not ok or not data:
                    return

                items = data.get('items') or data.get('notes') or []
                if not items and isinstance(data, list):
                    items = data
                captured_data.extend(items)
                self._log(f'    API 拦截: {len(items)} 条笔记')
            except Exception:
                pass

        try:
            # 注册拦截
            page.on('response', on_response)

            # 打开搜索页
            search_url = config.XHS_SEARCH.format(keyword)
            # 同时尝试 type=1（笔记类型）
            alt_url = f'{config.XHS_SEARCH.format(keyword)}&type=1'
            page.goto(alt_url, wait_until='domcontentloaded', timeout=config.REQUEST_TIMEOUT)

            # 等待 API 返回
            page.wait_for_timeout(3000)
            # 滚动触发更多加载
            for _ in range(6):
                page.evaluate('window.scrollBy(0, window.innerHeight * 0.6)')
                page.wait_for_timeout(1500)

            # 移除拦截器
            page.remove_listener('response', on_response)

            # 解析捕获的数据
            seen_ids = set()
            for item in captured_data:
                try:
                    # 数据结构适应不同的 API 格式
                    note_id = ''
                    title = ''

                    # 格式1: item.note_card / item.display_title
                    nc = item.get('note_card') or item
                    note_id = nc.get('note_id') or nc.get('id') or ''
                    title = (nc.get('display_title') or nc.get('title') or
                             nc.get('desc', '') or '').strip()

                    if not note_id:
                        continue

                    # 构建完整 URL
                    note_url = f'{config.XHS_BASE}/explore/{note_id}'

                    # 作者
                    user_info = nc.get('user') or nc.get('author') or {}
                    author_name = user_info.get('nickname') or user_info.get('nick_name') or ''
                    author_id = user_info.get('user_id') or user_info.get('red_id') or ''

                    if note_id in seen_ids:
                        continue
                    seen_ids.add(note_id)

                    notes.append(Note(
                        note_id=note_id,
                        title=title[:100] or '无标题',
                        author_id=str(author_id),
                        author_name=author_name,
                        url=note_url,
                        keyword=keyword,
                    ))
                except Exception:
                    continue

            self._log(f'  API 拦截共获 {len(notes)} 条有效笔记')

        except Exception as e:
            self._log(f'  API 拦截异常: {e}')
            # 回退到 DOM 解析
            notes = self._search_fallback(page, keyword)
        finally:
            # 确保移除
            try:
                page.remove_listener('response', on_response)
            except Exception:
                pass

        return notes

    def _search_fallback(self, page, keyword: str) -> List[Note]:
        """DOM 回退方案：提取页面上所有探索链接"""
        notes = []
        try:
            page.wait_for_timeout(3000)
            for _ in range(4):
                page.evaluate('window.scrollBy(0, window.innerHeight * 0.6)')
                page.wait_for_timeout(1500)

            # 截图保存
            ss = _os.path.join(config.DATA_DIR, f'debug_fallback_{keyword[:6]}.png')
            page.screenshot(path=ss)
            self._log(f'  已保存截图: {ss}')

            # 提取所有 a 标签
            all_a = page.query_selector_all('a')
            seen_ids = set()
            for a in all_a:
                try:
                    href = a.get_attribute('href') or ''
                    # 多种可能的笔记 URL 格式
                    note_id = None
                    if '/explore/' in href:
                        note_id = re.search(r'/explore/([a-zA-Z0-9_-]+)', href)
                    elif '/discovery/item/' in href:
                        note_id = re.search(r'/discovery/item/([a-zA-Z0-9_-]+)', href)
                    if note_id:
                        nid = note_id.group(1)
                        if nid and nid not in seen_ids:
                            seen_ids.add(nid)
                            text = (a.inner_text() or '').strip()[:100]
                            notes.append(Note(
                                note_id=nid,
                                title=text or '无标题',
                                author_id='', author_name='',
                                url=f'{config.XHS_BASE}/explore/{nid}',
                                keyword=keyword,
                            ))
                except Exception:
                    continue

            self._log(f'  回退方案找到 {len(notes)} 条笔记')
        except Exception as e:
            self._log(f'  回退方案失败: {e}')
        return notes

    # ==================== 评论（API 拦截） ====================

    def _comments_via_api(self, page, note: Note) -> List[Comment]:
        """通过拦截 XHS 评论 API 获取评论"""
        captured_comments = []

        def on_response(response):
            try:
                url = response.url
                if response.status != 200:
                    return
                # 评论 API 特征：/api/sns/web/ 路径中含 comment，排除子评论
                if not ('/api/sns/web/' in url and 'comment' in url and 'sub_comment' not in url):
                    return
                body = response.json()
                if not body:
                    return
                ok = body.get('success') or body.get('code') == 0
                data = body.get('data') or {}
                if not ok or not data:
                    return
                items = (data.get('comments') or data.get('items') or
                         data.get('comment_list') or [])
                captured_comments.extend(items)
            except Exception:
                pass

        comments = []
        try:
            page.on('response', on_response)
            page.goto(note.url, wait_until='domcontentloaded', timeout=config.REQUEST_TIMEOUT)
            page.wait_for_timeout(3000)

            # 滚动触发评论加载
            for _ in range(10):
                page.evaluate('window.scrollBy(0, window.innerHeight * 0.4)')
                page.wait_for_timeout(1200)

            page.remove_listener('response', on_response)

            seen = set()
            for item in captured_comments:
                try:
                    # 格式可能是直接的评论对象
                    content = (item.get('content') or item.get('comment') or
                               item.get('text') or '').strip()
                    if not content or len(content) < 2:
                        continue

                    user = item.get('user') or item.get('author') or item.get('user_info') or {}
                    user_name = (user.get('nickname') or user.get('nick_name') or
                                 item.get('nickname') or '')
                    user_id = (user.get('user_id') or user.get('red_id') or
                               item.get('user_id') or '')
                    created_at = (item.get('create_time') or item.get('time') or
                                  item.get('created_at') or '')

                    # 时间可能是时间戳
                    if isinstance(created_at, (int, float)) and created_at > 1000000000:
                        import datetime
                        created_at = datetime.datetime.fromtimestamp(
                            created_at / 1000 if created_at > 100000000000 else created_at
                        ).strftime('%Y-%m-%d %H:%M')

                    cid = item.get('id') or str(abs(hash(
                        note.note_id + content + str(user_id))) % 10000000)

                    if cid in seen:
                        continue
                    seen.add(cid)

                    comments.append(Comment(
                        comment_id=str(cid),
                        note_id=note.note_id,
                        content=content[:500],
                        user_id=str(user_id),
                        user_name=user_name or '用户',
                        created_at=str(created_at) if created_at else '',
                        note_title=note.title,
                        note_url=note.url,
                        keyword=note.keyword,
                    ))
                except Exception:
                    continue

        except Exception as e:
            self._log(f'  API 评论异常: {note.title[:20]} - {e}')
        finally:
            try:
                page.remove_listener('response', on_response)
            except Exception:
                pass

        return comments

    # ==================== 验证码 ====================

    def _check_captcha(self, page) -> bool:
        try:
            for s in ['[class*="captcha"]', '[class*="verify"]',
                      'text=请完成验证', 'text=滑动验证']:
                el = page.query_selector(s)
                if el and el.is_visible():
                    self._log('检测到验证码！')
                    return True
            if '请完成安全验证' in (page.inner_text('body') or '')[:500]:
                return True
        except Exception:
            pass
        return False

    def _wait_captcha(self, page) -> bool:
        with self._lock:
            self._status['phase'] = 'captcha'
            self._status['needs_manual_action'] = True
            self._status['error'] = '请在浏览器中完成验证码后自动继续'
        self._log('等待验证码解除...')
        for _ in range(100):
            if not self._status['running']:
                return False
            time.sleep(3)
            if not self._check_captcha(page):
                self._log('验证码已解除')
                with self._lock:
                    self._status['needs_manual_action'] = False
                    self._status['error'] = None
                return True
        self._set_error('验证码等待超时')
        return False

    # ==================== 辅助 ====================

    def _human_delay(self, lo, hi):
        time.sleep(lo + random.random() * (hi - lo))

    def _set_phase(self, phase, keyword):
        with self._lock:
            self._status['phase'] = phase
            self._status['keyword'] = keyword

    def _set_error(self, msg):
        with self._lock:
            self._status['error'] = msg
            self._status['running'] = False
            self._status['phase'] = 'error'

    def _log(self, msg):
        print(f'  [crawl] {msg}')
        with self._lock:
            self._status['log'].append(msg)
            if len(self._status['log']) > 50:
                self._status['log'] = self._status['log'][-30:]

    def stop(self):
        with self._lock:
            self._status['running'] = False
