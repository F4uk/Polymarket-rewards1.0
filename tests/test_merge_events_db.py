"""tests/test_merge_events_db.py — merge_events 表与 daily_pnl.merge_pnl 的 DB 层。"""

import sqlite3

import pytest

from models.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init()
    yield d
    d.close()


def test_create_and_read_event(db):
    eid = db.create_merge_event("0xA", "0xC1", route="PAIR", amount=60,
                                yes_token_id="Y", no_token_id="N")
    ev = db.get_merge_event(eid)
    assert ev["status"] == "READY" and ev["amount"] == 60
    assert ev["yes_token_id"] == "Y" and ev["no_token_id"] == "N"
    assert db.get_active_merge_event("0xA", "0xC1")["id"] == eid
    assert db.get_active_merge_event("0xA", "0xC2") is None


def test_only_one_active_event_per_wallet_condition(db):
    db.create_merge_event("0xA", "0xC1", route="PAIR", amount=60)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_merge_event("0xA", "0xC1", route="FOK_MERGE", amount=60)
    # 不同钱包/不同市场不受影响
    db.create_merge_event("0xB", "0xC1", route="PAIR", amount=60)
    db.create_merge_event("0xA", "0xC2", route="PAIR", amount=60)
    # 活动事件结束后可再建
    eid = db.get_active_merge_event("0xA", "0xC1")["id"]
    db.update_merge_event(eid, status="CONFIRMED", confirmed_at=1.0)
    db.create_merge_event("0xA", "0xC1", route="PAIR", amount=30)


def test_update_event_whitelist_and_timestamps(db):
    eid = db.create_merge_event("0xA", "0xC1", route="PAIR", amount=60)
    db.update_merge_event(eid, status="SUBMITTED", relayer_tx_id="tx1",
                          complement_order_id="ord1")
    ev = db.get_merge_event(eid)
    assert ev["status"] == "SUBMITTED" and ev["relayer_tx_id"] == "tx1"
    with pytest.raises(ValueError):
        db.update_merge_event(eid, wallet="0xHACK")


def test_list_filters_and_pagination(db):
    for i in range(5):
        eid = db.create_merge_event("0xA", f"0xC{i}", route="PAIR", amount=10 + i)
        if i % 2:
            db.update_merge_event(eid, status="CONFIRMED", confirmed_at=100 + i)
        else:
            db.update_merge_event(eid, status="FAILED", error_message="x")
    rows, total = db.list_merge_events(wallet="0xA", limit=2, offset=0)
    assert total == 5 and len(rows) == 2
    rows2, total2 = db.list_merge_events(wallet="0xA", statuses=["CONFIRMED"])
    assert total2 == 2 and all(r["status"] == "CONFIRMED" for r in rows2)
    rows3, total3 = db.list_merge_events(wallet="0xA", statuses=["READY", "SUBMITTED"])
    assert total3 == 0
    # start/end 过滤 created_at(真实时间):远古窗口不命中
    rows4, total4 = db.list_merge_events(start=100.0, end=102.0)
    assert rows4 == [] and total4 == 0
    # 近未来窗口命中全部
    import time

    rows5, total5 = db.list_merge_events(start=0.0, end=time.time() + 60)
    assert total5 == 5


def test_confirmed_events_by_wallet_and_window(db):
    db.create_merge_event("0xA", "0xC1", route="PAIR", amount=10)
    e2 = db.create_merge_event("0xA", "0xC2", route="PAIR", amount=20)
    db.update_merge_event(e2, status="CONFIRMED", confirmed_at=100.0, realized_pnl=1.5)
    e3 = db.create_merge_event("0xB", "0xC3", route="PAIR", amount=30)
    db.update_merge_event(e3, status="CONFIRMED", confirmed_at=200.0, realized_pnl=2.5)
    assert len(db.get_confirmed_merge_events(wallet="0xA")) == 1
    assert len(db.get_confirmed_merge_events(wallet="0xB")) == 1
    assert len(db.get_confirmed_merge_events()) == 2
    assert len(db.get_confirmed_merge_events(from_ts=150.0)) == 1
    assert len(db.get_confirmed_merge_events(from_ts=0, to_ts=150.0)) == 1


def test_count_active(db):
    db.create_merge_event("0xA", "0xC1", route="PAIR", amount=60)
    e2 = db.create_merge_event("0xA", "0xC2", route="PAIR", amount=60)
    db.update_merge_event(e2, status="SUBMITTED")
    db.create_merge_event("0xB", "0xC3", route="PAIR", amount=60)
    assert db.count_active_merge_events("0xA") == 2
    assert db.count_active_merge_events() == 3


def test_daily_pnl_merge_column_and_net_formula(db):
    db.upsert_daily_pnl("0xA", "2026-08-16", reward=7, rebate=0, sell_profit=2,
                        loss=1, fee=0.1, merge_pnl=3)
    row = db.get_daily_pnl("0xA", "2026-08-16", "2026-08-16")[0]
    assert row["merge_pnl"] == 3
    # net = 7 + 2 + 3 - 1 - 0.1
    assert abs(row["net"] - (7 + 2 + 3 - 1 - 0.1)) < 1e-9
    # 旧签名调用(无 merge_pnl)行为不变
    db.upsert_daily_pnl("0xA", "2026-08-16", 1, 0, 0, 0, 0)
    assert db.get_daily_pnl("0xA", "2026-08-16", "2026-08-16")[0]["merge_pnl"] == 0
    db.upsert_daily_pnl("0xB", "2026-08-16", reward=3, rebate=0, sell_profit=0,
                        loss=0, fee=0, merge_pnl=1.5)
    agg = db.get_daily_pnl_all("2026-08-16", "2026-08-16")[0]
    assert agg["merge_pnl"] == 1.5
