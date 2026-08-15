"""tests/test_protected_exit.py — 受保护 FAK / Maker 挂价 / FOK+Merge 报价纯函数。"""

from engine.protected_exit import (
    bid_walk,
    maker_sell_price,
    protected_fak_plan,
    quote_direct_exit,
    quote_merge_route,
)


def _bids(*pairs):
    return [{"price": p, "size": s} for p, s in pairs]


def test_bid_walk_full_depth():
    w = bid_walk(_bids((0.60, 40), (0.55, 50), (0.50, 100)), 60)
    assert w["executable"] == 60
    assert w["worst_price"] == 0.55
    assert w["levels"] == [(0.60, 40), (0.55, 20)]


def test_bid_walk_partial_depth():
    w = bid_walk(_bids((0.60, 40)), 60)
    assert w["executable"] == 40
    assert w["worst_price"] == 0.60


def test_bid_walk_invalid():
    assert bid_walk([], 10)["worst_price"] is None
    assert bid_walk(_bids((0, 10)), 10)["worst_price"] is None
    assert bid_walk(_bids((0.5, 10)), 0)["executable"] == 0


def test_protected_fak_plan_prices_at_worst_executable_bid():
    plan = protected_fak_plan(_bids((0.60, 40), (0.55, 50)), 60)
    assert plan["worst_price"] == 0.55  # 走完 60 份所需的最低买价
    assert plan["expected_recovery"] == 40 * 0.60 + 20 * 0.55
    assert plan["full"] is True


def test_protected_fak_plan_partial():
    plan = protected_fak_plan(_bids((0.60, 30)), 60)
    assert plan["executable"] == 30
    assert plan["full"] is False


def test_protected_fak_plan_none_without_bids():
    assert protected_fak_plan([], 10) is None


def test_maker_sell_price_prefers_ask_then_bid():
    asks = [{"price": 0.42, "size": 5}, {"price": 0.45, "size": 5}]
    assert maker_sell_price(asks, 0.41) == 0.42
    assert maker_sell_price([], 0.41) == 0.41  # 无卖盘时回退买一
    assert maker_sell_price([], None) is None


def test_quote_direct_exit_equals_fak_plan():
    d = quote_direct_exit(_bids((0.60, 40), (0.55, 50)), 60)
    assert d["worst_price"] == 0.55
    assert d["expected_recovery"] == 35.0


def test_merge_route_wins_when_cheaper_than_direct_plus_advantage():
    # 直接 FAK 回收 33;NO 卖盘 0.40×60 -> 补对成本 24;Merge 回收 60
    # 条件: 60 - 24 = 36 > 33 + 0.01 ✓
    q = quote_merge_route([{"price": 0.40, "size": 60}], 60, 33.0, 0.01, 1)
    assert q["feasible"] is True
    assert q["fok_worst_price"] == 0.40
    assert q["expected_recovery"] == 60.0


def test_merge_route_fails_when_complement_too_expensive():
    # NO 卖盘 0.60 -> 超出动态上限 0.4498 -> 深度不足 -> 不可行(不会按 0.60 补)
    q = quote_merge_route([{"price": 0.60, "size": 60}], 60, 33.0, 0.01, 1)
    assert q["feasible"] is False
    assert "深度不足" in q["reason"]


def test_merge_route_fails_when_direct_already_better():
    # 直接回收 59.99,优势 0.02 -> 动态上限 <= 0 -> 直接路线已更优
    q = quote_merge_route([{"price": 0.40, "size": 60}], 60, 59.99, 0.02, 1)
    assert q["feasible"] is False
    assert "更优或等价" in q["reason"]


def test_merge_route_requires_exact_depth():
    q = quote_merge_route([{"price": 0.40, "size": 10}], 60, 33.0, 0.01, 1)
    assert q["feasible"] is False
    assert "深度不足" in q["reason"]


def test_merge_route_respects_min_shares():
    q = quote_merge_route([{"price": 0.40, "size": 60}], 5, 3.0, 0.01, 10)
    assert q["feasible"] is False
    assert "merge_min_shares" in q["reason"]


def test_merge_route_worst_price_derived_dynamically():
    # 优势越大,可接受的 FOK 最坏价越低:0.01 优势可行,6.5 优势不可行(上限 0.3917)
    small_adv = quote_merge_route([{"price": 0.40, "size": 60}], 60, 30.0, 0.01, 1)
    big_adv = quote_merge_route([{"price": 0.40, "size": 60}], 60, 30.0, 6.5, 1)
    assert small_adv["feasible"] is True
    assert small_adv["fok_worst_price"] == 0.40
    assert big_adv["feasible"] is False
