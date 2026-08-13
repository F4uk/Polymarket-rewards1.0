"""tests/test_place_orders.py — place_orders 多档下单(mock API)。"""

from unittest.mock import MagicMock
from engine.manager import WalletWorker


def _tier(size, shares=None, av=None):
    return {
        "size": size,
        "enabled": True,
        "shares": shares or size,
        "rule1_min_coeff": 0,
        "rule2_min_coeff": 0,
        "rule3_min_coeff": 0,
        "gap_high_coeff_sum_min": 20,
        "amount_value_table": av or [{"upper": 1.0, "value": 1}],
    }


def _make_worker(balance=10000.0, template=None):
    api = MagicMock()
    db = MagicMock()
    api.get_balance.return_value = balance
    api.get_open_orders.return_value = []
    api.get_user_positions.return_value = []
    api.get_funder.return_value = "0xF"
    api.gamma_resolution_status.return_value = {}  # UMA 守卫默认:无市场在结算
    db.get_blacklist_ids.return_value = set()
    db.is_in_cooldown.return_value = False
    tmpl = {
        "max_exposure_usd": 250,
        "max_exposure_shares": 500,
        "max_concurrent_markets": 10,
        "size_tiers": [_tier(100)],
        "gap_wide_cents": 10,
        "gap_mid_cents": 5,
        "cliff_probe_cents": 0,
    }
    if template:
        tmpl.update(template)
    db.get_template_for.return_value = tmpl
    return WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5}), api, db


def _ob(bids, asks=None, tick="0.01"):
    return {
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in (asks or [(0.5, 1000)])],
        "tick_size": tick,
    }


def _elig(market_id, token_id, outcome, min_size=100):
    return {
        "market_id": market_id,
        "token_id": token_id,
        "outcome": outcome,
        "market_name": "M",
        "rewards_min_size": min_size,
        "rewards_max_spread": 6,
        "tick_size_str": "0.01",
        "neg_risk": False,
        "market_competitiveness": 0,
    }


def test_skips_market_in_uma_resolution():
    # 有人在 UMA 提交 resolution(umaResolutionStatus 非空)的市场 -> 本轮不挂买单,
    # 避免「撤了又挂」。
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    api.gamma_resolution_status.return_value = {"A": "proposed"}
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_balance_failure_skips_only_that_market():
    # get_balance 是市场循环里唯一没保护的网络调用,一次抖动就把整轮下单掀了:后面的
    # 市场既不下单也不撤单。市场顺序固定(按竞争度排序),所以排在后面的会被系统性饿死
    # ——2026-07-29 实盘 159 次整轮早退,每次前一行都是同钱包的 get_balance failed。
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    api.get_balance.side_effect = [RuntimeError("proxy down"), 10000.0]
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("B", "B-y", "Yes")])
    assert api.place_limit_buy.call_count == 1
    assert api.place_limit_buy.call_args[0][0] == "B-y"


def test_places_normally_when_not_in_resolution():
    # 正常市场(umaResolutionStatus=None)照常下单,守卫不误伤。
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    api.gamma_resolution_status.return_value = {"A": None}
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_called()


def test_buy_passes_neg_risk_none_for_client_autoresolve():
    # 奖励端点不含 neg_risk 字段 -> 买单绝不能写死 False:负风险市场会被 CLOB 拒
    # (invalid POLY_GNOSIS_SAFE signature,2026-07-05 真实事故——天气类负风险市场
    # 全被拒、一单挂不出)。传 None 让客户端按 token_id 自查真实 neg_risk,与卖单一致。
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    assert api.place_limit_buy.call_count >= 1
    assert all(
        c.kwargs.get("neg_risk") is None for c in api.place_limit_buy.call_args_list
    )


def test_reconcile_replaces_partial_fill_remnant():
    # 现象2 端到端:某买单原始 100、已成交 60(剩 40 < min_size 100),持仓已平(不在
    # held_assets)。下单轮 reconcile 须按剩余量(原始-已成交)判定量不符,撤掉残单并
    # 重挂满额 100,而非把它当「量符」一直留着。验证 size_matched 确实传到了 reconcile。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "id": "o1",
            "side": "BUY",
            "asset_id": "A-y",
            "market": "A",
            "price": "0.30",
            "original_size": "100",
            "size_matched": "60",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    cancel_calls = [c.args[0] for c in api.cancel_orders.call_args_list]
    assert ["o1"] in cancel_calls
    placed = {
        round(c.args[1], 2): c.args[2] for c in api.place_limit_buy.call_args_list
    }
    assert placed.get(0.30) == 100


