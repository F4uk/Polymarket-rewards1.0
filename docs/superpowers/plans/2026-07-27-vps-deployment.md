# VPS 部署实施计划

> **历史记录：** 本文保留当时的设计与实施过程；其中网页自更新任务已经停用，当前部署只允许管理员显式更新。请勿按本文重新启用运行时自更新。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让这个本地单机 Flask 应用能安全地部署到 Linux VPS，通过域名 + HTTPS 供一位远程用户使用，并保留网页上的一键更新能力。

**Architecture:** 引入一个由环境变量 `PMM_SERVER=1` 控制的"服务器模式"开关，隔离所有服务器专有行为（不开浏览器、固定端口、waitress、git 更新）。Flask 继续只绑 `127.0.0.1`，由同机的 Caddy 反代并提供 HTTPS。进程由 systemd 托管，`Restart=always` 同时承担崩溃恢复与"更新后自重启"。

**Tech Stack:** Python 3 / Flask 3.1 / waitress / systemd / Caddy 2 / ufw

## Global Constraints

- 设计依据：`docs/superpowers/specs/2026-07-27-vps-deployment-design.md`
- **必须单进程**：`web/routes.py` 的 `db` / `manager` / `encryption_key` 是模块级全局，引擎是进程内线程。禁止使用 gunicorn 等多 worker 方案。
- 非服务器模式（本地 Windows/mac）的行为必须逐字不变，除两项有意的安全收紧：`/api/update/*` 需要登录、新安装密码最短 12 位。
- 不改动做市策略、引擎、订单、监控的任何行为。
- 所有面向用户的字符串用简体中文。
- 环境变量名固定为 `PMM_SERVER`，值为 `"1"` 时开启。
- 测试用 pytest，放在 `tests/`，纯逻辑、不触网。
- 每个任务结束时提交，提交信息用中文，格式沿用仓库现有习惯（`feat:` / `fix:` / `docs:` / `chore:`）。

---

### Task 1: 给 `/api/update/*` 补上登录鉴权

三个更新端点当前无鉴权。本地只绑回环时无害，公网暴露后任何人 POST 一次 `/api/update/apply` 就能让进程退出重启。这是既有漏洞，与部署无关，先修。

**2026-07-27 裁定（覆盖本任务下文中"三个都加"的写法）**：只给 `apply` 和 `status` 加 `login_required`，**`check` 保持免登录**。`login.html` / `setup.html` 底部有「检查更新」链接，未登录时能查版本是有意功能；`check` 只向固定 GitHub URL 读版本号、带 30 分钟缓存，不改状态也不泄露钱包信息。

**Files:**
- Modify: `web/routes.py:1390-1404`
- Test: `tests/test_update_routes.py`（现有文件，需改写其中的免登录断言）

**Interfaces:**
- Consumes: 现有的 `login_required` 装饰器（`web/routes.py:176`）
- Produces: 无新接口

- [ ] **Step 1: 改写现有测试，断言未登录被拒、已登录可访问**

现有 `tests/test_update_routes.py` 里 `test_check_is_public_and_returns_json` 明确断言"不登录也能访问"，这个前提现在要反过来。把整个文件改成：

```python
"""tests/test_update_routes.py — 更新端点(需登录 + 安全闸)。"""

import web.routes as routes
import web.update as updater


def _client(logged_in=True):
    routes.app.config["TESTING"] = True
    c = routes.app.test_client()
    if logged_in:
        with c.session_transaction() as s:
            s["logged_in"] = True
    return c


def test_check_stays_public(monkeypatch):
    # 登录页/设置页底部的「检查更新」链接要能用。check 只读 GitHub 版本号,
    # 带 30 分钟缓存,不改状态也不泄露钱包信息。
    monkeypatch.setattr(
        updater, "check_update", lambda: {"update_available": False, "current": "1.0.7"}
    )
    resp = _client(logged_in=False).get("/api/update/check")
    assert resp.status_code == 200
    assert resp.get_json()["current"] == "1.0.7"


def test_apply_requires_login(monkeypatch):
    called = []
    monkeypatch.setattr(updater, "start_update", lambda mgr: called.append(1) or {})
    resp = _client(logged_in=False).post("/api/update/apply")
    assert resp.status_code in (301, 302)
    assert called == []  # 关键:未登录绝不能触发更新


def test_status_requires_login():
    resp = _client(logged_in=False).get("/api/update/status")
    assert resp.status_code in (301, 302)


def test_check_returns_json_when_logged_in(monkeypatch):
    monkeypatch.setattr(
        updater, "check_update", lambda: {"update_available": False, "current": "1.0.7"}
    )
    resp = _client().get("/api/update/check")
    assert resp.status_code == 200
    assert resp.get_json()["current"] == "1.0.7"


def test_apply_blocked_returns_409(monkeypatch):
    monkeypatch.setattr(
        updater, "start_update", lambda mgr: {"ok": False, "message": "引擎正在运行"}
    )
    resp = _client().post("/api/update/apply")
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


def test_apply_ok_returns_200(monkeypatch):
    monkeypatch.setattr(updater, "start_update", lambda mgr: {"ok": True})
    resp = _client().post("/api/update/apply")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_status_returns_snapshot_when_logged_in():
    updater.STATE.state = "downloading"
    updater.STATE.percent = 42
    resp = _client().get("/api/update/status")
    body = resp.get_json()
    assert body["state"] == "downloading"
    assert body["percent"] == 42
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_update_routes.py -v`
Expected: 三个 `*_requires_login` 测试 FAIL（返回 200 而非重定向）

- [ ] **Step 3: 加上装饰器**

`web/routes.py` 中三处，各加一行 `@login_required`（放在 `@app.route` 下方、`def` 上方）：

`check` 不加装饰器（保持免登录），只改 `apply` 与 `status`：

```python
@app.route("/api/update/apply", methods=["POST"])
@login_required
def api_update_apply():
    result = updater.start_update(manager)
    ...


@app.route("/api/update/status", methods=["GET"])
@login_required
def api_update_status():
    return jsonify(updater.STATE.snapshot())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_update_routes.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 确认没有连带破坏**

Run: `pytest -q`
Expected: 全部 PASS。若有其他测试依赖免登录访问更新端点，一并按同样方式补登录态。

- [ ] **Step 6: 提交**

```bash
git add web/routes.py tests/test_update_routes.py
git commit -m "fix(security): /api/update/* 补上登录鉴权

