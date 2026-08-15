"""tests/test_no_telemetry.py — 回归/安全:自更新与作者周报外推必须保持不存在。

运行时应用绝不允许:
- 下载自己的 Release / 启动安装器 / git reset / pull 自己
- 把钱包地址、PnL、余额、交易统计发给作者控制的第三方服务(REPORT_URL 等)

这些用例扫**运行时代码目录**(config.py/app.py/api/engine/models/utils/web/deploy),
不扫 docs/ 与根目录文档(那里允许用 Git 术语讨论手动升级流程)。
"""

import importlib
import io
import os
import sys

import pytest

RUNTIME_DIRS = ("config.py", "app.py", "api", "engine", "models", "utils", "web", "deploy")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _runtime_files():
    for name in RUNTIME_DIRS:
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            yield p
        elif os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
                for fn in filenames:
                    if fn.endswith((".py", ".html", ".js", ".sh", ".json")):
                        yield os.path.join(dirpath, fn)


def _runtime_text():
    out = []
    for p in _runtime_files():
        try:
            out.append((p, io.open(p, encoding="utf-8").read()))
        except Exception:
            pass
    return out


FORBIDDEN_STRINGS = (
    "REPORT_URL",
    "REPORT_KEY",
    "PUSH_HOUR",
    "send_report",
    "build_report_payload",
    "workers.dev",
    "releases/latest",
    "api.github.com/repos",
    "checkForUpdate",
    "_update_modal",
    "/api/update",
    "git reset --hard",
    "git pull",
)


def test_updater_and_report_destinations_absent_from_runtime():
    hits = []
    for p, text in _runtime_text():
        for s in FORBIDDEN_STRINGS:
            if s in text:
                hits.append((p, s))
    assert hits == [], f"运行时代码中发现了已移除功能: {hits}"


def test_updater_modules_cannot_be_imported():
    for mod in ("web.update", "engine.notify"):
        with pytest.raises(ImportError):
            importlib.import_module(mod)


def test_update_api_routes_return_404():
    import web.routes as routes
    from models.database import Database
    import tempfile

    db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
    db.init()
    routes.db = db
    routes.manager = None
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    for url in ("/api/update/check", "/api/update/apply", "/api/update/status"):
        r = client.get(url)
        assert r.status_code == 404, f"{url} 应已移除(404),实际 {r.status_code}"
    db.close()


def test_sidebar_and_login_have_no_update_links():
    for tmpl in ("base.html", "login.html", "setup.html"):
        p = os.path.join(ROOT, "web", "templates", tmpl)
        text = io.open(p, encoding="utf-8").read()
        assert "检查更新" not in text, tmpl
        assert "checkForUpdate" not in text, tmpl
        assert "_update_modal" not in text, tmpl


def test_manager_has_no_weekly_push_path():
    import engine.manager as m

    src = io.open(os.path.join(ROOT, "engine", "manager.py"), encoding="utf-8").read()
    assert "_maybe_push_weekly" not in src
    assert "_send_report" not in src
    assert "get_last_push_week" not in src


def test_database_has_no_last_push_week_persistence():
    import models.database as dbmod

    src = io.open(os.path.join(ROOT, "models", "database.py"), encoding="utf-8").read()
    assert "last_push_week" not in src


def test_config_has_no_report_constants():
    import config

    assert not hasattr(config, "REPORT_URL")
    assert not hasattr(config, "REPORT_KEY")
    assert not hasattr(config, "PUSH_HOUR")


def test_relayer_status_api_never_exposes_secrets(monkeypatch):
    """UI/API 只显示 已配置/未配置;密钥绝不进响应。"""
    import web.routes as routes
    from models.database import Database
    import tempfile

    db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
    db.init()
    routes.db = db
    routes.manager = None
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    secret = "top-secret-key-value-12345"
    monkeypatch.setenv("POLY_BUILDER_API_KEY", secret)
    monkeypatch.setenv("POLY_BUILDER_SECRET", "s")
    monkeypatch.setenv("POLY_BUILDER_PASSPHRASE", "p")
    body = client.get("/api/relayer/status").get_data(as_text=True)
    assert body == '{"configured": true}' or "configured" in body
    assert secret not in body
    assert "POLY_BUILDER" not in body
    db.close()


def test_version_docstring_no_update_reference():
    src = io.open(os.path.join(ROOT, "version.py"), encoding="utf-8").read()
    assert "update.py" not in src


def test_deploy_scripts_no_self_update():
    for p, text in _runtime_text():
        if not p.endswith(".sh"):
            continue
        assert "git fetch --tags" not in text or "git reset" not in text, p