def test_concurrent_market_cap_skips_new_markets():
    worker, api, db = _make_worker(template={"max_concurrent_markets": 1})
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "X",
            "asset_id": "X-y",
            "price": "0.30",
            "original_size": "100",
            "id": "o1",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_concurrent_market_cap_counts_open_sell_held_and_pending_merge():
    cases = [
        ([{"side": "SELL", "market": "X", "asset_id": "X-y", "id": "s1"}], []),
        ([], [{"conditionId": "X", "asset": "X-y", "size": 1, "curPrice": 0.3}]),
    ]
    for orders, positions in cases:
        worker, api, db = _make_worker(template={"max_concurrent_markets": 1})
        api.get_open_orders.return_value = orders
        api.get_user_positions.return_value = positions
        db.get_unresolved_merges.return_value = []
        api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
        worker.place_orders([_elig("A", "A-y", "Yes")])
        api.place_limit_buy.assert_not_called()

    worker, api, db = _make_worker(template={"max_concurrent_markets": 1})
    db.get_unresolved_merges.return_value = [{"condition_id": "X", "status": "submitted"}]
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_pending_merge_a_does_not_block_order_maintenance_for_c():
    worker, api, db = _make_worker()
    db.get_unresolved_merges.return_value = [
        {"condition_id": "A", "status": "submitted"}
    ]
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])

    worker.place_orders(
        [_elig("A", "A-y", "Yes"), _elig("C", "C-y", "Yes")]
    )

    placed_tokens = [call.args[0] for call in api.place_limit_buy.call_args_list]
    assert "A-y" not in placed_tokens
    assert "C-y" in placed_tokens


def test_configured_and_reward_price_ranges_must_intersect():
    worker, api, _ = _make_worker(template={"min_price_cents": 60, "max_price_cents": 70})
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_placement_price_stays_inside_fine_tick_intersection():
    worker, api, _ = _make_worker(template={"min_price_cents": 30.0, "max_price_cents": 30.2})
    api.get_orderbook.return_value = _ob([(0.300, 300)], [(0.302, 1000)], tick="0.001")
    worker.place_orders([_elig("A", "A-y", "Yes")])
    prices = [call.args[1] for call in api.place_limit_buy.call_args_list]
    assert prices and all(0.300 <= price <= 0.302 for price in prices)


def test_idempotent_skips_existing_target_order():
    # 目标单档(0.30 @min_size)已在挂 -> reconcile 判为量价皆符,不撤不重挂。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.30",
            "original_size": "100",
            "id": "o1",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()
    api.cancel_orders.assert_not_called()


def test_skips_held_and_cooldown_and_blacklist():
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    db.is_in_cooldown.return_value = True
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_book_shift_cancels_old_tier_and_places_new():
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.31",
            "original_size": "100",
            "id": "o-stale",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.32, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-stale" in cancelled
    placed = {round(c.args[1], 2) for c in api.place_limit_buy.call_args_list}
    assert 0.30 in placed and 0.31 not in placed


def test_paused_side_cancels_resting_and_other_side_runs():
    # 持有 YES(A-y) -> YES 侧暂停:撤光 YES 在挂买单、不挂 YES 新单;NO(A-n) 照常挂。
    worker, api, db = _make_worker()
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.30}
    ]
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.30",
            "original_size": "100",
            "id": "o-yes",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-yes" in cancelled  # YES 旧单被撤
    placed_tokens = [c.args[0] for c in api.place_limit_buy.call_args_list]
    assert placed_tokens and all(t == "A-n" for t in placed_tokens)  # 只挂 NO


