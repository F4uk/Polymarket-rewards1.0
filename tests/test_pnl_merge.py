"""tests/test_pnl_merge.py — Merge 盈亏进入本地台账与 API(只计 CONFIRMED,幂等)。"""

import time
from unittest.mock import MagicMock

import web.routes as routes
from models.database import Database


def _mk_db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    return db


def _api():
    api = MagicMock()
    api.get_funder.return_value = "0xFUNDER"
    api.get_activity.return_value = []
    api.get_trades.return_value = []
    return api


def test_confirmed_merge_appears_once_in_daily_pnl(tmp_path):
    from engine.pnl_ledger import rebuild_wallet_pnl

    db = _mk_db(tmp_path)
    api = _api()
    confirmed_ts = time.time()
    eid = db.create_merge_event("0xA", "0xC1", route="PAIR", amount=60,
                                yes_cost=33.0, no_cost=27.0)
    db.update_merge_event(eid, status="CONFIRMED", recovered_usd=60.0,
                          realized_pnl=0.0, confirmed_at=confirmed_ts)
    e2 = db.create_merge_event("0xA", "0xC2", route="PAIR", amount=30,
                               yes_cost=10.0, no_cost=10.0)
    db.update_merge_event(e2, status="CONFIRMED", recovered_usd=30.0,
                          realized_pnl=10.0, confirmed_at=confirmed_ts)

    rebuild_wallet_pnl(api, db, "0xA", "2026-08-01", "2026-08-16")
    # 重跑一次:幂等,合并盈亏不翻倍
    rebuild_wallet_pnl(api, db, "0xA", "2026-08-01", "2026-08-16")

    rows = db.get_daily_pnl("0xA", "2026-08-01", "2026-08-16")
    merge_total = sum(r["merge_pnl"] for r in rows)
    assert abs(merge_total - 10.0) < 1e-9  # 只有 CONFIRMED 计入,且只计一次
    # net 公式含 merge_pnl:reward=0 时 net = merge_pnl
    row = max(rows, key=lambda r: r["merge_pnl"])
    assert abs(row["net"] - row["merge_pnl"]) < 1e-9
    db.close()


def test_submitted_and_failed_merges_do_not_affect_pnl(tmp_path):
    from engine.pnl_ledger import rebuild_wallet_pnl

    db = _mk_db(tmp_path)
    api = _api()
    e1 = db.create_merge_event("0xA", "0xC1", route="PAIR", amount=60)
    db.update_merge_event(e1, status="SUBMITTED", relayer_tx_id="tx")
    e2 = db.create_merge_event("0xA", "0xC2", route="PAIR", amount=60)
    db.update_merge_event(e2, status="FAILED", error_message="boom")
    e3 = db.create_merge_event("0xA", "0xC3", route="PAIR", amount=60)
    # READY 未结账

    rebuild_wallet_pnl(api, db, "0xA", "2026-08-01", "2026-08-16")
    rows = db.get_daily_pnl("0xA", "2026-08-01", "2026-08-16")
    assert all(r["merge_pnl"] == 0 for r in rows)
    assert all(r["net"] == 0 for r in rows)
    db.close()


def test_pnl_api_includes_merge_pnl_in_totals_and_series(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    db.upsert_daily_pnl("0xA", "2026-08-16", reward=7, rebate=0, sell_profit=2,
                        loss=1, fee=0.1, merge_pnl=3)
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    r = client.get("/api/pnl?wallet=0xA&days=3650").get_json()
    assert r["series"][0]["merge_pnl"] == 3
    assert r["totals"]["merge_pnl"] == 3
    assert abs(r["totals"]["net"] - (7 + 2 + 3 - 1 - 0.1)) < 1e-9
    db.close()


def test_merge_events_api_pagination_and_filters(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    for i in range(7):
        eid = db.create_merge_event("0xA", f"0xC{i}", route="PAIR", amount=10 + i)
        db.update_merge_event(eid, status="CONFIRMED", confirmed_at=time.time())
    e = db.create_merge_event("0xB", "0xD1", route="FOK_MERGE", amount=5)
    db.update_merge_event(e, status="FAILED", error_message="rejected")
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True

    r = client.get("/api/merge-events?page=2&page_size=3").get_json()
    assert r["total"] == 8 and len(r["rows"]) == 3 and r["page"] == 2
    r = client.get("/api/merge-events?wallet=0xA&status=CONFIRMED").get_json()
    assert r["total"] == 7
    r = client.get("/api/merge-events?status=FAILED").get_json()
    assert r["total"] == 1 and r["rows"][0]["error_message"] == "rejected"
    r = client.get("/api/merge-events?wallet=0xB").get_json()
    assert r["total"] == 1 and r["rows"][0]["route"] == "FOK_MERGE"
    db.close()


def test_merge_events_api_never_returns_secrets(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    eid = db.create_merge_event("0xA", "0xC1", route="PAIR", amount=60)
    db.update_merge_event(eid, status="CONFIRMED", confirmed_at=time.time())
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    body = client.get("/api/merge-events").get_data(as_text=True)
    assert "POLY_BUILDER" not in body
    assert "private_key" not in body.lower()
    assert "encrypted" not in body
    db.close()


def test_positions_api_includes_exit_status(tmp_path, monkeypatch):
    from engine import exit_status

    db = _mk_db(tmp_path)
    exit_status.set_snapshot("0xA", {"tokY": "MAKER · 剩余30s"}, {"0xC1": "MAKER · 剩余30s"}, 1, 1.0)
    monkeypatch.setattr(routes, "db", db)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True

    api = MagicMock()
    api.get_funder.return_value = "0xF"
    api.get_user_positions.return_value = [
        {"asset": "tokY", "conditionId": "0xC1", "title": "M", "outcome": "Yes",
         "avgPrice": 0.5, "curPrice": 0.5, "size": 100},
    ]
    monkeypatch.setattr(routes, "_wallet_apis", lambda only=None: {"0xA": api})
    r = client.get("/api/positions").get_json()
    assert r[0]["exit_status"] == "MAKER · 剩余30s"
    exit_status.clear_snapshot()
    db.close()


def test_dashboard_counts_exits_pending(tmp_path, monkeypatch):
    from engine import exit_status

    db = _mk_db(tmp_path)
    exit_status.set_snapshot("0xA", {}, {}, 3, 1.0)
    monkeypatch.setattr(routes, "db", db)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    api = MagicMock()
    api.get_open_orders.return_value = []
    api.get_user_positions.return_value = []
    monkeypatch.setattr(routes, "_wallet_apis", lambda: {})
    monkeypatch.setattr(routes, "db", db)
    # list_wallets 直接打真实 db(空)
    r = client.get("/api/dashboard").get_json()
    assert r["exits_pending"] == 3
    exit_status.clear_snapshot()
    db.close()