三个更新端点原先免登录。本地只绑回环时无害,公网部署后任何人 POST
一次 /api/update/apply 即可让进程退出重启。"
```

---

### Task 2: 服务器模式开关

**Files:**
- Modify: `config.py`（在 `HOST`/`PORT` 附近）
- Test: `tests/test_server_mode.py`（新建）

**Interfaces:**
- Produces: `config.SERVER_MODE`（bool），后续所有任务都从 `config` 导入它

- [ ] **Step 1: 写失败测试**

创建 `tests/test_server_mode.py`：

```python
"""tests/test_server_mode.py — 服务器模式开关(PMM_SERVER)。"""

import importlib
import config


def _reload_with(monkeypatch, value):
    """用给定的 PMM_SERVER 值重新导入 config,返回重载后的模块。"""
    if value is None:
        monkeypatch.delenv("PMM_SERVER", raising=False)
    else:
        monkeypatch.setenv("PMM_SERVER", value)
    return importlib.reload(config)


def test_off_by_default(monkeypatch):
    assert _reload_with(monkeypatch, None).SERVER_MODE is False


def test_on_when_env_is_1(monkeypatch):
    assert _reload_with(monkeypatch, "1").SERVER_MODE is True


def test_off_for_other_values(monkeypatch):
    assert _reload_with(monkeypatch, "0").SERVER_MODE is False
    assert _reload_with(monkeypatch, "true").SERVER_MODE is False
    assert _reload_with(monkeypatch, "").SERVER_MODE is False


def teardown_module():
    """把 config 恢复成不带环境变量的状态,避免污染其他测试。"""
    import os

    os.environ.pop("PMM_SERVER", None)
    importlib.reload(config)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_server_mode.py -v`
Expected: FAIL，`AttributeError: module 'config' has no attribute 'SERVER_MODE'`

- [ ] **Step 3: 实现**

在 `config.py` 中 `SECRET_KEY = None` 那一行之后加入：

```python
# 服务器模式:部署在 VPS 上由 systemd 注入 PMM_SERVER=1 开启。
# 显式开关而非按 sys.platform 推断,这样在 mac/Windows 上开发时行为完全不变。
# 它控制四件事:不开浏览器、固定端口、用 waitress、更新走 git。
SERVER_MODE = os.environ.get("PMM_SERVER") == "1"
```

`config.py` 顶部已经 `import os`，不需要新增导入。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_server_mode.py -v && pytest -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add config.py tests/test_server_mode.py
git commit -m "feat(deploy): 新增服务器模式开关 PMM_SERVER"
```

---

### Task 3: 服务器模式下的端口策略与启动方式

`utils/net.py` 的 `pick_port` 在首选端口绑不上时会回退到随机端口 —— 这是为 Windows 的 Hyper-V 保留端口区间写的。服务器上换了端口，Caddy 就反代不到，表现为"服务启动成功但网页打不开"。服务器模式下必须固定端口。

**Files:**
- Modify: `utils/net.py`
- Modify: `app.py:60-77`
- Modify: `requirements.txt`
- Test: `tests/test_net.py`（现有文件，追加）

**Interfaces:**
- Consumes: `config.SERVER_MODE`（Task 2）
- Produces: `utils.net.resolve_port(host: str, preferred: int, server_mode: bool) -> int`

- [ ] **Step 1: 写失败测试**

在 `tests/test_net.py` 末尾追加：

```python
import socket
from utils.net import resolve_port


class TestResolvePort:
    def test_server_mode_returns_preferred_without_probing(self):
        # 服务器模式下不探测、不回退,直接返回首选端口
        assert resolve_port("127.0.0.1", 8765, True) == 8765

    def test_server_mode_returns_preferred_even_if_occupied(self):
        # 端口被占用也照样返回它 —— 让后续 bind 直接报错退出,
        # 而不是悄悄换端口导致 Caddy 反代不到
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            occupied = s.getsockname()[1]
            assert resolve_port("127.0.0.1", occupied, True) == occupied

    def test_local_mode_falls_back_when_occupied(self):
        # 本地模式保持原有行为:占用时回退到别的端口
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            occupied = s.getsockname()[1]
            assert resolve_port("127.0.0.1", occupied, False) != occupied

    def test_local_mode_returns_preferred_when_free(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        assert resolve_port("127.0.0.1", free, False) == free
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_net.py -v`
Expected: FAIL，`ImportError: cannot import name 'resolve_port'`

- [ ] **Step 3: 实现 `resolve_port`**

在 `utils/net.py` 末尾追加：

```python
def resolve_port(host: str, preferred: int, server_mode: bool) -> int:
    """选定实际监听端口。

    服务器模式下固定用 preferred:反向代理写死了这个端口,一旦回退到别的端口,
    服务看起来启动成功、网页却打不开。端口被占用时让后续 bind 直接失败,
    由 systemd 的重启循环和日志把问题暴露出来。
    本地模式沿用 pick_port 的回退行为(见其 docstring 中的 Windows 端口保留问题)。
    """
    return preferred if server_mode else pick_port(host, preferred)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_net.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 改 `app.py` 的启动路径**

把 `main()` 中从 `port = pick_port(...)` 到 `app.run(...)` 的整段替换为：

```python
    # Windows (Hyper-V/WSL) may reserve the configured port; fall back to a
    # free one so the app still starts, and open the browser to the real port.
    # 服务器模式下不回退 —— 反向代理写死了端口。
    port = resolve_port(HOST, PORT, SERVER_MODE)
    if port != PORT:
        logger.warning("端口 %d 不可用（可能被系统保留），改用 %d", PORT, port)

    if not SERVER_MODE:
        # Open browser after a short delay
        def open_browser():
            import time

            time.sleep(1.5)
            url = f"http://{HOST}:{port}"
            if pw_hash is None:
                url += "/setup"
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()

    if SERVER_MODE:
        from waitress import serve

        logger.info(
            "服务器模式启动:http://%s:%d（对外的 HTTPS 由反向代理提供）", HOST, port
        )
        # 必须单进程:routes 的 db/manager/encryption_key 是模块级全局,
        # 引擎是进程内线程。多 worker 会让同一批钱包被两套引擎重复下单。
        serve(app, host=HOST, port=port, threads=8)
    else:
        logger.info("Starting Polymarket Market Maker on http://%s:%d", HOST, port)
        app.run(host=HOST, port=port, debug=False, use_reloader=False)