def test_held_value_deducts_other_side_budget():
    # 持有 YES 市值 50U(100×0.50);max_exposure_usd=100 -> NO 侧预算 50U,
    # gap_single 挂 min_size=100 @0.50 恰好用满(100×0.50=50U)。
    worker, api, db = _make_worker(template={"max_exposure_usd": 100})
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.50}
    ]
    api.get_orderbook.return_value = _ob([(0.50, 1000)], [(0.51, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    placed = {
        round(c.args[1], 2): c.args[2]
        for c in api.place_limit_buy.call_args_list
        if c.args[0] == "A-n"
    }
    assert placed.get(0.50) == 100  # 预算 100-50=50U;挂 min_size=100 @0.50 恰用满
    assert not any(c.args[0] == "A-y" for c in api.place_limit_buy.call_args_list)


def test_both_sides_held_cancels_both_and_places_nothing():
    worker, api, db = _make_worker()
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.30},
        {"conditionId": "A", "asset": "A-n", "size": 100.0, "curPrice": 0.30},
    ]
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.30",
            "original_size": "100",
            "id": "o-yes",
        },
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-n",
            "price": "0.30",
            "original_size": "100",
            "id": "o-no",
        },
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-yes" in cancelled and "o-no" in cancelled
    api.place_limit_buy.assert_not_called()


def test_budget_exhausted_still_cancels_paused_side():
    # 已持仓市值吃满单市场敞口(budget_ok=False);暂停侧仍撤光旧单,活跃侧无预算不挂。
    worker, api, db = _make_worker(template={"max_exposure_usd": 20})
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.50}
    ]  # 市值 50U > 20U cap -> budget_ok False
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.50",
            "original_size": "100",
            "id": "o-yes",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.50, 1000)], [(0.51, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-yes" in cancelled  # 暂停侧仍撤光(不受 budget_ok 影响)
    api.place_limit_buy.assert_not_called()  # 活跃侧无预算 -> 不挂


def test_dropout_cancels_ineligible_market_buys():
    # 在市场 B 有买单,本轮 eligible 只有 A,cancel_dropouts=True -> B 买单被撤、A 照常挂。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "B",
            "asset_id": "B-y",
            "price": "0.40",
            "original_size": "100",
            "id": "o-b",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")], cancel_dropouts=True)
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-b" in cancelled
    placed_tokens = [c.args[0] for c in api.place_limit_buy.call_args_list]
    assert "A-y" in placed_tokens


def test_dropout_skips_cooldown_but_cancels_others():
    # C 冷却 -> 保留;D 非冷却且跌出 -> 撤。
    worker, api, db = _make_worker()
    db.is_in_cooldown.side_effect = lambda w, m: (m == "C")
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "C",
            "asset_id": "C-y",
            "price": "0.40",
            "original_size": "100",
            "id": "o-c",
        },
        {
            "side": "BUY",
            "market": "D",
            "asset_id": "D-y",
            "price": "0.40",
            "original_size": "100",
            "id": "o-d",
        },
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")], cancel_dropouts=True)
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-d" in cancelled  # 跌出(非冷却) -> 撤
    assert "o-c" not in cancelled  # 冷却 -> 保留


def test_dropout_off_by_default():
    # cancel_dropouts 默认 False(测试挂单路径):跌出市场买单不被撤。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "B",
            "asset_id": "B-y",
            "price": "0.40",
            "original_size": "100",
            "id": "o-b",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-b" not in cancelled


def test_dropout_cancels_only_buys_not_sells():
    # 跌出市场若有卖单,只撤买单、卖单保留。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "B",
            "asset_id": "B-y",
            "price": "0.40",
            "original_size": "100",
            "id": "o-b-buy",
        },
        {
            "side": "SELL",
            "market": "B",
            "asset_id": "B-y",
            "price": "0.60",
            "original_size": "100",
            "id": "o-b-sell",
        },
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")], cancel_dropouts=True)
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-b-buy" in cancelled and "o-b-sell" not in cancelled


