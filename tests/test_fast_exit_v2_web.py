"""V2 Fast-Exit web/config parity tests (spec §29, §47, §48)."""

import math
import time

import pytest

import web.routes as routes
from engine.inventory_exit import exit_method_label
from models.database import Database


def _client(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client, db


# --- §47: config page labels ------------------------------------------------


def test_config_page_contains_fast_exit_labels(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    html = client.get("/config").get_data(as_text=True)
    for label in (
        "成交后库存处理",
        "启用成交后快速库存退出",
        "启用自动 Merge",
        "最小 Merge 份数",
        "Maker 快速退出等待",
        "补边 Merge 最低优势",
        "Merge 未就绪时暂停新开仓",
        "紧急退出阈值",
    ):
        assert label in html, label


def test_config_page_fast_exit_inputs_are_boolean_and_numeric(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    html = client.get("/config").get_data(as_text=True)
    assert 'type="checkbox" name="fast_exit_enabled"' in html
    assert 'type="checkbox" name="require_merge_ready_for_new_buys"' in html
    field = html.split('name="maker_exit_wait_sec"', 1)[1].split(">", 1)[0]
    assert 'min="0"' in field
    assert "required" in field
    assert "Number.isFinite(data[key])" in html
    assert "data[key] < 0" in html
    # load + save wiring for every V2 checkbox
    for name in ("fast_exit_enabled", "require_merge_ready_for_new_buys", "merge_enabled"):
        assert f'name="{name}"' in html
        assert "input.checked = !!data[key]" in html
        assert f"data.{name} = !!(" in html


# --- §47: persistence and validation -----------------------------------------


def test_fast_exit_booleans_true_false_true_roundtrip(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    client.put(
        f"/api/templates/{tid}",
        json={"fast_exit_enabled": True, "require_merge_ready_for_new_buys": True},
    )
    assert db.get_template(tid)["fast_exit_enabled"] is True
    client.put(
        f"/api/templates/{tid}",
        json={"fast_exit_enabled": False, "require_merge_ready_for_new_buys": False},
    )
    saved = db.get_template(tid)
    assert saved["fast_exit_enabled"] is False
    assert saved["require_merge_ready_for_new_buys"] is False
    client.put(
        f"/api/templates/{tid}",
        json={"fast_exit_enabled": True, "require_merge_ready_for_new_buys": True},
    )
    saved = db.get_template(tid)
    assert saved["fast_exit_enabled"] is True
    assert saved["require_merge_ready_for_new_buys"] is True


@pytest.mark.parametrize("value", [0, 1, 5, 30, 120])
def test_maker_exit_wait_sec_valid_values(tmp_path, monkeypatch, value):
    client, db = _client(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    resp = client.put(f"/api/templates/{tid}", json={"maker_exit_wait_sec": value})
    assert resp.status_code == 200
    assert db.get_template(tid)["maker_exit_wait_sec"] == value


@pytest.mark.parametrize("value", [-1, -0.5])
def test_maker_exit_wait_sec_negative_rejected(tmp_path, monkeypatch, value):
    client, db = _client(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    resp = client.put(f"/api/templates/{tid}", json={"maker_exit_wait_sec": value})
    assert resp.status_code == 400


def test_maker_exit_wait_sec_nan_rejected(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    resp = client.put(
        f"/api/templates/{tid}", json={"maker_exit_wait_sec": float("nan")}
    )
    assert resp.status_code == 400


def test_maker_exit_wait_sec_wrong_type_rejected(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    for bad in ("abc", True, [5], {"x": 1}):
        resp = client.put(f"/api/templates/{tid}", json={"maker_exit_wait_sec": bad})
        assert resp.status_code == 400, bad


@pytest.mark.parametrize("key", ["fast_exit_enabled", "require_merge_ready_for_new_buys"])
def test_fast_exit_bools_must_be_boolean(tmp_path, monkeypatch, key):
    client, db = _client(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    for bad in (1, 0, "true", None, 3.5):
        resp = client.put(f"/api/templates/{tid}", json={key: bad})
        assert resp.status_code == 400, (key, bad)


def test_save_failure_does_not_partially_corrupt_template(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    client.put(
        f"/api/templates/{tid}",
        json={"fast_exit_enabled": True, "maker_exit_wait_sec": 30},
    )
    # A failing save (bad numeric) must not persist ANY of the submitted keys.
    resp = client.put(
        f"/api/templates/{tid}",
        json={"fast_exit_enabled": False, "maker_exit_wait_sec": -5},
    )
    assert resp.status_code == 400
    saved = db.get_template(tid)
    assert saved["fast_exit_enabled"] is True
    assert saved["maker_exit_wait_sec"] == 30


def test_v2_params_exist_in_settings_save_endpoint(tmp_path, monkeypatch):
    # /api/settings (全局参数面板) must accept and persist the V2 keys too.
    client, db = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/settings",
        json={
            "fast_exit_enabled": True,
            "require_merge_ready_for_new_buys": True,
            "maker_exit_wait_sec": 45,
            "merge_advantage_min_usd": 0.02,
        },
    )
    assert resp.status_code == 200
    saved = db.get_template(db.get_default_template_id())
    assert saved["maker_exit_wait_sec"] == 45
    assert saved["merge_advantage_min_usd"] == 0.02
    assert saved["fast_exit_enabled"] is True


# --- §29: regression — every V2 trading parameter is in UI + API ------------


def test_expected_v2_parameter_names_have_full_parity(tmp_path, monkeypatch):
    """Backend default -> DB -> API GET/PUT -> form input -> load/save JS."""
    from config import TEMPLATE_DEFAULTS

    client, db = _client(tmp_path, monkeypatch)
    html = client.get("/config").get_data(as_text=True)
    tid = db.get_default_template_id()
    expected = {
        "fast_exit_enabled": bool,
        "maker_exit_wait_sec": (int, float),
        "require_merge_ready_for_new_buys": bool,
        "merge_enabled": bool,
        "merge_min_shares": (int, float),
        "merge_advantage_min_usd": (int, float),
    }
    for key, types in expected.items():
        assert key in TEMPLATE_DEFAULTS, f"{key} missing backend default"
        assert key in html, f"{key} missing config form"
        got = client.get(f"/api/templates/{tid}").get_json()
        assert key in got, f"{key} missing API GET"
        assert isinstance(got[key], types), f"{key} wrong persisted type"
    # stop-loss keys stay compatible (renamed product semantics only)
    for key in ("stop_loss_mode", "stop_loss_percent", "theta_stop_cents"):
        assert key in TEMPLATE_DEFAULTS
        assert key in html


# --- §48: wallet readiness / monitor / history / dashboard ------------------


def test_wallet_list_includes_fast_exit_and_new_opening_fields(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    db.add_wallet("0xW", "enc", "0xF", signature_type=3)
    rows = client.get("/api/wallets").get_json()
    w = next(r for r in rows if r["address"] == "0xW")
    assert w["fast_exit_status"] in ("ready", "degraded", "blocked", "disabled")
    assert w["new_opening_allowed"] in ("allowed", "paused")
    assert "fast_exit_enabled" in w


def test_wallet_new_opening_paused_when_merge_not_ready(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    db.add_wallet("0xW", "enc", "0xF", signature_type=3)
    db.save_relayer_test_result("0xW", False, "RELAYER_NOT_CONFIGURED")
    rows = client.get("/api/wallets").get_json()
    w = next(r for r in rows if r["address"] == "0xW")
    assert w["new_opening_allowed"] == "paused"
    assert "Merge 未就绪" in w["new_opening_reason"]


def test_monitor_page_has_inventory_exit_view(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    html = client.get("/logs").get_data(as_text=True)
    assert "库存退出（Fast-Exit V2）" in html
    assert "id=\"exit-body\"" in html
    assert "/api/inventory-exit" in html


def test_history_page_has_exit_cycles_and_labels(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    html = client.get("/history").get_data(as_text=True)
    assert "库存退出周期（Fast-Exit V2）" in html
    for method in ("MERGE", "FOK+MERGE", "MAKER", "MARKET", "MIXED"):
        assert method in html
    assert "toggleExitCycleLegs" in html
    assert "/api/exit-cycles" in html


def test_exit_cycle_api_returns_legs_and_method(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    cid = "0x" + "c" * 64
    cycle_id = db.create_exit_cycle(
        "0xW", cid, trigger="reward_fill", held_side="NO", held_asset_id="no", qty=20
    )
    db.add_exit_leg(
        cycle_id, "0xW", cid, "reward_buy", asset_id="no", side="NO",
        qty=20, price=0.3, collateral=-6.0,
    )
    db.add_exit_leg(
        cycle_id, "0xW", cid, "market_sell", asset_id="no", side="SELL",
        qty=20, price=0.29, collateral=5.8, exit_method="MARKET",
    )
    db.close_exit_cycle(cycle_id, closed_reason="test")
    data = client.get("/api/exit-cycles").get_json()
    assert data["total"] == 1
    cycle = data["cycles"][0]
    assert cycle["method"] == "MARKET"
    assert len(cycle["legs"]) == 2
    # monitor endpoint only shows non-CLOSED cycles
    assert client.get("/api/inventory-exit").get_json()["cycles"] == []


def test_exit_method_label_renders_mixed(tmp_path, monkeypatch):
    legs = [
        {"exit_method": "MERGE", "kind": "merge", "status": "done"},
        {"exit_method": "MAKER", "kind": "maker_sell", "status": "confirmed"},
    ]
    assert exit_method_label(legs) == "MIXED"
    # audit fix I: a rested intent alone is not a realized exit method
    assert (
        exit_method_label(
            [{"exit_method": "MAKER", "kind": "maker_sell", "status": "rested"}]
        )
        == ""
    )


def test_dashboard_true_net_math_and_no_fake_zero(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    from engine.pnl import beijing_day

    today = beijing_day(time.time())
    db.upsert_daily_pnl(
        wallet="0xW", date=today,
        reward=10.0, rebate=1.0, sell_profit=3.0, loss=2.0, fee=0.0,
    )
    data = client.get("/api/dashboard").get_json()
    net = data["net"]
    assert net["rewards_today"] == pytest.approx(11.0)
    assert net["inventory_pnl_today"] == pytest.approx(1.0)
    assert net["net_today"] == pytest.approx(12.0)
    assert net["reward_unavailable"] is False
    # breakdown keys exist for all five exit labels
    assert set(data["exit_breakdown"]) == {"MERGE", "FOK+MERGE", "MAKER", "MARKET"}


def test_dashboard_reward_unavailable_not_fake_zero(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    data = client.get("/api/dashboard").get_json()
    assert data["net"]["reward_unavailable"] is True
    assert data["net"]["rewards_today"] == 0
    # the UI must show 待记账 rather than a fake 0
    html = client.get("/").get_data(as_text=True)
    assert "待记账" in html