```

同时更新 `app.py` 顶部的导入：

```python
from config import DB_PATH, LOG_PATH, HOST, PORT, SERVER_MODE
from utils.net import resolve_port
```

（`pick_port` 不再被 `app.py` 直接使用，从导入中去掉；它仍由 `resolve_port` 内部调用。）

- [ ] **Step 6: 加依赖**

`requirements.txt` 末尾加一行：

```
waitress>=3.0.0
```

- [ ] **Step 7: 验证**

Run: `pip install -r requirements.txt && pytest -q`
Expected: 全部 PASS

Run: `python app.py`
Expected: 本地行为不变 —— 浏览器自动打开，网页可用。确认后 Ctrl-C 退出。

Run: `PMM_SERVER=1 python app.py`
Expected: 不开浏览器，日志出现"服务器模式启动"，`curl -I http://127.0.0.1:8765/login` 返回 200。确认后 Ctrl-C 退出。

- [ ] **Step 8: 提交**

```bash
git add utils/net.py app.py requirements.txt tests/test_net.py
git commit -m "feat(deploy): 服务器模式用 waitress 起服务、固定端口、不开浏览器"
```

---

### Task 4: 会话与 Cookie 安全属性

**Files:**
- Modify: `web/routes.py:43-47`（`app = Flask(...)` 之后）
- Test: `tests/test_session_security.py`（新建）

**Interfaces:**
- Consumes: `config.SERVER_MODE`（Task 2）
- Produces: 无新函数；`app.config` 中的 cookie 相关配置项

- [ ] **Step 1: 写失败测试**

创建 `tests/test_session_security.py`：

```python
"""tests/test_session_security.py — 会话 cookie 的安全属性。"""

from datetime import timedelta

import web.routes as routes


def test_httponly_and_samesite_always_on():
    # 两种模式都要开:防 XSS 读取、防跨站带 cookie
    assert routes.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert routes.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_secure_flag_follows_server_mode():
    # 本地是 http,开了 Secure 浏览器就不会回传 cookie,直接登不进去;
    # 服务器上全程 https,必须开。
    # 比对 routes.SERVER_MODE(即 app.config 实际采用的那个值),不要重新 import
    # config —— tests/test_server_mode.py 会 reload config,值可能已被改过。
    assert routes.app.config["SESSION_COOKIE_SECURE"] is routes.SERVER_MODE


def test_session_lifetime_is_seven_days():
    assert routes.app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(days=7)


def test_proxyfix_installed():
    # Caddy 反代后 remote_addr 恒为 127.0.0.1,登录限速必须能拿到真实 IP
    from werkzeug.middleware.proxy_fix import ProxyFix

    assert isinstance(routes.app.wsgi_app, ProxyFix)


def test_forwarded_for_becomes_remote_addr():
    routes.app.config["TESTING"] = True
    seen = {}

    @routes.app.route("/__test_ip")
    def _echo_ip():
        from flask import request

        seen["ip"] = request.remote_addr
        return "ok"

    routes.app.test_client().get(
        "/__test_ip", headers={"X-Forwarded-For": "203.0.113.9"}
    )
    assert seen["ip"] == "203.0.113.9"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_session_security.py -v`
Expected: FAIL，`KeyError: 'SESSION_COOKIE_SECURE'` 或断言不通过

- [ ] **Step 3: 实现**

`web/routes.py` 中，在 `app.secret_key = os.urandom(32)` 之后加入：

```python
# 会话安全。secret_key 每次启动随机是有意的:进程重启后内存里的加密密钥必然丢失、
# 本来就得重新登录输密码,让旧 session 一并失效反而一致。
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # 本地是 http,开了 Secure 浏览器不会回传 cookie,会直接登不进去。
    SESSION_COOKIE_SECURE=SERVER_MODE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

# Caddy 反代后 request.remote_addr 恒为 127.0.0.1,按 IP 的登录限速会退化成全局限速
# (攻击者能借此把正常用户锁在门外)。取 X-Forwarded-For 的最后一跳。
# Flask 只绑回环、只可能被本机 Caddy 访问,该头不可能由外部伪造。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
```

同时在 `web/routes.py` 顶部补两个导入：

```python
from datetime import timedelta
from werkzeug.middleware.proxy_fix import ProxyFix
```

以及把 `from config import DB_PATH, HOST, PORT` 改为：

```python
from config import DB_PATH, HOST, PORT, SERVER_MODE
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_session_security.py -v && pytest -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add web/routes.py tests/test_session_security.py
git commit -m "feat(security): cookie 加 HttpOnly/SameSite、服务器模式加 Secure,反代取真实 IP"
```

---

### Task 5: 登录失败限速

**Files:**
- Modify: `web/routes.py`（新增模块级限速状态与三个函数；改 `login()` 路由，`web/routes.py:218-236`）
- Test: `tests/test_login_rate_limit.py`（新建）

**Interfaces:**
- Consumes: `ProxyFix` 提供的真实 `request.remote_addr`（Task 4）
- Produces:
  - `routes.login_lock_remaining(ip: str, now: float | None = None) -> int` — 剩余锁定秒数，未锁定返回 0
  - `routes.record_login_failure(ip: str, now: float | None = None) -> None`
  - `routes.clear_login_failures(ip: str) -> None`
  - `routes._LOGIN_FAIL_LIMIT = 5`、`routes._LOGIN_LOCK_SEC = 900`、`routes._login_fails: dict`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_login_rate_limit.py`：

```python
"""tests/test_login_rate_limit.py — 登录失败限速(纯逻辑 + 路由集成)。"""

import hashlib

import pytest

import web.routes as routes


@pytest.fixture(autouse=True)
def _clean_state():
    routes._login_fails.clear()
    yield
    routes._login_fails.clear()


