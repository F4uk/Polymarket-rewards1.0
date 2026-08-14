"""V2 pure economics tests (spec §40) — no network, no DB."""

import math

import pytest

from engine.inventory_exit import (
    choose_residual_route,
    complement_merge_recovery,
    direct_recovery,
    effective_opposite_buy_qty,
    exit_method_label,
    maker_escape_price,
    merge_advantage,
)


def _bids(*pairs):
    return [{"price": str(p), "size": str(s)} for p, s in pairs]


# --- direct recovery across executable depth ---------------------------------


def test_direct_recovery_walks_multiple_bid_levels():
    # 0.60×5 + 0.55×5 for qty 10: proceeds 5.75, worst price 0.55.
    value, worst = direct_recovery(_bids((0.60, 5), (0.55, 5), (0.50, 100)), 10)
    assert value == 5.75
    assert worst == 0.55


def test_direct_recovery_single_level_best_bid_only():
    # best bid alone contains the whole qty: best_bid * qty is correct here.
    value, worst = direct_recovery(_bids((0.60, 20)), 10)
    assert value == 6.0
    assert worst == 0.60


def test_direct_recovery_insufficient_depth_returns_none():
    # Full qty cannot execute within available depth -> (None, None).
    value, worst = direct_recovery(_bids((0.60, 3), (0.55, 3)), 10)
    assert value is None
    assert worst is None


def test_direct_recovery_fractional_shares():
    value, worst = direct_recovery(_bids((0.40, 7.5), (0.39, 2.5)), 10)
    assert value == 3.975
    assert worst == 0.39


# --- complement + Merge recovery ---------------------------------------------


def test_complement_merge_recovery_uses_worst_case_limit_not_best_ask():
    # asks 0.60×3 + 0.65×7 for qty 10: signed FOK limit must be 0.65 -> cost 6.5.
    asks = [{"price": "0.60", "size": "3"}, {"price": "0.65", "size": "7"}]
    recovery, limit = complement_merge_recovery(10, asks)
    assert limit == 0.65
    assert recovery == 10 - 6.5


def test_complement_merge_recovery_fee_buffer():
    asks = [{"price": "0.50", "size": "10"}]
    recovery, _ = complement_merge_recovery(10, asks, fee_buffer=0.05)
    assert recovery == 10 - 5.0 - 0.05


def test_complement_merge_recovery_insufficient_depth():
    asks = [{"price": "0.50", "size": "9"}]
    recovery, limit = complement_merge_recovery(10, asks)
    assert recovery is None
    assert limit is None


def test_merge_advantage_difference():
    assert merge_advantage(13.9, 11.17) == pytest.approx(2.73)
    assert merge_advantage(None, 11.17) is None
    assert merge_advantage(13.9, None) is None


# --- route selection -----------------------------------------------------------


def test_route_fok_merge_when_merge_recovers_more_than_margin():
    route = choose_residual_route(
        qty=50, direct_value=11.17, direct_worst=0.2,
        merge_value=13.9, margin=0.01, wait_sec=30, elapsed=5,
    )
    assert route["route"] == "FOK_MERGE"


def test_route_direct_wins_goes_to_maker_window_then_direct():
    # merge does not beat direct by the margin -> normal path waits in window.
    route = choose_residual_route(
        qty=50, direct_value=12.0, direct_worst=0.24,
        merge_value=12.009, margin=0.01, wait_sec=30, elapsed=5,
    )
    assert route["route"] == "MAKER_WINDOW"
    assert route["window_remaining"] == 25
    # timeout -> protected direct exit.
    route2 = choose_residual_route(
        qty=50, direct_value=12.0, direct_worst=0.24,
        merge_value=12.009, margin=0.01, wait_sec=30, elapsed=31,
    )
    assert route2["route"] == "DIRECT"


def test_route_direct_unavailable_merge_available():
    route = choose_residual_route(
        qty=10, direct_value=None, direct_worst=None,
        merge_value=4.5, margin=0.01, wait_sec=30, elapsed=2,
    )
    assert route["route"] == "FOK_MERGE"


def test_route_merge_unavailable_direct_available():
    route = choose_residual_route(
        qty=10, direct_value=4.0, direct_worst=0.4,
        merge_value=None, margin=0.01, wait_sec=30, elapsed=31,
    )
    assert route["route"] == "DIRECT"


def test_route_neither_executable_blocks():
    route = choose_residual_route(
        qty=10, direct_value=None, direct_worst=None,
        merge_value=None, margin=0.01, wait_sec=0, elapsed=0, urgent=True,
    )
    assert route["route"] == "BLOCKED"


def test_route_urgent_skips_maker_wait():
    # emergency / low-balance / resolution: no maker window.
    route = choose_residual_route(
        qty=10, direct_value=4.0, direct_worst=0.4,
        merge_value=3.0, margin=0.01, wait_sec=300, elapsed=1, urgent=True,
    )
    assert route["route"] == "DIRECT"


def test_route_zero_wait_immediate_direct():
    route = choose_residual_route(
        qty=10, direct_value=4.0, direct_worst=0.4,
        merge_value=3.0, margin=0.01, wait_sec=0, elapsed=0,
    )
    assert route["route"] == "DIRECT"


def test_route_noop_no_residual():
    route = choose_residual_route(
        qty=0, direct_value=0, direct_worst=None, merge_value=None,
    )
    assert route["route"] == "NOOP"


# --- maker escape price: below cost allowed, never crossing -------------------


def test_maker_escape_uses_best_ask_and_can_be_below_cost():
    # cost 0.30, best ask 0.25 -> rest at 0.25 (below cost is allowed in V2).
    assert maker_escape_price(0.24, 0.25, 0.01) == 0.25


def test_maker_escape_never_crosses_book():
    # ask missing -> smallest tick above best bid.
    assert maker_escape_price(0.24, None, 0.01) == 0.25
    # degenerate book ask <= bid -> tick above bid, never a taker price.
    assert maker_escape_price(0.25, 0.24, 0.01) == 0.26
    # price must be strictly above best bid.
    assert maker_escape_price(0.30, 0.30, 0.01) == 0.31


def test_maker_escape_subcent_tick():
    assert maker_escape_price(0.235, None, 0.005) == 0.24


def test_maker_escape_no_book_returns_none():
    assert maker_escape_price(None, None, 0.01) is None


# --- opposite buy cap ----------------------------------------------------------


def test_opposite_buy_cap_clamps_to_unpaired_residual():
    assert effective_opposite_buy_qty(20, 0, 50) == 20
    assert effective_opposite_buy_qty(20, 5, 50) == 15
    assert effective_opposite_buy_qty(20, 20, 50) == 0
    assert effective_opposite_buy_qty(20, 25, 50) == 0
    assert effective_opposite_buy_qty(0, 0, 50) == 0


# --- exit method labels ---------------------------------------------------------


def _leg(method, kind="market_sell"):
    return {"exit_method": method, "kind": kind}


def test_exit_method_labels():
    assert exit_method_label([_leg("MERGE")]) == "MERGE"
    assert exit_method_label([_leg("FOK+MERGE"), _leg("FOK+MERGE")]) == "FOK+MERGE"
    assert exit_method_label([_leg("MAKER")]) == "MAKER"
    assert exit_method_label([_leg("MARKET")]) == "MARKET"
    assert exit_method_label([_leg("MERGE"), _leg("MARKET")]) == "MIXED"
    assert exit_method_label([_leg("")]) == ""
    assert exit_method_label([]) == ""
