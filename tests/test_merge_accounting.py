"""tests/test_merge_accounting.py — Merge 的 FIFO 消耗、按日盈亏聚合与重建集成。"""

from engine.fills import extract_fills
from engine.merge_accounting import (
    consume_fifo_cost,
    merge_event_pnl_from_lots,
    merge_pnl_by_day,
    pair_amount,
)
from engine.take_profit import position_cost_with_lots


def _lots(*pairs):
    return [
        {"price": p, "take": s, "ts": ts, "trade_id": f"t{i}"}
        for i, (p, s, ts) in enumerate(pairs)
    ]


def test_consume_fifo_oldest_first():
    lots = _lots((0.60, 100, 1.0), (0.50, 50, 2.0))
    cost, left = consume_fifo_cost(lots, 120)
    assert cost == 0.60 * 100 + 0.50 * 20
    assert left == [{"price": 0.50, "take": 30.0, "ts": 2.0, "trade_id": "t1"}]


def test_consume_fifo_over_qty_returns_none():
    lots = _lots((0.60, 100, 1.0))
    assert consume_fifo_cost(lots, 101)[0] is None
    assert consume_fifo_cost(lots, 0)[0] == 0.0


def test_merge_event_pnl_from_lots():
    yes = _lots((0.55, 60, 1.0))
    no = _lots((0.45, 60, 1.0))
    pnl, yc, nc, ok = merge_event_pnl_from_lots(60, yes, no)
    assert ok and yc == 33.0 and nc == 27.0 and pnl == 0.0
    pnl2, _, _, ok2 = merge_event_pnl_from_lots(60, yes, _lots((0.45, 30, 1.0)))
    assert not ok2 and pnl2 == 0.0


def test_merge_pnl_by_day_groups_and_ignores_non_confirmed_input():
    events = [
        {"confirmed_at": 1_755_300_000, "realized_pnl": 2.5},
        {"confirmed_at": 1_755_300_100, "realized_pnl": -1.0},
        {"confirmed_at": 1_755_400_000, "realized_pnl": 7.0},
    ]
    out = merge_pnl_by_day(events, lambda ts: "2026-08-16" if ts < 1_755_350_000 else "2026-08-17")
    assert out == {"2026-08-16": 1.5, "2026-08-17": 7.0}
    assert merge_pnl_by_day([], lambda ts: "d") == {}


def test_pair_amount_uses_min():
    assert pair_amount(100, 60) == 60
    assert pair_amount(0, 60) == 0
    assert pair_amount(60, 0) == 0


def test_position_cost_reconstruction_consumes_confirmed_merges():
    fills = [
        {"side": "BUY", "price": 0.60, "size": 100, "ts": 1.0, "trade_id": "a"},
        {"side": "BUY", "price": 0.50, "size": 100, "ts": 2.0, "trade_id": "b"},
    ]
    events = [{"amount": 100}, {"amount": 60}]
    cost, lots = position_cost_with_lots(fills, 40, events)
    # 先烧 100 份(0.60 全 + 0.50 的 60?不:FIFO 先烧 0.60 的 100),再烧 60(0.50 的 60)
    # 0.60×100 烧完;0.50×100 烧 60 -> 剩 40 @0.50
    assert abs(cost - 0.50) < 1e-9
    assert sum(l["take"] for l in lots) == 40


def test_position_cost_without_events_unchanged():
    fills = [
        {"side": "BUY", "price": 0.60, "size": 100, "ts": 1.0, "trade_id": "a"},
    ]
    cost, lots = position_cost_with_lots(fills, 100)
    assert abs(cost - 0.60) < 1e-9
    # 显式传空列表行为一致
    cost2, _ = position_cost_with_lots(fills, 100, [])
    assert cost2 == cost


def test_extract_fills_replay_with_merge_events_matches_data_api_size():
    trades = [
        {"id": "1", "market": "C", "match_time": 1.0, "trader_side": "TAKER",
         "side": "BUY", "price": 0.50, "size": 100, "asset_id": "Y", "maker_orders": []},
    ]
    fills = extract_fills(trades, "0xF", "Y")
    cost, lots = position_cost_with_lots(fills, 40, [{"amount": 60}])
    assert abs(cost - 0.50) < 1e-9
    assert sum(l["take"] for l in lots) == 40