class TestPureLogic:
    def test_fresh_ip_not_locked(self):
        assert routes.login_lock_remaining("1.2.3.4", now=1000.0) == 0

    def test_below_limit_not_locked(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT - 1):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        assert routes.login_lock_remaining("1.2.3.4", now=1000.0) == 0

    def test_locks_at_limit(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        assert routes.login_lock_remaining("1.2.3.4", now=1000.0) == routes._LOGIN_LOCK_SEC

    def test_lock_counts_down(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        remaining = routes.login_lock_remaining("1.2.3.4", now=1000.0 + 300)
        assert remaining == routes._LOGIN_LOCK_SEC - 300

    def test_lock_expires_and_counter_resets(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        after = 1000.0 + routes._LOGIN_LOCK_SEC + 1
        assert routes.login_lock_remaining("1.2.3.4", now=after) == 0
        # 到期后计数清零,下一次失败不应立刻再次锁定
        routes.record_login_failure("1.2.3.4", now=after)
        assert routes.login_lock_remaining("1.2.3.4", now=after) == 0

    def test_success_clears(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT - 1):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        routes.clear_login_failures("1.2.3.4")
        routes.record_login_failure("1.2.3.4", now=1000.0)
        assert routes.login_lock_remaining("1.2.3.4", now=1000.0) == 0

    def test_ips_are_independent(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        assert routes.login_lock_remaining("5.6.7.8", now=1000.0) == 0


RIGHT_PASSWORD = "correct-horse-battery"


@pytest.fixture
def client(monkeypatch):
    """把 derive_key 换成快速假实现。

    真 derive_key 是 600k 次 PBKDF2(约 0.5 秒),这组测试要发十几次登录请求,
    用真的会让测试跑十几秒。这里关心的是限速逻辑,不是 KDF 本身
    (KDF 有 tests/test_crypto.py 覆盖)。
    """
    right_hash = hashlib.sha256(b"RIGHT").hexdigest()

    class _DB:
        def get_password(self):
            return right_hash, b"s" * 16

    monkeypatch.setattr(routes, "db", _DB())
    monkeypatch.setattr(
        routes,
        "derive_key",
        lambda pw, salt: b"RIGHT" if pw == RIGHT_PASSWORD else b"WRONG",
    )
    routes.app.config["TESTING"] = True
    yield routes.app.test_client()
    # 登录成功的用例会设进程级密钥,清掉以免污染其他测试
    routes.set_encryption_key(None)


class TestRouteIntegration:
    def _post(self, client, password, ip="203.0.113.7"):
        return client.post(
            "/login",
            data={"password": password},
            headers={"X-Forwarded-For": ip},
            follow_redirects=False,
        )

    def test_failures_counted_per_forwarded_ip(self, client):
        self._post(client, "wrong", ip="203.0.113.7")
        # 计数记在真实 IP 上,而不是反代的 127.0.0.1
        assert "203.0.113.7" in routes._login_fails
        assert "127.0.0.1" not in routes._login_fails

    def test_locked_out_after_limit(self, client):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            self._post(client, "wrong")
        resp = self._post(client, "wrong")
        assert resp.status_code == 200  # 停在登录页
        assert "登录失败次数过多" in resp.get_data(as_text=True)

    def test_correct_password_rejected_while_locked(self, client, monkeypatch):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            self._post(client, "wrong")
        # 锁定期内即使密码正确也不放行,且不应去派生密钥
        called = []
        monkeypatch.setattr(
            routes, "derive_key", lambda p, s: called.append(1) or b"x" * 32
        )
        resp = self._post(client, RIGHT_PASSWORD)
        assert resp.status_code == 200
        assert called == []

    def test_success_clears_counter(self, client, monkeypatch):
        monkeypatch.setattr(routes, "init_manager", lambda m: None)
        monkeypatch.setattr(routes, "EngineManager", lambda db, key: object())
        self._post(client, "wrong")
        assert routes._login_fails
        resp = self._post(client, RIGHT_PASSWORD)
        assert resp.status_code in (301, 302)  # 登录成功,重定向到面板
        assert routes._login_fails == {}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_login_rate_limit.py -v`
Expected: FAIL，`AttributeError: module 'web.routes' has no attribute '_login_fails'`

- [ ] **Step 3: 实现限速逻辑**

在 `web/routes.py` 的 `login_required` 定义之前加入：

```python
# --- 登录限速 ---
# 公网部署后,登录密码是解开所有钱包私钥的唯一屏障。单进程单用户,内存字典足够。
_LOGIN_FAIL_LIMIT = 5  # 连续失败多少次触发锁定
_LOGIN_LOCK_SEC = 900  # 锁定时长(秒)
_login_fails: dict = {}  # ip -> (连续失败次数, 锁定截止时间戳)


def login_lock_remaining(ip, now=None):
    """该 IP 还要锁多少秒;未锁定返回 0。锁定到期时顺手清零计数。"""
    now = time.time() if now is None else now
    count, lock_until = _login_fails.get(ip, (0, 0.0))
    if lock_until and now >= lock_until:
        _login_fails.pop(ip, None)  # 到期后重新计数,而不是"再错一次又锁 15 分钟"
        return 0
    return max(0, int(lock_until - now))


def record_login_failure(ip, now=None):
    """记一次失败;达到上限则开始锁定。"""
    now = time.time() if now is None else now
    count = _login_fails.get(ip, (0, 0.0))[0] + 1
    lock_until = now + _LOGIN_LOCK_SEC if count >= _LOGIN_FAIL_LIMIT else 0.0
    _login_fails[ip] = (count, lock_until)


def clear_login_failures(ip):
    """登录成功后清零。"""
    _login_fails.pop(ip, None)
```

`web/routes.py` 顶部补 `import time`。

- [ ] **Step 4: 接进 `login()` 路由**

把 `login()` 的 POST 分支改成（锁定检查必须在 `derive_key` 之前 —— 那是 600k 次 PBKDF2，很慢，不能让被锁的 IP 还能消耗 CPU）：

```python
@app.route("/login", methods=["GET", "POST"])
def login():
    pw_hash, salt = db.get_password()
    if pw_hash is None:
        return redirect(url_for("setup"))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        wait = login_lock_remaining(ip)
        if wait:
            logger.warning("登录被限速 ip=%s 剩余=%ds", ip, wait)
            flash(f"登录失败次数过多，请 {wait // 60 + 1} 分钟后再试")
            return render_template("login.html")
        password = request.form.get("password", "")
        key = derive_key(password, salt)
        hashed = hashlib.sha256(key).hexdigest()
        if hashed == pw_hash:
            clear_login_failures(ip)
            set_encryption_key(key)
            global manager
            if manager is None:
                mgr = EngineManager(db, key)
                init_manager(mgr)
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        record_login_failure(ip)
        logger.warning("登录密码错误 ip=%s", ip)
        flash("密码错误")
    return render_template("login.html")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_login_rate_limit.py -v && pytest -q`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add web/routes.py tests/test_login_rate_limit.py
git commit -m "feat(security): 登录失败限速——同 IP 连错 5 次锁 15 分钟"
```

---

### Task 6: 新安装密码最短 12 位

项目没有改密码功能，所以这只影响全新 setup。

**Files:**
- Modify: `web/routes.py:189-215`（`setup()`）
- Modify: `web/templates/setup.html`（提示文案）
- Test: `tests/test_setup_password.py`（新建）

**Interfaces:**
- Produces: `routes._MIN_PASSWORD_LEN = 12`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_setup_password.py`：

```python
"""tests/test_setup_password.py — 首次设置密码的强度要求。"""

import pytest

import web.routes as routes


class _EmptyDB:
    """还没设过密码的库。save_password 记录调用。"""

    def __init__(self):
        self.saved = []

    def get_password(self):
        return None, None

    def save_password(self, hashed, salt):
        self.saved.append((hashed, salt))


@pytest.fixture
def db(monkeypatch):
    d = _EmptyDB()
    monkeypatch.setattr(routes, "db", d)
    routes.app.config["TESTING"] = True
    yield d
    # 设置成功的用例会设进程级密钥,清掉以免污染其他测试
    routes.set_encryption_key(None)


def _post(pw, confirm=None):
    return routes.app.test_client().post(
        "/setup", data={"password": pw, "confirm": confirm if confirm is not None else pw}
    )


def test_min_length_is_12():
    assert routes._MIN_PASSWORD_LEN == 12


def test_rejects_short_password(db):
    resp = _post("shortpw12")  # 9 位
    assert resp.status_code == 200
    assert "12" in resp.get_data(as_text=True)
    assert db.saved == []


def test_rejects_exactly_11(db):
    _post("a" * 11)
    assert db.saved == []


def test_accepts_12(db, monkeypatch):
    monkeypatch.setattr(routes, "init_manager", lambda m: None)
    monkeypatch.setattr(routes, "EngineManager", lambda d, key: object())
    resp = _post("a" * 12)
    assert resp.status_code in (301, 302)
    assert len(db.saved) == 1


def test_mismatch_still_rejected(db):
    _post("a" * 12, confirm="b" * 12)
    assert db.saved == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_setup_password.py -v`
Expected: FAIL，`AttributeError: ... '_MIN_PASSWORD_LEN'`；`test_rejects_short_password` 也会失败（9 位当前会被接受）

- [ ] **Step 3: 实现**

在 `web/routes.py` 的限速常量附近加：

```python
# 这个密码同时是解开所有钱包私钥的加密密钥。公网部署后弱口令的代价太大,
# 首次设置强制 12 位起。项目没有改密码功能,故只影响全新安装。
_MIN_PASSWORD_LEN = 12
```

把 `setup()` 中的长度检查改为：

```python
        if len(password) < _MIN_PASSWORD_LEN:
            flash(f"密码至少{_MIN_PASSWORD_LEN}个字符")
            return render_template("setup.html")
```

- [ ] **Step 4: 同步页面文案**

`web/templates/setup.html` 中若有"至少6个字符"之类的提示或 `minlength="6"` 属性，改为 12。先确认：

Run: `grep -n "6\|minlength" web/templates/setup.html`

把所有指代密码长度 6 的地方改成 12。

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_setup_password.py -v && pytest -q`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add web/routes.py web/templates/setup.html tests/test_setup_password.py
git commit -m "feat(security): 首次设置密码最短提到 12 位"
```

---

### Task 7: 服务器模式的 git 更新

**Files:**
- Modify: `web/update.py`（`check_update`、`start_update`，新增 `_run_git_update`、`_run_cmd`、`_repo_dir`）
- Test: `tests/test_update_server.py`（新建）

**Interfaces:**
- Consumes: `config.SERVER_MODE`（Task 2）；现有的 `engine_active(mgr)`、`STATE`、`parse_release`、`_shutdown_self`
- Produces:
  - `update._repo_dir() -> str`
  - `update._run_cmd(args: list[str], cwd: str) -> tuple[int, str]`
  - `update._run_git_update(state, info: dict, repo_dir: str, *, run_cmd, shutdown) -> None`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_update_server.py`：

```python
"""tests/test_update_server.py — 服务器模式更新(git 同步,全离线)。"""

import threading

import pytest

import web.update as updater
from web.update import _State, _run_git_update, check_update, start_update


class _FakeRunner:
    """把 (args, cwd) 记下来,按预设返回码应答。"""

    def __init__(self, failures=None):
        # failures: {命令中的关键字: (返回码, 输出)}
        self.failures = failures or {}
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append(args)
        for keyword, result in self.failures.items():
            if keyword in args:
                return result
        if args[:2] == ["git", "rev-parse"]:
            return 0, "oldcommit123\n"
        return 0, ""

    def git_args(self):
        return [a for a in self.calls if a and a[0] == "git"]


INFO = {"tag": "v9.9.9", "version": "9.9.9"}


class TestHappyPath:
    def _run(self):
        state, runner, exited = _State(), _FakeRunner(), []
        _run_git_update(
            state, INFO, "/repo", run_cmd=runner, shutdown=lambda: exited.append(1)
        )
        return state, runner, exited

    def test_command_sequence(self):
        _, runner, _ = self._run()
        assert runner.calls[0] == ["git", "rev-parse", "HEAD"]
        assert runner.calls[1] == ["git", "fetch", "--tags", "origin"]
        assert runner.calls[2] == ["git", "reset", "--hard", "v9.9.9"]
        assert "pip" in runner.calls[3]
        assert "install" in runner.calls[3]

    def test_exits_for_systemd_to_restart(self):
        _, _, exited = self._run()
        assert exited == [1]

    def test_no_error_state(self):
        state, _, _ = self._run()
        assert state.state != "error"


class TestFailureRollback:
    def test_pip_failure_rolls_back_and_keeps_running(self):
        state, exited = _State(), []
        runner = _FakeRunner(failures={"install": (1, "No matching distribution")})
        _run_git_update(
            state, INFO, "/repo", run_cmd=runner, shutdown=lambda: exited.append(1)
        )
        # 回滚到更新前的 commit
        assert ["git", "reset", "--hard", "oldcommit123"] in runner.git_args()
        assert state.state == "error"
        assert "回滚" in state.message
        # 关键:不退出进程,旧版本继续跑
        assert exited == []

    def test_fetch_failure_stops_before_reset(self):
        state, exited = _State(), []
        runner = _FakeRunner(failures={"fetch": (1, "could not resolve host")})
        _run_git_update(
            state, INFO, "/repo", run_cmd=runner, shutdown=lambda: exited.append(1)
        )
        assert not any("reset" in a for a in runner.git_args())
        assert state.state == "error"
        assert exited == []

    def test_reset_failure_stops_before_pip(self):
        state, exited = _State(), []
        runner = _FakeRunner(failures={"reset": (1, "unknown revision")})
        _run_git_update(
            state, INFO, "/repo", run_cmd=runner, shutdown=lambda: exited.append(1)
        )
        assert not any("pip" in a for a in runner.calls)
        assert state.state == "error"
        assert exited == []

    def test_unexpected_exception_is_caught(self):
        state, exited = _State(), []

        def boom(args, cwd):
            raise RuntimeError("炸了")

        _run_git_update(
            state, INFO, "/repo", run_cmd=boom, shutdown=lambda: exited.append(1)
        )
        assert state.state == "error"
        assert exited == []


class TestStartUpdateDispatch:
    @pytest.fixture(autouse=True)
    def _reset(self):
        updater.STATE.state = "idle"
        yield
        updater.STATE.state = "idle"

    def test_server_mode_uses_git_path(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", True)
        seen, done = {}, threading.Event()

        def _fake(state, info, repo_dir, **kw):
            seen["info"], seen["repo"] = info, repo_dir
            done.set()

        # 不要去 patch updater.threading.Thread —— 那是全局 threading 模块,
        # 改它会影响整个进程。让真线程跑,用 Event 等它。
        monkeypatch.setattr(updater, "_run_git_update", _fake)
        result = start_update(None, info_provider=lambda: INFO)
        assert result["ok"] is True
        assert done.wait(5), "更新线程未在 5 秒内启动"
        assert seen["info"]["tag"] == "v9.9.9"

    def test_engine_running_blocks_update(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", True)
        monkeypatch.setattr(updater, "engine_active", lambda mgr: True)
        called = []
        monkeypatch.setattr(
            updater, "_run_git_update", lambda *a, **k: called.append(1)
        )
        result = start_update(object(), info_provider=lambda: INFO)
        assert result["ok"] is False
        assert "停止引擎" in result["message"]
        assert called == []

    def test_missing_tag_reports_error(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", True)
        result = start_update(None, info_provider=lambda: None)
        assert result["ok"] is False


class TestCheckUpdateInServerMode:
    @pytest.fixture(autouse=True)
    def _clear(self):
        updater._reset_cache()
        yield
        updater._reset_cache()

    def _release(self, tag):
        # 没有任何 Linux 安装包 —— 服务器模式不该依赖 asset
        return {"tag_name": tag, "body": "", "assets": []}

    def test_server_mode_ignores_missing_asset(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", True)
        r = check_update(current="1.0.0", fetch=lambda: self._release("v1.0.1"))
        assert r["update_available"] is True

    def test_server_mode_still_respects_version(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", True)
        r = check_update(current="1.0.1", fetch=lambda: self._release("v1.0.1"))
        assert r["update_available"] is False

    def test_local_mode_still_requires_asset(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", False)
        r = check_update(current="1.0.0", fetch=lambda: self._release("v1.0.1"))
        assert r["update_available"] is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_update_server.py -v`
Expected: FAIL，`ImportError: cannot import name '_run_git_update'`

- [ ] **Step 3: 实现 git 更新**

在 `web/update.py` 顶部导入处加 `from config import SERVER_MODE`（`subprocess`、`sys`、`os`、`threading` 已在导入列表中）。

在 `_shutdown_self` 之后加入：

```python
def _repo_dir():
    """仓库根目录(web/ 的上一级)。服务器模式是 git clone 出来的源码目录。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_cmd(args, cwd):
    """在 cwd 里执行命令,返回 (返回码, stdout+stderr)。"""
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=600)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _run_git_update(state, info, repo_dir, *, run_cmd, shutdown):
    """服务器模式更新:同步到 release tag -> 装依赖 -> 退出,由 systemd 拉起新代码。

    任一步失败都回滚到更新前的 commit、置 error、**不退出进程**,让当前能跑的版本
    继续跑。副作用(执行命令、退出进程)经参数注入,便于离线单测。

    不做 sha256 校验:原流程校验是因为要下载并执行二进制安装包,而 git fetch 走
    HTTPS 且校验 GitHub 证书,完整性已经具备。
    git reset --hard 不影响 untracked 文件,market_maker.db 在 .gitignore 里。
    """
    try:
        state.state, state.percent, state.message = "downloading", 10, "读取当前版本"
        rc, out = run_cmd(["git", "rev-parse", "HEAD"], repo_dir)
        if rc != 0:
            state.state, state.message = "error", f"读取当前版本失败:{out.strip()}"
            return
        old_commit = out.strip()

        state.percent, state.message = 30, "拉取新版本"
        rc, out = run_cmd(["git", "fetch", "--tags", "origin"], repo_dir)
        if rc != 0:
            state.state, state.message = "error", f"拉取失败:{out.strip()}"
            return

        state.percent, state.message = 50, "切换到新版本"
        rc, out = run_cmd(["git", "reset", "--hard", info["tag"]], repo_dir)
        if rc != 0:
            state.state, state.message = "error", f"切换版本失败:{out.strip()}"
            return

        state.state, state.percent, state.message = "installing", 70, "安装依赖"
        rc, out = run_cmd(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], repo_dir
        )
        if rc != 0:
            run_cmd(["git", "reset", "--hard", old_commit], repo_dir)
            state.state = "error"
            state.message = f"安装依赖失败,已回滚到原版本:{out.strip()[:300]}"
            return

        state.percent, state.message = 100, "重启中"
        logger.info("更新完成,退出进程交由 systemd 重启")
        shutdown()
    except Exception as e:  # noqa: BLE001
        logger.exception("更新失败")
        state.state, state.message = "error", f"更新失败:{e}"
```

- [ ] **Step 4: 在 `start_update` 里分派**

`start_update` 现有的 `engine_active` 闸和"更新已在进行中"判断保持不动，在它们之后、`info = ...` 之前插入服务器模式分支：

```python
    if SERVER_MODE:
        info = (info_provider or (lambda: parse_release(_fetch_latest_release())))()
        if not info or not info.get("tag"):
            return {"ok": False, "message": "未获取到最新版本信息"}
        deps.setdefault("run_cmd", _run_cmd)
        deps.setdefault("shutdown", _shutdown_self)
        STATE.state, STATE.percent, STATE.message = "downloading", 0, ""
        threading.Thread(
            target=_run_git_update,
            args=(STATE, info, _repo_dir()),
            kwargs=deps,
            daemon=True,
        ).start()
        return {"ok": True}
```

- [ ] **Step 5: 让 `check_update` 在服务器模式下不看 asset**

把 `check_update` 里这一行：

```python
    available = bool(info["pkg_url"]) and is_newer(info["version"], current)
```

改为：

```python
    # 服务器模式走 git,不需要 release 里有本平台的安装包,只比版本号。
    has_package = SERVER_MODE or bool(info["pkg_url"])
    available = has_package and is_newer(info["version"], current)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest tests/test_update_server.py tests/test_update.py tests/test_update_routes.py -v`
Expected: 全部 PASS

Run: `pytest -q`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add web/update.py tests/test_update_server.py
git commit -m "feat(deploy): 服务器模式更新改走 git 同步 + 自重启,失败自动回滚"
```

---

### Task 8: 部署产物

**Files:**
- Create: `deploy/pmm.service`
- Create: `deploy/Caddyfile`
- Create: `deploy/install.sh`
- Create: `deploy/README.md`

**Interfaces:**
- Consumes: `PMM_SERVER=1`（Task 2）、端口 `8765`（`config.PORT`）
- Produces: 无代码接口

这一整块没有自动化测试（shell / 配置文件），靠部署时实际运行验证 —— 见 Task 9 的验收清单。

- [ ] **Step 1: 写 systemd unit**

创建 `deploy/pmm.service`：

```ini
[Unit]
Description=Polymarket Market Maker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pmm
Group=pmm
WorkingDirectory=/opt/pmm/poly-marketmaker
Environment=PMM_SERVER=1
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/pmm/venv/bin/python app.py
# 崩溃后自动拉起,同时也是网页「更新」按钮的实现基础:
# 更新流程结束时进程主动退出,由这里把新代码拉起来。
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: 写 Caddyfile**

创建 `deploy/Caddyfile`（部署时把 `YOUR_DOMAIN` 替换成真实域名）：

```
YOUR_DOMAIN {
	encode gzip

	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "no-referrer"
	}

	# 应用只绑 127.0.0.1,公网唯一入口就是这里。
	reverse_proxy 127.0.0.1:8765
}
```

Caddy 会自动申请并续期 Let's Encrypt 证书，也会自动把 http 跳转到 https，不用额外配置。

- [ ] **Step 3: 写安装脚本**

创建 `deploy/install.sh`：

```bash
#!/usr/bin/env bash
# deploy/install.sh — 在一台干净的 Debian/Ubuntu VPS 上部署本程序。
# 用法(需要 root):
#   bash install.sh your-domain.com
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
	echo "用法: bash install.sh <域名>" >&2
	exit 1
fi

REPO="https://github.com/VMInsideVM/poly-marketmaker.git"
BASE=/opt/pmm
APP="$BASE/poly-marketmaker"

echo "==> 设置时区为 Asia/Shanghai"
# 每日盈亏台账、周报、监控 watermark 都按本地时间算,留在 UTC 会导致日期错位。
timedatectl set-timezone Asia/Shanghai

echo "==> 安装系统依赖"
apt-get update
apt-get install -y python3 python3-venv python3-pip git curl ufw \
	debian-keyring debian-archive-keyring apt-transport-https

echo "==> 创建服务用户 pmm"
id -u pmm >/dev/null 2>&1 || useradd --system --create-home --home-dir "$BASE" --shell /usr/sbin/nologin pmm
mkdir -p "$BASE"
chown -R pmm:pmm "$BASE"

echo "==> 克隆代码"
# 必须是 git clone(而不是下载 zip):网页上的「更新」按钮靠 git fetch/reset 工作。
if [ ! -d "$APP/.git" ]; then
	sudo -u pmm git clone "$REPO" "$APP"
fi

echo "==> 建虚拟环境、装 Python 依赖"
sudo -u pmm python3 -m venv "$BASE/venv"
sudo -u pmm "$BASE/venv/bin/pip" install --upgrade pip
sudo -u pmm "$BASE/venv/bin/pip" install -r "$APP/requirements.txt"

echo "==> 安装 Caddy"
if ! command -v caddy >/dev/null 2>&1; then
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' |
		gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' |
		tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
	apt-get update
	apt-get install -y caddy
fi

echo "==> 配置 Caddy(域名: $DOMAIN)"
sed "s/YOUR_DOMAIN/$DOMAIN/" "$APP/deploy/Caddyfile" >/etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo "==> 安装 systemd 服务"
cp "$APP/deploy/pmm.service" /etc/systemd/system/pmm.service
systemctl daemon-reload
systemctl enable --now pmm

echo "==> 配置防火墙"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo
echo "完成。打开 https://$DOMAIN 首次设置密码。"
echo "查看服务日志: journalctl -u pmm -f"
```

- [ ] **Step 4: 写部署说明**

创建 `deploy/README.md`：

````markdown
# VPS 部署说明

把这个程序部署到一台 Linux VPS，通过域名 + HTTPS 远程使用。
设计背景见 `docs/superpowers/specs/2026-07-27-vps-deployment-design.md`。

## 前提

- 一台 Debian 12 或 Ubuntu 22.04+ 的 VPS，有 root
- 一个域名，A 记录已经指向这台 VPS 的 IPv4 地址（必须先解析生效，
  Caddy 申请证书时要用它验证域名归属）

## 部署

```bash
ssh root@<你的VPS地址>
git clone https://github.com/VMInsideVM/poly-marketmaker.git /tmp/pmm-src
bash /tmp/pmm-src/deploy/install.sh your-domain.com
```

脚本会：设时区为北京时间、建 `pmm` 服务用户、克隆代码到 `/opt/pmm/poly-marketmaker`、
建虚拟环境装依赖、安装并配置 Caddy、注册 systemd 服务、开启 ufw（只放行 22/80/443）。

完成后打开 `https://your-domain.com`，首次访问会进入设置页要求设定密码。

## 必做的 SSH 加固

私钥托管在这台机器上，SSH 弱口令等于把私钥送人。部署完立刻改
`/etc/ssh/sshd_config`：

```
PasswordAuthentication no
PermitRootLogin prohibit-password
```

然后 `systemctl restart sshd`。改之前先确认自己的公钥已经在
`~/.ssh/authorized_keys` 里，否则会把自己锁在外面。

## 日常运维

```bash
systemctl status pmm          # 看服务状态
journalctl -u pmm -f          # 看实时日志
systemctl restart pmm         # 重启(重启后需要重新登录网页)
```

**重启后必须有人登录网页。** 钱包私钥是用登录密码派生的密钥加密的，密钥只存在内存里，
进程一重启就没了。所以引擎不会自动恢复，得有人打开网页输密码、再手动启动引擎。
这是有意的设计——把密码存在服务器上就等于取消了加密。

## 更新

网页上有「更新」按钮，会 `git fetch` 到最新的 release tag、装依赖、然后退出进程，
由 systemd 用新代码拉起来。

- 引擎运行中点更新会被拒绝（更新要中断做市，持仓会失去止损保护），先停引擎。
- 任何一步失败都会自动回滚到原来的版本，进程继续跑，不会把服务搞挂。
- 万一某个新版本有启动期 bug 导致进程起不来，网页就打不开了，需要 SSH 上去手工回退：

```bash
cd /opt/pmm/poly-marketmaker
sudo -u pmm git reset --hard <上一个可用的 tag>
systemctl restart pmm
```

## 备份

`/opt/pmm/poly-marketmaker/market_maker.db` 存着加密后的私钥和全部历史数据。
它不在 git 里，更新不会动它。要备份就备份这个文件，但注意它只是密文——
没有登录密码解不开，所以密码得另外记牢。
````

- [ ] **Step 5: 给脚本加执行位并做语法检查**

```bash
chmod +x deploy/install.sh
bash -n deploy/install.sh
```

Expected: 无输出（语法正确）

- [ ] **Step 6: 提交**

```bash
git add deploy/pmm.service deploy/Caddyfile deploy/install.sh deploy/README.md
git commit -m "feat(deploy): 新增 VPS 部署产物——systemd/Caddy/安装脚本/说明"
```

---

### Task 9: 版本号、发布说明与实机验收

**Files:**
- Modify: `version.py`
- Modify: `RELEASE_NOTES.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: 前八个任务的全部改动

按 `docs/版本号规范.md`：本次不改策略/订单逻辑、不破坏数据格式，是新增部署能力 + 安全加固，属 MINOR → `8.2.0`。

- [ ] **Step 1: 提版本号**

`version.py` 改为：

```python
__version__ = "8.2.0"
```

- [ ] **Step 2: 写发布说明**

在 `RELEASE_NOTES.md` 顶部插入（沿用现有的面向非技术用户的中文风格）：

```markdown
## v8.2.0 · 可以部署到服务器上跑了

日常在自己电脑上用的话，这一版跟你没什么关系，两个小改动写在最后。

### 新增：部署到 Linux 服务器

现在可以把程序装到一台 Linux VPS 上，通过自己的域名用 https 访问，不用自己电脑一直开着。
部署步骤见 `deploy/README.md`。

几点先说清楚：

- **必须配域名并走 https。** 用裸 IP 加 http 访问的话，私钥在提交时是明文穿过公网的，
  中间任何一个节点都能抓到。部署脚本会自动申请证书，你只要把域名解析指过来。
- **进程重启后要重新登录。** 私钥是用登录密码加密的，密钥只存在内存里，重启就没了，
  引擎不会自动恢复。这是有意的——把密码存在服务器上等于取消加密。
- **更新按钮在服务器上照常能用**，点一下会拉最新版本并自动重启。失败会自动回滚，
  不会把服务搞挂。跟本地一样，引擎运行中不让更新。

### 两个安全改动（本地版也生效）

- 「检查更新 / 应用更新」这几个接口以前不需要登录就能调用。本地只监听自己电脑时没影响，
  但既然现在能部署到公网，这里补上了登录检查。你不会有任何感知。
- **新安装时密码最短要求从 6 位提到 12 位。** 已经设过密码的不受影响，程序没有改密码的功能。
```

- [ ] **Step 3: 更新 CLAUDE.md**

在 `CLAUDE.md` 的 "Commands" 一节后面加一段（保持文件既有的英文技术描述风格）：

```markdown
## Server deployment

The app can also run on a Linux VPS behind a domain + HTTPS (`deploy/README.md`, design in
`docs/superpowers/specs/2026-07-27-vps-deployment-design.md`). A single env var,
`PMM_SERVER=1`, switches on all server-only behavior: no `webbrowser.open`, fixed port (no
`pick_port` fallback — the reverse proxy hardcodes it), waitress instead of the Flask dev
server, and a git-based update path instead of downloading a `.exe`/`.dmg`. **It must stay
single-process** — `web/routes.py` holds `db`/`manager`/`encryption_key` as module globals and
the engine runs as in-process threads, so a multi-worker WSGI server would run two engines
over the same wallets. Flask still binds `127.0.0.1` only; Caddy terminates TLS and proxies to
it. `ProxyFix(x_for=1)` recovers the real client IP for login rate limiting (5 failures per IP
→ 15 min lock) — without it every request looks like `127.0.0.1` and the limiter degrades into
a global lock anyone can trip. The server-mode update (`_run_git_update` in `web/update.py`)
syncs to the release tag, installs deps, then exits for systemd to restart; any failure rolls
back to the pre-update commit and leaves the process running.
```

- [ ] **Step 4: 全量测试**

Run: `pytest -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add version.py RELEASE_NOTES.md CLAUDE.md
git commit -m "chore(release): v8.2.0——可部署到 Linux 服务器 + 登录限速与鉴权加固"
```

- [ ] **Step 6: 实机验收（在真实 VPS 上手工执行）**

按 spec 的验收标准逐条核对：

1. 浏览器访问 `https://<域名>` 打开登录页，证书有效；`http://<域名>` 自动跳转到 https
2. 从外部机器访问 `http://<VPS IP>:8765` 不通（`curl --connect-timeout 5 http://<IP>:8765` 应超时）
3. 全新 setup 时输 11 位密码被拒绝，12 位通过
4. 连续 5 次错误密码后被锁定；`journalctl -u pmm` 里记录的是真实客户端 IP，不是 `127.0.0.1`
5. 退出登录后 `curl -X POST https://<域名>/api/update/apply` 不触发更新（返回重定向）
6. 停止引擎后点网页更新按钮，进程自动重启到新版本，钱包和历史数据都还在
7. 引擎运行中点更新按钮，被拒绝并提示先停止引擎
8. `systemctl restart pmm` 后服务自动起来，停在登录页；登录后引擎能正常启动
9. VPS 上 `date` 显示北京时间

- [ ] **Step 7: 发布**

版本发布仍按 `docs/版本号规范.md` 走 `release.ps1`（在 Windows 上执行，会构建安装包、打 tag、发 GitHub Release）。服务器端的更新按钮依赖这个 tag 存在。
