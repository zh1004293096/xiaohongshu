# 小红书团建线索挖掘工具

## 项目进展（2026-05-31 / 已进入编码阶段）

### 项目目标
挖掘小红书上江浙沪团建相关内容，识别带有咨询意向的评论（如"求推荐""怎么安排""求方案"等），整理为销售线索用于手动揽客。

### 已确定的用户需求
- **数据来源**: 采集小红书公开笔记和评论
- **范围**: 特定团建关键词搜索，后续陆续补充
- **目标区域**: 江浙沪地区
- **数据字段**: 评论内容、评论者小红书ID、评论者昵称、评论时间、笔记链接、笔记标题
- **用户群体**: 自己做团建业务（团建公司 + 个人接单）
- **技术栈**: Python + Flask + Playwright + SQLite
- **交付形式**: PyInstaller 打包为 exe，可双击运行
- **用户技术背景**: 非技术用户，需要图形界面
- **登录方式**: 支持扫码登录 + 手机号验证码登录
- **触发方式**: 手动随时触发
- **筛选需求**: 
  - 支持按时间段筛选评论
  - 支持筛选最新 N 条评论
  - 支持点击链接直接跳转到对应笔记

---

## 头脑风暴 ✅ 已全部完成

- ✅ 探索项目上下文
- ✅ 提出澄清问题
- ✅ 提出 2-3 种方案 → **方案二：Flask + 浏览器**
- ✅ 展示设计（分节确认）
- ✅ 编写设计文档
- ✅ 规格自检
- ✅ 用户审查规格
- ✅ 过渡到实现

---

## 设计决策汇总

### 架构：三层 + 模块化
1. **Web 界面层** (`web/`) — Flask 模板 + 静态文件
2. **业务逻辑层** (`core/`) — 认证、采集、筛选、导出
3. **数据层** (`data/`) — SQLite 操作封装 + 数据模型

### 组件
| 模块 | 文件 | 职责 |
|------|------|------|
| 入口 | `app.py` | Flask 路由、启动浏览器、生命周期管理 |
| 配置 | `config.py` | 关键词预设、采集参数、数据库路径 |
| 认证 | `core/auth.py` | Playwright 扫码/手机号登录、Session 持久化 |
| 采集 | `core/crawler.py` | 关键词搜索 → 笔记提取 → 评论采集 |
| 筛选 | `core/filter.py` | 意向关键词匹配、时间段过滤、最新 N 条 |
| 导出 | `core/exporter.py` | Excel (.xlsx) 导出，意向行高亮 |
| 数据库 | `data/database.py` | 线程安全 SQLite CRUD |
| 模型 | `data/models.py` | Note / Comment 数据类 |

### 数据流
用户操作 → Flask 路由 → crawler 采集 → SQLite 存储 → 前端展示 → 筛选 → 导出 Excel

### 错误处理
- 登录失败/过期 → 弹窗提示重新登录
- 采集超时 → 自动重试 1 次，再失败提示"网络不稳定"
- 未登录搜索 → 跳转登录页
- 导出失败 → 提示"导出失败，请重试"

### 页面
- **登录页** (`login.html`)：扫码 + 手机号两 Tab
- **主页** (`main.html`)：关键词管理 + 采集控制 + 结果表格 + 导出

---

## 当前文件结构

```
d:\Project\xiaohongshu\
├── app.py                 # Flask 主入口 ✅
├── config.py              # 配置文件 ✅
├── requirements.txt       # 依赖清单 ✅
├── CLAUDE.md              # 本文件
├── core/
│   ├── __init__.py        ✅
│   ├── auth.py            # 登录认证 ✅
│   ├── crawler.py         # 内容采集 ✅
│   ├── filter.py          # 评论筛选 ✅
│   └── exporter.py        # Excel 导出 ✅
├── data/
│   ├── __init__.py        ✅
│   ├── models.py          # 数据模型 ✅
│   └── database.py        # 数据库操作 ✅
├── data/                  # 运行时数据目录（自动创建）
│   └── leads.db           # SQLite 数据库
├── web/
│   ├── templates/
│   │   ├── login.html     # 登录页 ✅
│   │   └── main.html      # 主页 ✅
│   └── static/
│       └── style.css      # 样式 ✅
└── docs/
    └── brainstorming-progress-2026-05-31.md
```