def test_gap_single_places_one_order_highest_qualifying():
    worker, api, db = _make_worker(template=_gap_template())
    # 相邻价差 1¢(规则3);买一 0.28 系数 50/(20*2)=1.25 >0 -> 挂一单 20 股 @0.28。
    api.get_orderbook.return_value = _ob([(0.28, 50), (0.27, 40)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    assert api.place_limit_buy.call_count == 1
    c = api.place_limit_buy.call_args_list[0]
    assert round(c.args[1], 2) == 0.28 and c.args[2] == 20


def _gap_template():
    return {
        "size_tiers": [
            _tier(
                20,
                av=[
                    {"upper": 0.20, "value": 1},
                    {"upper": 0.25, "value": 1.5},
                    {"upper": 0.31, "value": 2},
                ],
            )
        ],
        "gap_wide_cents": 10,
        "gap_mid_cents": 5,
    }


def _actions(db, atype):
    return [
        c
        for c in db.record_action.call_args_list
        if c.kwargs.get("action_type") == atype
    ]


def test_gap_single_place_buy_records_rule_reason():
    # ① 断层单档挂单记真实原因(规则级/断层/系数),不再是 laddering 死文案。
    worker, api, db = _make_worker(template=_gap_template())
    api.get_orderbook.return_value = _ob([(0.28, 50), (0.27, 40)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    calls = _actions(db, "place_buy")
    assert len(calls) == 1
    reason = calls[0].kwargs["reason"]
    assert "规则3" in reason
    assert "累计厚度" not in reason
    assert "最大断层" in calls[0].kwargs["price_basis"]


def test_gap_single_skip_records_reason_and_dedups():
    # ② 判成不挂的市场记 gap_skip;同一判断第二轮去重不再记。
    tmpl = _gap_template()
    tmpl["size_tiers"][0]["rule3_min_coeff"] = 100  # 无档系数 >100 -> 规则3 但不挂
    worker, api, db = _make_worker(template=tmpl)
    api.get_orderbook.return_value = _ob([(0.28, 50), (0.27, 40)], [(0.31, 1000)])
    elig = [_elig("A", "A-y", "Yes", min_size=20)]
    worker.place_orders(elig)
    skips = _actions(db, "gap_skip")
    assert len(skips) == 1
    assert "规则3" in skips[0].kwargs["reason"]
    basis = skips[0].kwargs["price_basis"]
    assert "区间内买档" in basis
    assert "各档系数均 ≤ 选档门槛100" in basis
    assert "get_orderbook" in basis
    assert "断层单档判定不挂" not in basis  # 旧通用串已弃用
    api.place_limit_buy.assert_not_called()
    db.record_action.reset_mock()
    worker.place_orders(elig)
    assert _actions(db, "gap_skip") == []


def test_cliff_below_zone_skips_side():
    worker, api, db = _make_worker(template={"cliff_probe_cents": 2})
    # 买一0.28/买二0.27(区间内),下方直接砸到0.05(区间下沿2¢内无支撑)→ 悬崖 → 不挂
    # (min_size 用默认 100,匹配 _make_worker 默认档位 100,悬崖判断先于系数计算、
    # 与 min_size 取值无关)。
    api.get_orderbook.return_value = _ob(
        [(0.28, 50), (0.27, 40), (0.05, 9999)], [(0.31, 1000)]
    )
    worker.place_orders([_elig("A", "A-y", "Yes")])
    assert api.place_limit_buy.call_count == 0


def test_no_matching_tier_market_skipped():
    # 双点防御:eligible 里混进无档市场(配置变更时差)-> 下单层也要跳过。
    worker, api, db = _make_worker()  # 模板只有 100 档
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=50)])
    api.place_limit_buy.assert_not_called()


def test_tier_shares_used_for_order_size():
    # 档位 100 配 150 份 -> 挂 150(预算/敞口都够)。
    worker, api, db = _make_worker(template={"size_tiers": [_tier(100, shares=150)]})
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    assert api.place_limit_buy.call_count == 1
    assert api.place_limit_buy.call_args_list[0].args[2] == 150


def test_tier_shares_zero_falls_back_to_min_size():
    # 档位 shares 为 0(校验未接入前可直接写进模板)-> 回退挂 min_size(100),
    # 而不是把 0 当合法份数:那会让该边 plan 为空 -> reconcile 撤掉现有买单且不补,
    # 静默摘牌无痕。
    t = _tier(100)
    t["shares"] = 0  # _tier 的 `shares or size` 会把 0 变 size,须直接改 dict
    worker, api, db = _make_worker(template={"size_tiers": [t]})
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    assert api.place_limit_buy.call_count == 1
    assert api.place_limit_buy.call_args_list[0].args[2] == 100
