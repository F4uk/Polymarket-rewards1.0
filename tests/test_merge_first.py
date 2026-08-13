"""Deterministic V1 merge-first helpers and accounting tests (no network)."""

import pytest

from engine.exit_router import executable_limit_price, route_residual
from engine.merge import ordinary_binary_plan, pair_quantity, reserved_sell_quantity
from engine.pnl import realized_pnl_by_day
from engine.take_profit import position_cost_with_lots


def _positions(yes, no, *, neg_risk=False):
    return [
        {"conditionId": "0xc", "asset": "yes", "outcome": "Yes", "size": yes, "negRisk": neg_risk},
        {"conditionId": "0xc", "asset": "no", "outcome": "No", "size": no, "negRisk": neg_risk},
    ]


def test_merge_pair_planning_exact_unequal_and_fractional():
    assert pair_quantity(20, 12) == 12
    assert pair_quantity(2.5, 2.25) == 2.25
    assert pair_quantity(1, 0, 0.1) == 0
    plan = ordinary_binary_plan("0xc", _positions(20, 12), 1)
    assert plan and (plan.yes_asset_id, plan.no_asset_id, plan.qty) == ("yes", "no", 12)
    assert ordinary_binary_plan("0xc", _positions(20, 20, neg_risk=True), 1) is None


def test_reserved_sell_quantity_tracks_unfilled_reservation():
    orders = [
        {"side": "SELL", "asset_id": "yes", "original_size": "20", "size_matched": "3"},
        {"side": "BUY", "asset_id": "yes", "original_size": "9"},
    ]
    assert reserved_sell_quantity(orders, "yes") == 17


def test_merge_fifo_consumption_leaves_residual_yes_cost_correct():
    yes_fills = [
        {"side": "BUY", "price": 0.30, "size": 20, "ts": 1, "trade_id": "y"},
        {"side": "MERGE", "price": 1, "size": 12, "ts": 3},
    ]
    cost, lots = position_cost_with_lots(yes_fills, 8)
    assert cost == 0.30
    assert lots[0]["take"] == 8


def test_merge_realized_pnl_is_collateral_minus_both_leg_costs():
    fills = [
        {"side": "BUY", "price": 0.30, "size": 12, "ts": 1},
        {"side": "SELL", "price": 1, "size": 12, "ts": 2},
    ]
    daily = realized_pnl_by_day(fills)
    bucket = next(iter(daily.values()))
    # The operation-level merge ledger contributes collateral once. Per-asset
    # synthetic merge events are inventory consumption only, not two sales.
    assert bucket["sell_profit"] == pytest.approx(8.4)


def test_multiple_merge_sell_and_buy_replay_reconciles_fractional_residual():
    fills = [
        {"side": "BUY", "price": 0.30, "size": 20, "ts": 1},
        {"side": "MERGE", "size": 12, "ts": 2},
        {"side": "SELL", "price": 0.45, "size": 3, "ts": 3},
        {"side": "BUY", "price": 0.40, "size": 2.5, "ts": 4},
        {"side": "MERGE", "size": 1.25, "ts": 5},
    ]
    cost, lots = position_cost_with_lots(fills, 6.25)
    # FIFO consumes the oldest remaining YES lot on each merge: 3.75 @ 0.30
    # and 2.5 @ 0.40 remain after the sequence.
    assert cost == pytest.approx((3.75 * 0.30 + 2.5 * 0.40) / 6.25)
    assert sum(lot["take"] for lot in lots) == pytest.approx(6.25)


def test_severe_router_prefers_direct_when_margin_not_met_or_depth_missing():
    direct = route_residual(
        qty=10, severe_loss=True,
        direct_levels=[{"price": 0.40, "size": 10}],
        complement_asks=[{"price": 0.599, "size": 10}], safety_margin=0.02,
    )
    assert direct.action == "direct_sell"
    missing = route_residual(
        qty=10, severe_loss=True,
        direct_levels=[{"price": 0.40, "size": 10}],
        complement_asks=[{"price": 0.30, "size": 9}], safety_margin=0.01,
    )
    assert missing.action == "direct_sell"


def test_severe_router_selects_protected_complement_when_decisively_better():
    route = route_residual(
        qty=10, severe_loss=True,
        direct_levels=[{"price": 0.20, "size": 10}],
        complement_asks=[{"price": 0.50, "size": 10}], safety_margin=0.01,
    )
    assert route.action == "buy_complement_and_merge"
    assert route.advantage == 3


def test_severe_router_can_use_merge_when_direct_depth_is_insufficient():
    route = route_residual(
        qty=10, severe_loss=True,
        direct_levels=[{"price": 0.20, "size": 9}],
        complement_asks=[{"price": 0.50, "size": 10}], safety_margin=0.01,
    )
    assert route.action == "buy_complement_and_merge"


def test_fok_limit_is_the_worst_price_needed_for_full_complement_depth():
    asks = [{"price": 0.40, "size": 3}, {"price": 0.42, "size": 7}, {"price": 0.95, "size": 100}]
    assert executable_limit_price(asks, 10) == 0.42
    assert executable_limit_price(asks, 111) is None