---

## 进展日志（2026-05-31 编码阶段）

### ✅ 第一轮自测通过

- ✅ Python 3.14 环境可用
- ✅ `pip install flask playwright openpyxl` 依赖齐全
- ✅ `playwright install chromium` 浏览器已安装
- ✅ 所有模块导入测试通过
- ✅ `match_intent` 意向关键词匹配测试通过
- ✅ Database CRUD 测试通过

### 🔧 本日修复的 Bug

1. **login.html switchTab 冗余逻辑** — 删除了无效的 CSS 伪类选择器
2. **exporter.py 路径问题** — 导出文件从 `core/` 改为 `data/exports/` 目录
3. **auth.py `_check_existing_login` 误判** — 原逻辑"没找到登录弹窗=已登录"过于宽松，改为需要正面证据
4. **auth.py QR 码兜底** — 找不到二维码时返回 None 而非全页面截图

### 🔧 本日架构变更

5. **登录方式从 headless 截图改为可见浏览器窗口** — 小红书风控拦截 headless 浏览器（error_code=300012），必须用 `headless=False`
6. **浏览器启动从 `launch_persistent_context` 改为 `launch()`** — 持久化 context 在 Windows 上崩溃，改用 `launch()` + Cookie JSON 文件持久化登录态
7. **爬虫独立创建 Playwright 实例** — 避免跨线程 greenlet 冲突，爬虫在后台线程中新建浏览器并从 Cookie 文件恢复登录态
8. **Flask 改为单线程模式** — 确保所有 Playwright 操作在同一 greenlet 线程
9. **浏览器预初始化** — Flask 启动时预启动 Chromium，避免首次登录等待过长
10. **登录流程改为 fire-and-forget + 轮询** — 前端不等待 HTTP 响应，立即开始轮询登录状态

### ✅ 本轮自测通过

- Flask 启动正常，所有 10 个 API 端点可访问
- 登录页渲染正常，点击"打开小红书登录页"成功弹出浏览器
- 浏览器成功访问小红书（无风控拦截）
- 状态轮询正常工作（返回 waiting/scanned/success）
- Cookie 持久化写入 `data/cookies.json`
- 爬虫可独立创建浏览器并从 Cookie 恢复登录态

### ⏳ 待验证（需要真人操作）

- 扫码登录流程 — 用小红书 App 扫浏览器中的二维码
- 手机号验证码登录流程 — 输入手机号获取验证码
- 采集流程 — 选关键词 → 开始采集 → 查看结果
- Excel 导出端到端

### ⏳ 待实现

- PyInstaller 打包为 exe

---

## 使用说明

### 启动
```bash
cd d:\Project\xiaohongshu
python app.py
```
浏览器自动打开 `http://127.0.0.1:5000/`。

### 登录
1. 点击「打开小红书登录页」→ 弹出 Chromium 浏览器窗口
2. 用小红书 App 扫描窗口中的二维码
3. 扫码成功后自动跳转主页
4. 登录态保存在 `data/cookies.json`，下次启动自动恢复

### 采集
1. 在主页选择关键词（默认 20 个团建相关关键词）
2. 点击「开始采集」
3. 等待进度条完成
4. 在表格中查看结果，意向评论标 🔥

### 导出
点击「导出为 Excel」下载 `.xlsx` 文件，意向行黄色高亮。

---

## 下一步

- ❌ 用户验证登录流程（扫码 + 手机号）
- ❌ 用户验证采集流程
- ❌ PyInstaller 打包为 exe
