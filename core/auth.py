"""小红书登录认证模块（Playwright 浏览器自动化）"""
import os
import time
import json
import shutil
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

import config

# Cookie 持久化文件路径
COOKIE_FILE = os.path.join(config.DATA_DIR, 'cookies.json')


class AuthManager:
    """管理小红书登录状态，支持扫码登录和手机号登录

    使用可见浏览器窗口（headless=False），用户在浏览器中直接扫码或输手机号。
    Web 界面轮询 check_login_status() 等待登录完成。
    Cookie 持久化到 JSON 文件，重启后无需重新登录。
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in = False
        self._user_info = {}
        self._login_page_opened = False

    # -------- 属性 --------

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    @property
    def user_info(self) -> dict:
        return self._user_info

    # -------- Cookie 持久化 --------

    def _save_cookies(self):
        """保存 Cookie 到文件"""
        if self._context:
            try:
                cookies = self._context.cookies()
                os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
                with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(cookies, f, ensure_ascii=False)
            except Exception:
                pass

    def _load_cookies(self):
        """从文件加载 Cookie 并注入到当前 context"""
        if not os.path.exists(COOKIE_FILE):
            return False
        try:
            with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            if cookies and self._context:
                self._context.add_cookies(cookies)
                return True
        except Exception:
            pass
        return False

    # -------- 浏览器生命周期 --------

    def init_browser(self):
        """启动 Playwright 浏览器（使用 launch() 避免 persistent_context 兼容性问题）

        在 Flask 启动时预调用，避免首次登录时等待过长。
        """
        if self._playwright is not None:
            return

        print('  [auth] 启动 Playwright...')
        self._playwright = sync_playwright().start()
        print('  [auth] 启动 Chromium 浏览器...')
        self._browser = self._playwright.chromium.launch(
            headless=False,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
            ],
        )
        self._context = self._browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            locale='zh-CN',
        )
        self._page = self._context.new_page()

        # 隐藏自动化特征
        self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
            delete navigator.__proto__.webdriver;
            window.chrome = { runtime: {} };
        """)

        # 尝试恢复 Cookie
        self._load_cookies()
        self._check_existing_login()

    def _check_existing_login(self):
        """检查是否已有登录态（通过 Cookie 恢复后验证）"""
        try:
            self._page.goto(config.XHS_EXPLORE, wait_until='domcontentloaded', timeout=15000)
            self._page.wait_for_timeout(2000)

            # 检查是否被风控拦截
            if '/website-login/error' in self._page.url:
                return

            current_url = self._page.url
            is_login_page = any(kw in current_url for kw in ['/login', '/sign', 'new_login'])

            page_text = self._page.inner_text('body')[:2000] if self._page else ''
            has_login_modal = ('手机号登录' in page_text and '扫码登录' in page_text)

            # 登录正面证据
            logged_in_texts = ['创作中心', '消息', '发布', '我的']
            has_logged_in_text = any(t in page_text for t in logged_in_texts)

            user_menu = self._page.query_selector(
                '[class*="user-menu"], [class*="userMenu"], '
                '[class*="avatar-wrapper"], [class*="AvatarWrapper"], '
                '.side-bar [class*="user"]')
            has_user_menu = user_menu is not None

            has_login_entry = '登录' in page_text and '注册' in page_text

            if (not is_login_page and not has_login_modal and
                    has_logged_in_text and has_user_menu and not has_login_entry):
                self._logged_in = True
                self._try_extract_user_info()
                self._save_cookies()
        except Exception:
            pass

    def _try_extract_user_info(self):
        """尝试从页面提取用户信息"""
        try:
            name_el = self._page.query_selector(
                '[class*="user-name"], [class*="username"], [class*="nickname"]')
            if name_el:
                self._user_info['name'] = name_el.inner_text()
        except Exception:
            pass

    def release_browser(self):
        """释放浏览器 context（保留 Cookie 文件供爬虫复用）"""
        if self._context:
            try:
                self._save_cookies()
                self._context.close()
            except Exception:
                pass
            self._context = None
        self._page = None

    def close(self):
        """关闭浏览器并停止 Playwright"""
        self.release_browser()
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None

    # -------- 扫码登录（浏览器窗口直接显示） --------

    def open_login_page(self) -> dict:
        """打开小红书登录页（在可见浏览器窗口中），立即返回。

        不等页面加载完成——调用方通过轮询 check_login_status() 判断登录结果。
        """
        if not self._page:
            try:
                self.init_browser()
            except Exception as e:
                return {'success': False, 'message': f'浏览器启动失败: {e}'}

        # 如果已经登录，直接返回
        if self._logged_in:
            return {'success': True, 'message': '已登录', 'already_logged_in': True}

        try:
            # 快速导航（短超时，不计成败）
            try:
                self._page.goto(config.XHS_BASE, wait_until='domcontentloaded', timeout=8000)
            except Exception:
                pass  # 超时也不影响，页面会在浏览器中继续加载

            # 检查风控
            if '/website-login/error' in self._page.url:
                return {
                    'success': False,
                    'message': '访问受限，请稍等片刻再试',
                }

            # 快速点击登录按钮（不计成败）
            self._try_click_login()
            self._login_page_opened = True
            return {'success': True, 'message': '请在浏览器窗口扫码登录'}

        except Exception as e:
            return {'success': False, 'message': f'打开登录页失败: {e}'}

    def _try_click_login(self):
        """快速尝试点击登录按钮，不计成败"""
        try:
            close_btns = self._page.query_selector_all(
                '[class*="close"], [class*="Close"], .mask-close')
            for btn in close_btns[:2]:
                try:
                    btn.click()
                except Exception:
                    pass

            login_triggers = [
                'text=登录',
                'button:has-text("登录")',
                '[class*="login-btn"]',
                'span:has-text("登录")',
            ]
            for sel in login_triggers:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        return
                except Exception:
                    continue
        except Exception:
            pass

    def check_login_status(self) -> dict:
        """轮询检测登录状态"""
        if not self._page:
            return {'status': 'error', 'message': '浏览器未启动'}

        # 如果已经标记为登录，做二次确认
        if self._logged_in:
            try:
                self._page.goto(config.XHS_EXPLORE, wait_until='domcontentloaded', timeout=10000)
                self._page.wait_for_timeout(1500)
                if '/website-login/error' in self._page.url:
                    self._logged_in = False
                    return {'status': 'error', 'message': '登录态失效，请重新登录'}
            except Exception:
                pass
            return {'status': 'success', 'message': '登录成功'}

        try:
            url = self._page.url

            # 风控检查
            if '/website-login/error' in url:
                return {'status': 'error',
                        'message': '访问受限，请关闭浏览器后稍等片刻再试'}

            # 登录成功判断：页面跳转到 explore 且无登录弹窗
            page_text = self._page.inner_text('body')[:2000] if self._page else ''

            # 检查用户菜单/头像
            user_signs = self._page.query_selector_all(
                '[class*="user-menu"], [class*="userMenu"], '
                '[class*="side-bar"] a[href*="/profile/"], '
                '[class*="avatar-wrapper"]'
            )

            # 检查是否还有登录弹窗
            login_modal = self._page.query_selector(
                '[class*="login-container"], [class*="login-modal"], [class*="LoginModal"]')
            has_login_modal = login_modal and login_modal.is_visible()

            if (url and ('/explore' in url or '/recommend' in url) and
                    len(user_signs) > 0 and not has_login_modal):
                self._logged_in = True
                self._try_extract_user_info()
                self._save_cookies()
                return {'status': 'success', 'message': '登录成功'}

            # 检查扫码进度
            if '扫描成功' in page_text or '已扫描' in page_text or '确认登录' in page_text:
                return {'status': 'scanned', 'message': '已扫描，请在手机上确认登录'}

            if '二维码已过期' in page_text or '点击刷新' in page_text:
                return {'status': 'expired', 'message': '二维码已过期，请刷新后重新扫码'}

            return {'status': 'waiting', 'message': '请用小红书 App 扫码'}

        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    # -------- 手机号登录 --------

    def phone_send_code(self, phone: str) -> dict:
        """发送手机验证码"""
        if not self._page:
            self.init_browser()

        # 如果浏览器是新开的，先打开登录页
        if not self._login_page_opened:
            result = self.open_login_page()
            if not result['success']:
                return result

        try:
            # 确保在首页且登录弹窗打开
            current_url = self._page.url
            if config.XHS_BASE not in current_url and '/explore' not in current_url:
                self._page.goto(config.XHS_BASE, wait_until='domcontentloaded', timeout=15000)
                self._page.wait_for_timeout(2000)

            # 切换到手机号登录 tab
            phone_tabs = [
                'text=手机号登录',
                'text=手机登录',
                '[class*="phone-login"]',
                '[class*="phone"]:has-text("登录")',
                '.tab:has-text("手机")',
            ]
            for sel in phone_tabs:
                try:
                    tab_el = self._page.query_selector(sel)
                    if tab_el and tab_el.is_visible():
                        tab_el.click()
                        self._page.wait_for_timeout(1000)
                        break
                except Exception:
                    continue

            # 输入手机号
            phone_input = self._page.query_selector(
                'input[placeholder*="手机号"], input[type="tel"]')
            if not phone_input:
                phone_input = self._page.query_selector(
                    '.phone-input input, [class*="phone"] input')
            if phone_input:
                phone_input.click()
                phone_input.fill(phone)

            # 点发送验证码
            send_btn = self._page.query_selector(
                'text=发送验证码, text=获取验证码, [class*="send-code"], [class*="sms"]')
            if send_btn:
                send_btn.click()
                self._page.wait_for_timeout(2000)

            # 检查是否有滑块验证码
            slider = self._page.query_selector(
                '[class*="slider"], [class*="captcha"], [class*="slide"]')
            if slider and slider.is_visible():
                self._solve_slider()
                self._page.wait_for_timeout(1000)
                # 滑块通过后再点一次发送
                send_btn2 = self._page.query_selector(
                    'text=发送验证码, text=获取验证码')
                if send_btn2:
                    send_btn2.click()
                    self._page.wait_for_timeout(1500)

            return {'success': True, 'message': '验证码已发送（如未收到请在浏览器中完成滑块验证）'}

        except Exception as e:
            return {'success': False, 'message': str(e)}

    def phone_verify(self, phone: str, code: str) -> dict:
        """输入验证码完成登录"""
        if not self._page:
            return {'success': False, 'message': '浏览器未启动'}

        try:
            code_input = self._page.query_selector(
                'input[placeholder*="验证码"], input[maxlength="6"], input[type="number"]')
            if code_input:
                code_input.click()
                code_input.fill(code)

            self._page.wait_for_timeout(500)

            # 点登录按钮
            login_btn = self._page.query_selector(
                'text=登录, button[type="submit"], [class*="login-btn"]')
            if login_btn:
                login_btn.click()
                self._page.wait_for_timeout(3000)

            # 验证是否登录成功
            url = self._page.url
            if '/explore' in url or '/recommend' in url:
                self._logged_in = True
                self._try_extract_user_info()
                self._save_cookies()
                return {'success': True, 'message': '登录成功'}

            # 检查是否有错误提示
            err = self._page.query_selector(
                '[class*="error"], [class*="提示"], [class*="toast"]')
            err_msg = err.inner_text() if err else '验证失败，请检查验证码'
            return {'success': False, 'message': err_msg}

        except Exception as e:
            return {'success': False, 'message': str(e)}

    def _solve_slider(self):
        """尝试自动过滑块验证"""
        try:
            slider = self._page.query_selector(
                '[class*="slider"], [class*="slide"], [class*="drag"]')
            if not slider:
                return
            box = slider.bounding_box()
            if box:
                x_center = box['x'] + box['width'] / 2
                y_center = box['y'] + box['height'] / 2
                self._page.mouse.move(x_center, y_center)
                self._page.mouse.down()
                for step in range(10):
                    self._page.mouse.move(
                        x_center + box['width'] * (step + 1) / 10, y_center)
                    self._page.wait_for_timeout(50)
                self._page.mouse.up()
                self._page.wait_for_timeout(500)
        except Exception:
            pass

    # -------- 退出 --------

    def logout(self):
        """退出登录，清除本地数据"""
        self._logged_in = False
        self._user_info = {}
        self._login_page_opened = False
        # 清除 Cookie 文件
        if os.path.exists(COOKIE_FILE):
            try:
                os.remove(COOKIE_FILE)
            except Exception:
                pass
        # 清除浏览器 context 中的 Cookie
        if self._context:
            try:
                self._context.clear_cookies()
            except Exception:
                pass
            try:
                self._page.evaluate('localStorage.clear(); sessionStorage.clear();')
            except Exception:
                pass
        # 清除旧的 browser_profile 目录
        if os.path.exists(config.BROWSER_PROFILE_DIR):
            try:
                shutil.rmtree(config.BROWSER_PROFILE_DIR)
            except Exception:
                pass
