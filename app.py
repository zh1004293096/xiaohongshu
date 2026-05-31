"""小红书团建线索挖掘工具 - Flask 主程序"""
import os
import sys
import json
import threading
import webbrowser
from flask import Flask, render_template, request, jsonify, send_file, session

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.database import Database
from core.auth import AuthManager
from core.crawler import Crawler
from core.exporter import export_to_excel
import config

app = Flask(__name__, template_folder='web/templates', static_folder='web/static')
app.secret_key = os.urandom(24).hex()

# ---- 全局组件 ----
db = Database()
auth = AuthManager()
crawler = Crawler(auth_manager=auth, database=db)

# ---- 确保数据目录存在 ----
os.makedirs(config.DATA_DIR, exist_ok=True)
os.makedirs(config.BROWSER_PROFILE_DIR, exist_ok=True)
db.init_default_keywords()


# ============================================================
#  页面路由
# ============================================================

@app.route('/')
def index():
    if auth.is_logged_in:
        return render_template('main.html')
    return render_template('login.html')


@app.route('/main')
def main_page():
    return render_template('main.html')


# ============================================================
#  认证 API
# ============================================================

@app.route('/api/auth/check', methods=['GET'])
def api_auth_check():
    return jsonify({
        'logged_in': auth.is_logged_in,
        'user_info': auth.user_info,
    })


@app.route('/api/auth/login-page/open', methods=['POST'])
def api_auth_open_login():
    """打开小红书登录页（在浏览器窗口中）"""
    try:
        result = auth.open_login_page()
        if result.get('already_logged_in'):
            return jsonify({'success': True, 'message': '已登录'})
        if result.get('success'):
            return jsonify({'success': True, 'message': result.get('message', '请在浏览器中登录')})
        return jsonify({'success': False, 'message': result.get('message', '打开登录页失败')})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/auth/qrcode', methods=['POST'])
def api_auth_qrcode():
    """（兼容旧接口）打开登录页"""
    return api_auth_open_login()


@app.route('/api/auth/status', methods=['GET'])
def api_auth_status():
    """查询登录状态（前端轮询）"""
    return jsonify(auth.check_login_status())


@app.route('/api/auth/phone/send', methods=['POST'])
def api_auth_phone_send():
    """发送手机验证码"""
    data = request.get_json()
    phone = (data or {}).get('phone', '')
    if not phone:
        return jsonify({'success': False, 'message': '请输入手机号'})
    result = auth.phone_send_code(phone)
    return jsonify(result)


@app.route('/api/auth/phone/verify', methods=['POST'])
def api_auth_phone_verify():
    """验证手机验证码"""
    data = request.get_json()
    phone = (data or {}).get('phone', '')
    code = (data or {}).get('code', '')
    if not phone or not code:
        return jsonify({'success': False, 'message': '请输入手机号和验证码'})
    result = auth.phone_verify(phone, code)
    return jsonify(result)


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    auth.logout()
    return jsonify({'success': True})


# ============================================================
#  采集 API
# ============================================================

@app.route('/api/crawl/start', methods=['POST'])
def api_crawl_start():
    """开始采集"""
    if not auth.is_logged_in:
        return jsonify({'success': False, 'message': '请先登录'})

    if crawler.status['running']:
        return jsonify({'success': False, 'message': '采集正在进行中'})

    data = request.get_json() or {}
    keywords = data.get('keywords', [])
    if not keywords:
        keywords = [k for k in db.get_keywords()]

    crawl_keywords = [k.strip() for k in keywords if k and k.strip()]
    if not crawl_keywords:
        return jsonify({'success': False, 'message': '没有可用关键词'})

    # 释放登录浏览器窗口（爬虫会自己创建浏览器实例）
    auth.release_browser()

    crawler.start(crawl_keywords)
    return jsonify({'success': True, 'message': '采集已开始'})


@app.route('/api/crawl/status', methods=['GET'])
def api_crawl_status():
    """查询采集进度（前端轮询）"""
    return jsonify(crawler.status)


@app.route('/api/crawl/stop', methods=['POST'])
def api_crawl_stop():
    """停止采集"""
    crawler.stop()
    return jsonify({'success': True})


# ============================================================
#  数据查询 API
# ============================================================

@app.route('/api/results', methods=['GET'])
def api_results():
    """查询采集结果"""
    keyword = request.args.get('keyword')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    intent_only = request.args.get('intent_only', '0') == '1'
    limit = request.args.get('limit', type=int, default=None)
    offset = request.args.get('offset', type=int, default=0)

    results = db.get_results(
        keyword=keyword,
        start_date=start_date,
        end_date=end_date,
        intent_only=intent_only,
        limit=limit or 200,
        offset=offset,
    )
    return jsonify({'success': True, 'data': results, 'count': len(results)})


@app.route('/api/stats', methods=['GET'])
def api_stats():
    """获取统计数据"""
    return jsonify({'success': True, 'stats': db.get_statistics()})


# ============================================================
#  导出 API
# ============================================================

@app.route('/api/export', methods=['POST'])
def api_export():
    """导出评论为 Excel"""
    data = request.get_json() or {}
    keyword = data.get('keyword')
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    intent_only = data.get('intent_only', False)

    results = db.get_results(
        keyword=keyword,
        start_date=start_date,
        end_date=end_date,
        intent_only=intent_only,
        limit=None,  # 导出全部
    )

    if not results:
        return jsonify({'success': False, 'message': '没有数据可导出'})

    filepath = export_to_excel(results)
    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))


# ============================================================
#  关键词管理 API
# ============================================================

@app.route('/api/keywords', methods=['GET'])
def api_keywords():
    """获取关键词列表"""
    return jsonify({'success': True, 'keywords': db.get_keywords()})


@app.route('/api/keywords', methods=['POST'])
def api_keywords_add():
    """添加关键词"""
    data = request.get_json() or {}
    keyword = (data.get('keyword') or '').strip()
    if not keyword:
        return jsonify({'success': False, 'message': '关键词不能为空'})
    db.add_keyword(keyword)
    return jsonify({'success': True, 'keywords': db.get_keywords()})


@app.route('/api/keywords/<keyword>', methods=['DELETE'])
def api_keywords_remove(keyword):
    """删除关键词"""
    db.remove_keyword(keyword)
    return jsonify({'success': True, 'keywords': db.get_keywords()})


# ============================================================
#  数据清理 API
# ============================================================

@app.route('/api/results/clear', methods=['POST'])
def api_results_clear():
    """清空采集数据"""
    db.clear_all()
    return jsonify({'success': True, 'message': '数据已清空'})


# ============================================================
#  启动
# ============================================================

def main():
    print(f' ========================================')
    print(f'  小红书团建线索挖掘工具')
    print(f'  地址: http://{config.FLASK_HOST}:{config.FLASK_PORT}')
    print(f' ========================================')

    # 自动打开系统浏览器访问 Web 界面（不弹 Playwright，用户点按钮时才弹）
    threading.Thread(
        target=lambda: (__import__('time').sleep(1.5),
                        webbrowser.open(f'http://{config.FLASK_HOST}:{config.FLASK_PORT}')),
        daemon=True,
    ).start()

    try:
        app.run(
            host=config.FLASK_HOST,
            port=config.FLASK_PORT,
            debug=False,
            threaded=False,  # 单线程：所有 Playwright 操作在同一 greenlet，避免冲突
        )
    finally:
        auth.close()


if __name__ == '__main__':
    main()
