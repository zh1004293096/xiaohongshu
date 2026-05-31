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
        self._api_dumped = False  # 每次采集重置调试标记

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
        raw_items = []  # 保存原始 item 用于日志
        api_urls_seen = set()
        is_first = not getattr(self, '_api_dumped', False)

        def on_response(response):
            nonlocal raw_items
            try:
                url = response.url
                if is_first and '/api/' in url and url not in api_urls_seen:
                    api_urls_seen.add(url)
                    if any(k in url for k in ['search', 'note', 'feed']):
                        self._log(f'  API: ...{url[-80:]}')

                if response.status != 200:
                    return

                is_search_api = (
                    '/api/sns/web/v1/search' in url or
                    '/api/sns/web/v2/search' in url or
                    ('/api/' in url and 'search' in url and 'notes' in url)
                )
                if not is_search_api:
                    return

                body = response.json()
                if not body:
                    return

                ok = body.get('success') or body.get('code') == 0
                data = body.get('data') or {}
                if not ok or not data:
                    return

                items = data.get('items') or data.get('notes') or []
                if not items and isinstance(data, list):
                    items = data

                if is_first and items:
                    try:
                        dump_path = _os.path.join(config.DATA_DIR, 'debug_api_response.json')
                        with open(dump_path, 'w', encoding='utf-8') as f:
                            json.dump(items[:2], f, ensure_ascii=False, indent=2)
                        self._log(f'  原始每条记录的所有 key: {list(items[0].keys()) if items else []}')
                        self._log(f'  原始数据已保存: {dump_path}')
                        self._api_dumped = True
                    except Exception:
                        pass

                raw_items.extend(items)
                self._log(f'    API 返回: {len(items)} 条')
            except Exception:
                pass

        try:
            page.on('response', on_response)
            alt_url = f'{config.XHS_SEARCH.format(keyword)}&type=1'
            page.goto(alt_url, wait_until='domcontentloaded', timeout=config.REQUEST_TIMEOUT)
            page.wait_for_timeout(3000)

            for _ in range(6):
                page.evaluate('window.scrollBy(0, window.innerHeight * 0.6)')
                page.wait_for_timeout(1500)

            page.remove_listener('response', on_response)

            # --- 解析笔记（更健壮的数据路径匹配）---
            seen_ids = set()
            for item in raw_items:
                try:
                    # XHS API 可能有多种嵌套方式
                    # 尝试所有可能的 note 数据容器
                    candidates = [
                        item,
                        item.get('note_card'),
                        item.get('note'),
                        item.get('noteCard'),
                        item.get('post'),
                    ]

                    for nc in candidates:
                        if not isinstance(nc, dict):
                            continue

                        # 尝试所有可能的 ID 字段
                        note_id = ''
                        for id_field in ('note_id', 'id', 'nid', 'noteId',
                                         'noteid', 'post_id'):
                            vid = nc.get(id_field)
                            if vid:
                                note_id = str(vid)
                                break
                        if not note_id:
                            continue

                        # 尝试所有可能的标题字段
                        title = ''
                        for t_field in ('display_title', 'title', 'content', 'desc',
                                        'description', 'summary', 'note_title', 'name'):
                            vt = nc.get(t_field)
                            if vt and isinstance(vt, str) and len(vt.strip()) >= 3:
                                title = vt.strip()
                                break
                        if not title:
                            # 可能标题在内层
                            for sub in (nc.get('info'), nc.get('meta')):
                                if isinstance(sub, dict):
                                    for t_field in ('title', 'display_title'):
                                        vt = sub.get(t_field)
                                        if vt and isinstance(vt, str) and len(vt.strip()) >= 3:
                                            title = vt.strip()
                                            break
                                    if title:
                                        break

                        note_url = f'{config.XHS_BASE}/explore/{note_id}'

                        # 作者
                        user_info = {}
                        for uf in ('user', 'author', 'user_info', 'owner', 'creator'):
                            u = nc.get(uf)
                            if isinstance(u, dict):
                                user_info = u
                                break
                        author_name = (user_info.get('nickname') or
                                       user_info.get('nick_name') or
                                       user_info.get('name') or '')
                        author_id = (user_info.get('user_id') or
                                     user_info.get('red_id') or
                                     user_info.get('id') or '')

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
                        break  # 找到有效数据，跳过其他 candidate
                except Exception:
                    continue

            self._log(f'  解析出 {len(notes)} 条有效笔记')

            if is_first and raw_items and not notes:
                # 没解析出笔记，把完整原始数据 dump 出来
                dump2 = _os.path.join(config.DATA_DIR, 'debug_api_full.json')
                try:
                    with open(dump2, 'w', encoding='utf-8') as f:
                        json.dump(raw_items[:5], f, ensure_ascii=False, indent=2)
                    self._log(f'  完整原始数据: {dump2}')
                except Exception:
                    pass

        except Exception as e:
            self._log(f'  API 拦截异常: {e}')
            notes = self._search_fallback(page, keyword)
        finally:
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
        raw_responses = []  # 保存原始响应用于调试
        is_first_note = (self._status['comments_found'] == 0)

        def on_response(response):
            nonlocal raw_responses
            try:
                url = response.url
                if response.status != 200:
                    return

                # 评论 API：放宽匹配条件
                # XHS 笔记详情页的评论 API 路径可能有 /comment/page, /comment/list 等
                is_comment_api = (
                    ('/api/' in url and 'comment' in url.lower() and 'sub_comment' not in url) or
                    # 也可能是 note detail API 直接带评论
                    ('/api/' in url and 'note' in url and 'detail' in url)
                )
                if not is_comment_api:
                    return

                body = response.json()
                if not body:
                    return

                ok = body.get('success') or body.get('code') == 0
                if not ok:
                    return

                data = body.get('data') or {}
                if not data:
                    return

                # 多种可能的评论列表字段
                com_list = (data.get('comments') or data.get('comment_list') or
                            data.get('items') or data.get('list') or [])
                if not com_list:
                    # 也可能 data 本身是评论数组
                    if isinstance(data, list):
                        com_list = data
                    else:
                        # 更深: data.note.comments 等
                        note_data = (data.get('note') or data.get('note_detail') or
                                     data.get('item') or {})
                        com_list = (note_data.get('comments') or
                                    note_data.get('comment_list') or [])
                if not com_list:
                    return

                if is_first_note and raw_responses == []:
                    # 保存第一个评论 API 响应
                    try:
                        dump_path = _os.path.join(config.DATA_DIR, 'debug_comment_api.json')
                        with open(dump_path, 'w', encoding='utf-8') as f:
                            json.dump(com_list[:3], f, ensure_ascii=False, indent=2)
                        self._log(f'    评论 API 数据: {dump_path}')
                    except Exception:
                        pass

                raw_responses.append(len(com_list))
                captured_comments.extend(com_list)
            except Exception:
                pass

        comments = []
        try:
            page.on('response', on_response)
            page.goto(note.url, wait_until='domcontentloaded', timeout=config.REQUEST_TIMEOUT)
            page.wait_for_timeout(3000)

            # 滚动触发评论加载
            for _ in range(12):
                page.evaluate('window.scrollBy(0, window.innerHeight * 0.4)')
                page.wait_for_timeout(1000 + random.randint(0, 500))

            page.remove_listener('response', on_response)

            if is_first_note and not captured_comments:
                self._log(f'    未拦截到评论 API，API 请求数: {len(raw_responses)}')

            seen = set()
            for item in captured_comments:
                try:
                    # 尝试多种内容字段
                    content = ''
                    for cf in ('content', 'comment', 'text', 'body', 'desc', 'note'):
                        v = item.get(cf)
                        if v and isinstance(v, str) and len(v.strip()) >= 2:
                            content = v.strip()
                            break
                    if not content:
                        continue

                    # 用户信息——可能在 user / author / user_info / 顶层字段
                    user = {}
                    for uf in ('user', 'author', 'user_info', 'creator', 'owner'):
                        u = item.get(uf)
                        if isinstance(u, dict):
                            user = u
                            break
                    user_name = (user.get('nickname') or user.get('nick_name') or
                                 user.get('name') or item.get('nickname') or
                                 item.get('user_name') or '用户')
                    user_id = (user.get('user_id') or user.get('red_id') or
                               user.get('id') or item.get('user_id') or '')

                    # 时间——可能是字符串或时间戳
                    created_at = ''
                    for tf in ('create_time', 'time', 'created_at', 'createTime',
                               'createtime', 'create_timestamp'):
                        vt = item.get(tf) or user.get(tf)
                        if vt:
                            if isinstance(vt, (int, float)) and vt > 1000000000:
                                import datetime
                                div = 1000 if vt > 100000000000 else 1
                                created_at = datetime.datetime.fromtimestamp(
                                    vt / div).strftime('%Y-%m-%d %H:%M')
                            else:
                                created_at = str(vt)[:30]
                            break

                    cid = item.get('id') or str(abs(hash(
                        note.note_id + content + str(user_id))) % 10000000)

                    if cid in seen:
                        continue
                    seen.add(cid)

                    comments.append(Comment(
                        comment_id=str(cid), note_id=note.note_id,
                        content=content[:500], user_id=str(user_id),
                        user_name=user_name, created_at=created_at,
                        note_title=note.title, note_url=note.url,
                        keyword=note.keyword,
                    ))
                except Exception:
                    continue

        except Exception as e:
            self._log(f'  评论异常: {note.title[:20]} - {e}')
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
