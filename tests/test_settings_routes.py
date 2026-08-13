"""tests/test_settings_routes.py — /api/settings GET/POST 的 v4 参数 round-trip 契约。

路由本就按键归类(引擎键->settings、策略键->默认模板)且 json.dumps 存值,故对
现有代码即通过。本测试把这一契约钉死,作为 SP6a 前端依赖的回归闸。"""

import web.routes as routes
from models.database import Database


def _client_with_db(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client, db


def test_get_settings_returns_v4_params(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    data = client.get("/api/settings").get_json()
    for k in (
        "theta_loss_cents",
        "theta_stop_cents",
        "case_a_mode",
        "included_categories",
        "include_other",
        "max_exposure_usd",
        "max_exposure_shares",
        "max_concurrent_markets",
        "discovery_interval_sec",
    ):
        assert k in data, f"GET /api/settings 缺 {k}"


def test_post_settings_roundtrips_structured(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    payload = {
        "theta_loss_cents": 3,
        "case_a_mode": "market",
        "included_categories": ["politics"],
        "include_other": False,
        "discovery_interval_sec": 7200,
    }
    resp = client.post("/api/settings", json=payload)
    assert resp.status_code == 200
    tmpl = db.get_template(db.get_default_template_id())
    assert tmpl["theta_loss_cents"] == 3
    assert tmpl["case_a_mode"] == "market"
    assert tmpl["included_categories"] == ["politics"]
    assert tmpl["include_other"] is False
    assert db.get_settings()["discovery_interval_sec"] == 7200


def test_post_settings_routes_engine_vs_template(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    client.post("/api/settings", json={"scan_interval_sec": 45, "max_exposure_usd": 99})
    assert db.get_settings()["scan_interval_sec"] == 45
    assert db.get_template(db.get_default_template_id())["max_exposure_usd"] == 99
    assert "scan_interval_sec" not in db.get_template(db.get_default_template_id())
    assert "max_exposure_usd" not in db.get_settings()


def test_post_settings_roundtrips_gap_single_keys(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    payload = {
        "gap_wide_cents": 10,
        "gap_mid_cents": 5,
        "take_profit_mode": "market",
        "stop_loss_mode": "off",
    }
    resp = client.post("/api/settings", json=payload)
    assert resp.status_code == 200
    tmpl = db.get_template(db.get_default_template_id())
    assert tmpl["gap_wide_cents"] == 10
    assert tmpl["gap_mid_cents"] == 5
    assert tmpl["take_profit_mode"] == "market"
    assert tmpl["stop_loss_mode"] == "off"


def test_reward_scan_max_pages_default_and_roundtrip(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    # 默认值出现在 GET
    assert client.get("/api/settings").get_json()["reward_scan_max_pages"] == 20
    # POST 存 + 回读
    resp = client.post("/api/settings", json={"reward_scan_max_pages": 12})
    assert resp.status_code == 200
    assert db.get_settings()["reward_scan_max_pages"] == 12
    # 属引擎键,不落默认模板
    assert "reward_scan_max_pages" not in db.get_template(db.get_default_template_id())


def _tier_payload(**over):
    t = {
        "size": 20,
        "enabled": True,
        "shares": 40,
        "rule1_min_coeff": 0,
        "rule2_min_coeff": 0,
        "rule3_min_coeff": 0,
        "gap_high_coeff_sum_min": 20,
        "amount_value_table": [{"upper": 0.2, "value": 1}],
    }
    t.update(over)
    return t


def test_size_tiers_roundtrip_via_template_put(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    r = client.put(f"/api/templates/{tid}", json={"size_tiers": [_tier_payload()]})
    assert r.status_code == 200
    saved = db.get_template(tid)["size_tiers"]
    assert saved[0]["size"] == 20 and saved[0]["shares"] == 40


def test_size_tiers_invalid_rejected_400(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    # shares < size
    r = client.put(
        f"/api/templates/{tid}", json={"size_tiers": [_tier_payload(shares=10)]}
    )
    assert r.status_code == 400 and "挂单份数" in r.get_json()["error"]
    # size 重复
    r = client.put(
        f"/api/templates/{tid}",
        json={"size_tiers": [_tier_payload(), _tier_payload()]},
    )
    assert r.status_code == 400 and "重复" in r.get_json()["error"]
    # /api/settings 同样拦
    r = client.post("/api/settings", json={"size_tiers": [_tier_payload(shares=1)]})
    assert r.status_code == 400


def test_dead_template_keys_no_longer_stored(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    client.post("/api/settings", json={"rule1_min_coeff": 9, "amount_value_table": []})
    tmpl = db.get_template(db.get_default_template_id())
    assert "rule1_min_coeff" not in tmpl and "amount_value_table" not in tmpl


def test_settings_endpoint_rejects_invalid_merge_numeric_without_partial_save(
    tmp_path, monkeypatch
):
    client, db = _client_with_db(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    original_exposure = db.get_template(tid)["max_exposure_usd"]

    response = client.post(
        "/api/settings",
        json={"max_exposure_usd": 999, "merge_min_shares": None},
    )

    assert response.status_code == 400
    template = db.get_template(tid)
    assert template["merge_min_shares"] == 1.0
    assert template["max_exposure_usd"] == original_exposure
