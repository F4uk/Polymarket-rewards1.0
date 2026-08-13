"""tests/test_monitor.py — API-driven OrderMonitor unit tests."""

import logging
import pytest
from unittest.mock import MagicMock, call, patch
from engine.monitor import OrderMonitor
from engine.monitor_status import get_snapshot, clear_snapshot


def _make_monitor(settings=None):
    api = MagicMock()
    db = MagicMock()
    default_settings = {
        "cooldown_minutes": 20,
        "rewards_cache_ttl_sec": 600,
        # 悬崖复查默认关:这些用例测的是价差/奖励/价格带子逻辑,不该被悬崖否决误伤
        # (与 tests/test_place_orders.py 的 _make_worker 同样处理)。要测的用例显式传。
        "cliff_probe_cents": 0,
    }
    if settings:
        default_settings.update(settings)
    db.get_settings.return_value = default_settings
    db.get_template_for.return_value = default_settings
    db.get_blacklist_ids.return_value = set()
    # funder address via api.get_funder()
    api.get_funder.return_value = "0xFUNDER"
    # sane default so methods that read get_trades don't iterate a MagicMock
    api.get_trades.return_value = []
    # 结算守卫默认:无市场在结算(否则裸 MagicMock 会被 in_resolution 判真,冲垮离场用例)
    api.gamma_resolution_status.return_value = {}
    # Step 1 只撤仍在挂的单;默认无在挂单(测试按需覆盖)
    api.get_open_orders.return_value = []
    # 批量盘口默认转发到单发 mock:用例只需照旧设 api.get_orderbook.return_value,
    # 不必关心取数走批量还是单发(那是取数实现,不是这些用例要测的判定逻辑)。
    # 要断言批量契约本身的用例自行覆盖 side_effect。
    api.get_orderbooks.side_effect = lambda ids: {t: api.get_orderbook(t) for t in ids}
    monitor = OrderMonitor(api, db, "0xABC")
    return monitor, api, db


# ---------------------------------------------------------------------------
# Step 1: check_buy_orders — fill detection via get_trades
# ---------------------------------------------------------------------------


class TestCheckBuyOrders:
    def test_filled_order_sets_cooldown_and_cancels_remainder(self):
        # Take-profit is position-driven (check_take_profit) and buy "history"
        # is the live Data API position now, so Step 1 writes NO trade row — it
        # only sets the cooldown and cancels the filled buy's remainder.
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        api.get_open_orders.return_value = [{"id": "ord1"}]  # 该单仍在挂 -> 有余量可撤

        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = [
                {
                    "trade_id": "trade1",
                    "order_id": "ord1",
                    "asset_id": "tok1",
                    "price": 0.25,
                    "size": 1000.0,
                    "market": "mkt1",
                    "ts": 1000000.0,
                }
            ]
            monitor.check_buy_orders()

        api.place_limit_sell.assert_not_called()  # no per-fill sell anymore
        db.record_trade.assert_not_called()  # no trades-table write anymore
        db.set_side_pause.assert_called_with("0xABC", "mkt1", "tok1", 20)
        api.cancel_orders.assert_called_with(["ord1"])
        action_types = [
            c.kwargs["action_type"] for c in db.record_action.call_args_list
        ]
        assert "take_profit_sell" not in action_types
        assert "cancel_remainder" in action_types

    def test_old_fill_of_gone_order_not_cancelled(self):
        # 根治重放洪水:成交回放里那些早已成交/消失的单不在当前挂单里 -> 不撤(不刷
        # already-canceled)。即便 get_trades 去重在并发下失效、把老成交当"新成交",只要
        # 它的单已不在挂单,就不撤——这个护栏不依赖 get_trades/水位是否可靠。
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        api.get_open_orders.return_value = []  # 钱包无在挂单(单早成交消失)
        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = [
                {
                    "trade_id": "T",
                    "order_id": "gone-ord",
                    "asset_id": "tok",
                    "price": 0.2,
                    "size": 20.0,
                    "market": "mkt",
                    "ts": 1.0,
                }
            ]
            monitor.check_buy_orders()
        api.cancel_orders.assert_not_called()  # 单不在挂单 -> 不撤


    def test_scoring_shadow_records_true_false_and_unknown_without_mutating_orders(self):
        monitor, api, db = _make_monitor()
        orders = [
            {"id": "yes", "side": "BUY", "market": "m", "asset_id": "y"},
            {"id": "no", "side": "BUY", "market": "m", "asset_id": "n"},
            {"id": "other", "side": "SELL", "market": "m", "asset_id": "y"},
        ]
        api.are_orders_scoring.return_value = {"yes": True, "no": False}
        monitor._observe_order_scoring(orders)
        observed = [call.args[-1] for call in db.record_scoring_observation.call_args_list]
        assert observed == ["true", "false"]
        api.cancel_orders.assert_not_called()

        monitor._scoring_checked_at = 0
        api.are_orders_scoring.side_effect = RuntimeError("offline")
        monitor._observe_order_scoring(orders)
        assert [call.args[-1] for call in db.record_scoring_observation.call_args_list[-2:]] == ["unknown", "unknown"]
        api.cancel_orders.assert_not_called()

    def test_fill_pauses_only_the_filled_token_side(self):
        monitor, api, db = _make_monitor()
        monitor._handle_fill(
            {"asset_id": "yes", "market": "m", "size": 2, "price": 0.3, "order_id": None},
            set(), set(),
        )
        db.set_side_pause.assert_called_once_with("0xABC", "m", "yes", 20)
        db.set_cooldown.assert_not_called()

    def test_partial_fill_writes_no_trade(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []

        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = [
                {
                    "trade_id": "trade2",
                    "order_id": "ord1",
                    "asset_id": "tok1",
                    "price": 0.25,
                    "size": 600.0,
                    "market": "mkt1",
                    "ts": 1000001.0,
                }
            ]
            monitor.check_buy_orders()

        api.place_limit_sell.assert_not_called()
        db.record_trade.assert_not_called()

    def test_unfilled_order_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []

        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = []
            monitor.check_buy_orders()

        api.place_limit_sell.assert_not_called()

    def test_get_trades_failure_logs_and_returns(self):
        monitor, api, db = _make_monitor()
        api.get_trades.side_effect = Exception("network error")

        monitor.check_buy_orders()  # must not raise

        api.place_limit_sell.assert_not_called()

    def test_seen_fill_keys_updated(self):
        """Watermark and seen-key are updated after each fill."""
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        api.place_limit_sell.return_value = {}

        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = [
                {
                    "trade_id": "t1",
                    "order_id": "o1",
                    "asset_id": "tok1",
                    "price": 0.5,
                    "size": 100.0,
                    "market": "mkt1",
                    "ts": 9999.0,
                }
            ]
            monitor.check_buy_orders()

        assert ("t1", "o1") in monitor._seen_fill_keys
        assert monitor._after_ts == 9999.0

    def test_handle_fill_exception_still_marks_seen(self):
        """If a fill handler op raises, seen-key and watermark must still update."""
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        db.set_side_pause.side_effect = Exception("boom")

        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = [
                {
                    "trade_id": "T",
                    "order_id": "O",
                    "asset_id": "A",
                    "price": 0.5,
                    "size": 10.0,
                    "market": "M",
                    "ts": 123.0,
                }
            ]
            monitor.check_buy_orders()  # must not raise

        assert ("T", "O") in monitor._seen_fill_keys
        assert monitor._after_ts == 123.0


class TestInitWatermark:
    """Watermark seeds from trades AND actions, so clearing the trades table
    does not reset it to 0 (which would refetch all history every restart)."""

    def test_seeds_from_actions_when_trades_empty(self):
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = [{"created_at": 555.0}]
        monitor.init_watermark()
        assert monitor._after_ts == 555.0

    def test_seeds_from_max_of_trades_and_actions(self):
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = [{"created_at": 100.0}]
        db.get_actions.return_value = [{"created_at": 555.0}]
        monitor.init_watermark()
        assert monitor._after_ts == 555.0

    def test_zero_when_both_empty(self):
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = []
        monitor.init_watermark()
        assert monitor._after_ts == 0.0

    def _buy_trade(self, tid, oid, asset="tok1", ts="100"):
        return {
            "id": tid,
            "market": "mkt1",
            "match_time": ts,
            "maker_orders": [
                {
                    "order_id": oid,
                    "maker_address": "0xFUNDER",
                    "side": "BUY",
                    "matched_amount": "100",
                    "price": "0.3",
                    "asset_id": asset,
                }
            ],
        }

    def test_seeds_seen_keys_from_existing_fills(self):
        # 启动恢复:把当前已存在的成交预灌进去重集合,使其能跨重启覆盖历史成交。
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = []
        api.get_trades.return_value = [self._buy_trade("T-old", "O-old")]
        monitor.init_watermark()
        assert ("T-old", "O-old") in monitor._seen_fill_keys

    def test_restart_does_not_reprocess_already_seen_fill(self):
        # 2026-05-29 实盘现象:重启后 _seen_fill_keys 本是空的 + 水位线只是粗下界,
        # 旧成交被 get_trades 重新捞回、当成"新成交"重放 -> 给已平仓市场重写
        # cancel_remainder / 重设冷却。init_watermark 预灌 seen 后,启动后不再重放。
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = []
        old_buy = self._buy_trade("T-old", "O-old")
        api.get_trades.return_value = [old_buy]

        monitor.init_watermark()  # 启动恢复:预灌 seen
        monitor.check_buy_orders()  # 下一 tick:同一笔旧成交不应再被处理

        db.set_side_pause.assert_not_called()
        api.cancel_orders.assert_not_called()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "cancel_remainder" not in ats

    def test_new_fill_after_startup_still_handled(self):
        # 反向保证:启动后真正新发生的成交照常处理(设冷却 + 撤余单)。
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = []
        api.get_trades.return_value = []  # 启动时无历史成交
        monitor.init_watermark()

        api.get_trades.return_value = [self._buy_trade("T-new", "O-new", ts="200")]
        api.get_open_orders.return_value = [{"id": "O-new"}]  # 新成交的单仍在挂
        monitor.check_buy_orders()

        db.set_side_pause.assert_called_once()
        api.cancel_orders.assert_called_once_with(["O-new"])
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "cancel_remainder" in ats

    def test_priming_uses_no_after_to_cover_all_fills(self):
        # priming 必须用无 after 查询稳拿全部历史成交:after 时间过滤不可靠,水位偏高时
        # priming 若也带 after 就漏灌、tick 又把历史成交捞回来重放(用户 2026-07-04 实盘:
        # 水位=今天,after=水位 拉回 0 -> seen 空 -> 洪水)。
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = [
            {"created_at": 9_000_000_000.0}
        ]  # 水位很高(今天之后)
        old = self._buy_trade("T", "O", ts="100")

        def by_after(params=None):
            # 复现:带 after 拉回空(漏),无 after 才拿到全部
            return [] if getattr(params, "after", None) else [old]

        api.get_trades.side_effect = by_after
        monitor.init_watermark()
        assert ("T", "O") in monitor._seen_fill_keys  # priming 无 after -> 灌到了
        params = api.get_trades.call_args.args[0]
        assert params.after is None  # priming 不带 after

    def test_flood_stops_when_after_filter_returns_history_despite_high_watermark(self):
        # 端到端复现:水位=今天,但 tick 的 after 被服务端忽略、仍返回 6 月历史成交。
        # priming 已用无 after 把它们灌进 seen -> tick 全被去重 -> 不重放(不再刷洪水)。
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = [{"created_at": 9_000_000_000.0}]  # 高水位=今天
        old = self._buy_trade("T-old", "O-old", ts="100")
        api.get_trades.return_value = [old]  # after 被忽略:无论传不传都返回历史成交
        monitor.init_watermark()
        monitor.check_buy_orders()
        db.set_side_pause.assert_not_called()
        api.cancel_orders.assert_not_called()

    def test_failed_priming_skips_fills_then_recovers_without_reprocess(self):
        # priming 失败(启动瞬间网络抖)时,check_buy_orders 先不处理成交(不重放历史),
        # 网络恢复后自动补 priming、把历史成交灌进去重集 -> 仍不重放。
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = []
        api.get_trades.side_effect = Exception("network down")
        monitor.init_watermark()  # priming 失败

        monitor.check_buy_orders()  # 网络仍断:不处理成交(不重放)
        db.set_side_pause.assert_not_called()
        api.cancel_orders.assert_not_called()

        api.get_trades.side_effect = None
        api.get_trades.return_value = [self._buy_trade("T-old", "O-old")]
        monitor.check_buy_orders()  # 网络恢复:补 priming -> 历史成交进 seen -> 不重放
        db.set_side_pause.assert_not_called()
        api.cancel_orders.assert_not_called()


# ---------------------------------------------------------------------------
# Step 3: check_sell_orders — strategy compliance on resting buys
# ---------------------------------------------------------------------------


class TestCheckSellOrders:
    def _ob(self, best_bid="0.48", best_ask="0.52", tick="0.01"):
        return {
            "bids": [{"price": best_bid, "size": "1000"}],
            "asks": [{"price": best_ask, "size": "1000"}],
            "tick_size": tick,
        }

    def test_keep_compliant_order(self):
        # price 0.48, mid=0.50, max_spread=3 → band [0.47, 0.53] → in-band → keep
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()

    def test_prefetch_takes_all_books_in_one_batch(self):
        """N 个在挂买单只发一次盘口请求,不再逐个单发。

        2026-07-29 实盘:51+ 挂单时 Step3 预取中位 10.7s、p90 17.3s,而判定本身
        只要几毫秒(相邻判定日志 90% 间隔 <10ms)——时间全耗在等 N 个单发往返上,
        把撤单延迟(= tick 耗时 + 5s)抬到 p99 22s。批量把 N 次往返压成 1 次。
        """
        monitor, api, db = _make_monitor()
        tokens = ["tok1", "tok2", "tok3"]
        api.get_open_orders.return_value = [
            {
                "id": f"o{i}",
                "side": "BUY",
                "asset_id": t,
                "market": f"cid{i}",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
            for i, t in enumerate(tokens)
        ]
        api.get_orderbooks.side_effect = None  # 覆盖 _make_monitor 的单发转发
        api.get_orderbooks.return_value = {t: self._ob() for t in tokens}
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        monitor.check_sell_orders()

        assert api.get_orderbook.call_count == 0, "不该再逐个单发盘口"
        assert api.get_orderbooks.call_count == 1, "所有盘口必须一次拿完"
        assert set(api.get_orderbooks.call_args[0][0]) == set(tokens)

    def test_cancel_outofband_order(self):
        # price 0.40 is outside reward band [0.47, 0.53] → cancel, no re-place
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
                "neg_risk": False,
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        monitor.check_sell_orders()

        api.cancel_orders.assert_called_with(["o1"])
        api.place_limit_buy.assert_not_called()

    def test_skip_sell_side_orders(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "SELL",
                "asset_id": "tok1",
                "size_matched": "0",
                "price": "0.60",
            }
        ]

        monitor.check_sell_orders()

        api.get_orderbook.assert_not_called()

    def test_skip_partially_filled_buy(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "size_matched": "100",
                "price": "0.48",
            }
        ]

        monitor.check_sell_orders()

        api.get_orderbook.assert_not_called()

    def test_get_open_orders_failure_returns_gracefully(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.side_effect = Exception("timeout")

        monitor.check_sell_orders()  # must not raise

        api.get_orderbook.assert_not_called()

    def test_empty_orderbook_skips_compliance(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()

    def test_uses_real_max_spread_from_rewards_api(self):
        # max_spread=3 from API: mid=0.50 → band [0.47, 0.53]; price 0.48 in-band → keep
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()

    def test_rewards_cache_hit_single_api_call(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            },
            {
                "id": "o2",
                "side": "BUY",
                "asset_id": "tok2",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            },
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        monitor.check_sell_orders()

        assert api.get_rewards_for_market.call_count == 1

    def test_skip_when_rewards_api_fails(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            },
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.side_effect = Exception("boom")

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()
        api.place_limit_buy.assert_not_called()

    def test_skip_when_max_spread_unparseable(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            },
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{}]

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()
        api.place_limit_buy.assert_not_called()

    def test_log_outofband_cancel_has_detail(self, caplog):
        # price 0.40 out of reward band [0.47, 0.53] → logged cancel
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
                "neg_risk": False,
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor.check_sell_orders()
        text = caplog.text
        assert "[Step3]" in text
        assert "o1" in text and "cid1" in text
        assert "cancel" in text

    def test_log_keep_has_detail(self, caplog):
        # price 0.48 in reward band [0.47, 0.53] → logged keep
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "keep" in caplog.text

    def test_log_skip_when_max_spread_unknown(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{}]
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "rewards_max_spread" in caplog.text
        assert "跳过" in caplog.text

    def test_log_skip_when_empty_orderbook(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "盘口为空" in caplog.text


class TestMonitorStatusSnapshot:
    @pytest.fixture(autouse=True)
    def _clean_snap(self):
        clear_snapshot()
        yield
        clear_snapshot()

    def _ob(self):
        return {
            "bids": [{"price": "0.48", "size": "1000"}],
            "asks": [{"price": "0.52", "size": "1000"}],
            "tick_size": "0.01",
        }

    def test_step3_keep_records_row(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        r = next(x for x in rows if x.get("stage") == "Step3")
        assert r["wallet"] == "0xABC"
        assert "keep" in r["action"]
        assert "cid1" in r["market"]

    def test_step3_skip_empty_orderbook_records_row(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        assert any("盘口为空" in x.get("action", "") for x in rows)

    def test_sell_order_gets_a_row(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "s1",
                "side": "SELL",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.60",
                "original_size": "200",
            }
        ]
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        r = next(x for x in rows if x["side"] == "卖出")
        assert r["stage"] == "止盈卖单"
        assert r["action"] == "挂单中"

    def test_step1_fill_records_row(self):
        monitor, api, db = _make_monitor()
        db.get_settings.return_value = {
            "cooldown_minutes": 20,
            "rewards_cache_ttl_sec": 600,
        }
        monitor.begin_status_tick()
        monitor._handle_fill(
            {
                "size": 120,
                "price": 0.60,
                "asset_id": "tok1",
                "market": "cid1",
                "order_id": "o9",
            },
            set(),
            {"o9"},  # open_ids:该单仍在挂
        )
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        r = next(x for x in rows if x["stage"] == "Step1")
        assert "成交" in r["action"]
        assert r["market"] == "cid1"
        assert r["wallet"] == "0xABC"

    def test_begin_tick_clears_previous_rows(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "s1",
                "side": "SELL",
                "asset_id": "t",
                "market": "c",
                "size_matched": "0",
                "price": "0.6",
                "original_size": "1",
            }
        ]
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        first = len(get_snapshot()["rows"])
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        assert len(get_snapshot()["rows"]) == first
        assert len(get_snapshot()["rows"]) == 1


class TestStep1ActionLog:
    def test_no_sell_no_cancel_when_no_order_id(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        with patch("engine.monitor.select_new_buy_fills") as mf:
            mf.return_value = [
                {
                    "trade_id": "t1",
                    "order_id": None,
                    "asset_id": "tok1",
                    "price": 0.25,
                    "size": 100.0,
                    "market": "mkt1",
                    "ts": 1.0,
                }
            ]
            monitor.check_buy_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "take_profit_sell" not in ats
        assert "cancel_remainder" not in ats
        api.place_limit_sell.assert_not_called()

    def test_record_action_never_breaks_fill(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        api.get_open_orders.return_value = [{"id": "o1"}]  # 该单仍在挂 -> 会撤余量
        db.record_action.side_effect = RuntimeError("db down")
        with patch("engine.monitor.select_new_buy_fills") as mf:
            mf.return_value = [
                {
                    "trade_id": "t1",
                    "order_id": "o1",
                    "asset_id": "tok1",
                    "price": 0.25,
                    "size": 100.0,
                    "market": "mkt1",
                    "ts": 1.0,
                }
            ]
            monitor.check_buy_orders()  # must not raise
        api.place_limit_sell.assert_not_called()
        db.record_trade.assert_not_called()
        api.cancel_orders.assert_called_once_with(["o1"])


class TestStep3ActionLog:
    def _ob(self):
        return {
            "bids": [{"price": "0.48", "size": "1000"}],
            "asks": [{"price": "0.52", "size": "1000"}],
            "tick_size": "0.01",
        }

    def _order(self, price="0.40"):
        return {
            "id": "o1",
            "side": "BUY",
            "asset_id": "tok1",
            "market": "cid1",
            "size_matched": "0",
            "price": price,
            "original_size": "500",
            "neg_risk": False,
        }

    def test_outofband_records_cancel_action(self):
        # price 0.40 out of reward band → step3_cancel_outofband, no re-place
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["step3_cancel_outofband"]
        call = db.record_action.call_args_list[0]
        assert call.kwargs["side"] == "-"
        assert call.kwargs["price"] == -1
        assert "奖励区间" in call.kwargs["reason"]
        api.place_limit_buy.assert_not_called()

    def _ob_cliff(self, extra_bid=None):
        # 买一 0.48 / 卖一 0.52 -> mid 0.50、max_spread 3 -> 奖励区间 [0.47,0.53],
        # 下沿 0.47。extra_bid 落在 [0.45,0.47) 带内即为支撑,否则下方直通 0.02 = 悬崖。
        bids = [{"price": "0.48", "size": "1000"}]
        if extra_bid:
            bids.append({"price": extra_bid, "size": "300"})
        bids.append({"price": "0.02", "size": "9999"})
        return {
            "bids": bids,
            "asks": [{"price": "0.52", "size": "1000"}],
            "tick_size": "0.01",
        }

    def test_cliff_below_band_cancels_resting_buy(self):
        # 挂单价 0.48 在奖励区间内(出界闸门不撤它),但下沿往下 2¢ 内无买档 -> 悬崖 -> 撤。
        # 下单时判过一次还不够:预算被持仓吃空的市场整轮 continue(manager.py:452),
        # 那条路径下在挂单永远走不到下单轮的判定,只有 Step3 复查够得着。
        monitor, api, db = _make_monitor({"cliff_probe_cents": 2})
        api.get_open_orders.return_value = [self._order(price="0.48")]
        api.get_orderbook.return_value = self._ob_cliff()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["cliff_cancel"]
        assert "悬崖" in db.record_action.call_args_list[0].kwargs["reason"]
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()

    def test_cliff_support_within_band_keeps_resting_buy(self):
        # 0.46 落在 [0.45,0.47) 带内 -> 有支撑 -> 保持不动。
        monitor, api, db = _make_monitor({"cliff_probe_cents": 2})
        api.get_open_orders.return_value = [self._order(price="0.48")]
        api.get_orderbook.return_value = self._ob_cliff(extra_bid="0.46")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_cliff_disabled_keeps_resting_buy(self):
        # cliff_probe_cents=0 -> 整段不查(零回归),同样的悬崖盘口也不撤。
        monitor, api, db = _make_monitor({"cliff_probe_cents": 0})
        api.get_open_orders.return_value = [self._order(price="0.48")]
        api.get_orderbook.return_value = self._ob_cliff()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_inband_records_no_action(self):
        # price 0.48 in reward band [0.47, 0.53] → keep, no action recorded
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order(price="0.48")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_empty_orderbook_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_no_max_spread_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{}]
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_partial_fill_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "100",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_cancel_failure_records_no_action(self):
        # If the cancel API call fails, no action should be recorded
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.cancel_orders.side_effect = RuntimeError("network")
        monitor.check_sell_orders()  # must not raise
        db.record_action.assert_not_called()


class TestStep3PriceBand:
    """调单算出的目标价超出配置「单价区间」[min,max]美分时，撤买单不重挂。"""

    SETTINGS = {"min_price_cents": 10.0, "max_price_cents": 50.0}

    def _ob(self, best_bid="0.48", best_ask="0.52", tick="0.01"):
        return {
            "bids": [{"price": best_bid, "size": "1000"}],
            "asks": [{"price": best_ask, "size": "1000"}],
            "tick_size": tick,
        }

    def _order(self, price="0.48"):
        return {
            "id": "o1",
            "side": "BUY",
            "asset_id": "tok1",
            "market": "cid1",
            "size_matched": "0",
            "price": price,
            "original_size": "500",
            "neg_risk": False,
        }

    def test_cancels_when_price_above_band(self):
        # 挂单价 0.55 = 55c > 50c 上限：price_band_cancel，不重挂。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.55")]
        api.get_orderbook.return_value = self._ob(best_bid="0.55", best_ask="0.56")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["price_band_cancel"]
        call = db.record_action.call_args_list[0]
        assert call.kwargs["side"] == "-"
        assert call.kwargs["price"] == -1
        assert "单价区间" in call.kwargs["reason"]

    def test_cancels_when_price_below_band(self):
        # 挂单价 0.08 = 8c < 10c 下限：price_band_cancel，不重挂。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.08")]
        api.get_orderbook.return_value = self._ob(best_bid="0.08", best_ask="0.09")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()

    def test_keeps_when_price_at_band_edge(self):
        # 含端点：挂单价 0.50 = 50c 恰在上限，且在奖励区间内，保持不动。
        # mid = (0.50+0.51)/2 = 0.505; max_spread=3 → band [0.475, 0.535]; 0.50 in-band.
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.50")]
        api.get_orderbook.return_value = self._ob(best_bid="0.50", best_ask="0.51")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()

    def test_within_price_band_and_reward_band_keeps(self):
        # 挂单价 0.48 在单价区间 [10c,50c] 内，且在奖励区间内：保持不动。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.48")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()
        api.place_limit_buy.assert_not_called()

    def test_no_band_keys_is_noop(self):
        # 默认 settings 不含价格区间键：price_band 闸门 no-op。
        # 挂单价 0.40 在奖励区间外 → step3_cancel_outofband。
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order(price="0.40")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["step3_cancel_outofband"]
        api.place_limit_buy.assert_not_called()


class TestStep3EligibilityRecheck:
    SETTINGS = {"max_spread_cents": 3.0}

    def _ob(self, best_bid="0.30", best_ask="0.31", tick="0.01"):
        return {
            "bids": [{"price": best_bid, "size": "1000"}],
            "asks": [{"price": best_ask, "size": "1000"}],
            "tick_size": tick,
        }

    def _buy(self):
        return [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.30",
                "original_size": "500",
            }
        ]

    def test_cancels_when_spread_at_or_over_threshold(self):
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(
            best_bid="0.30", best_ask="0.35"
        )  # 5c >= 3c
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()

    def test_records_eligibility_cancel_action(self):
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(best_bid="0.30", best_ask="0.36")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        ats = [c.kwargs.get("action_type") for c in db.record_action.call_args_list]
        assert "eligibility_cancel" in ats

    def test_eligibility_cancel_failure_does_not_record_action(self):
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(best_bid="0.30", best_ask="0.36")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.cancel_orders.side_effect = Exception("network error")
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_keeps_and_runs_compliance_when_spread_narrow(self):
        # buy price=0.30, bid=0.30, ask=0.31 → mid=0.305, band [0.275, 0.335] → in-band → keep
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(
            best_bid="0.30", best_ask="0.31"
        )  # 1c < 3c
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()

    def test_dropped_dimensions_do_not_cause_cancel(self):
        # buy price=0.30, bid=0.60, ask=0.61 → 1c spread → passes eligibility.
        # mid=0.605, max_spread=3 → band [0.575, 0.635]. Order price 0.30 is out
        # of reward band → step3_cancel_outofband (not eligibility_cancel).
        # The comment below preserves the original intent: spread check alone does
        # not cancel when spread is narrow, even if the price seems off.
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(
            best_bid="0.60", best_ask="0.61"
        )  # 1c spread
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        # eligibility cancel must NOT fire (spread 1c < threshold 3c)
        ats = [c.kwargs.get("action_type") for c in db.record_action.call_args_list]
        assert "eligibility_cancel" not in ats


class TestCostHelper:
    def _buy_trade(self, asset, price, size, ts="100"):
        return {
            "id": f"t-{asset}-{ts}",
            "market": "mkt1",
            "match_time": ts,
            "maker_orders": [
                {
                    "order_id": f"o-{asset}-{ts}",
                    "maker_address": "0xFUNDER",
                    "side": "BUY",
                    "matched_amount": str(size),
                    "price": str(price),
                    "asset_id": asset,
                }
            ],
        }

    def test_cost_from_get_trades_weighted(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = [
            self._buy_trade("tok1", 0.20, 200, ts="1"),
            self._buy_trade("tok1", 0.28, 200, ts="2"),
        ]
        monitor.begin_status_tick()
        cost, lots = monitor._cost_lots("tok1", 400, "mkt1")
        assert cost == pytest.approx(0.24)

    def test_cost_cached_within_tick(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = [self._buy_trade("tok1", 0.28, 361)]
        monitor.begin_status_tick()
        monitor._cost_lots("tok1", 361, "mkt1")
        monitor._cost_lots("tok1", 361, "mkt1")
        assert api.get_trades.call_count == 1  # 同 tick 只取一次

    def test_cost_retries_self_fetch_when_prefetch_failed(self):
        # 预取表里键在、值是 None = 那一路取数失败。不能直接判成本未知:2026-07-29 实盘
        # 810 次 ⚠️裸奔跳过里有 789 次(97.4%)紧跟着一次 [离场预取] get_trades 失败,
        # 一次网络抖动就被直接翻译成整仓无保护。回退自取一次,真取不到才算成本未知。
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._trades_by_cid = {"mkt1": None}
        api.get_trades.return_value = [self._buy_trade("tok1", 0.28, 361)]
        cost, lots = monitor._cost_lots("tok1", 361, "mkt1")
        assert cost == pytest.approx(0.28)
        assert api.get_trades.call_count == 1

    def test_cost_none_when_prefetch_failed_and_self_fetch_also_fails(self):
        # 自取也失败 -> 成本仍然未知(跳过 + ⚠️裸奔),绝不拿不确定的成本去卖。
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._trades_by_cid = {"mkt1": None}
        api.get_trades.side_effect = Exception("boom")
        cost, lots = monitor._cost_lots("tok1", 361, "mkt1")
        assert cost is None

    def test_cost_none_when_get_trades_fails(self):
        monitor, api, db = _make_monitor()
        api.get_trades.side_effect = Exception("boom")
        monitor.begin_status_tick()
        cost, lots = monitor._cost_lots("tok1", 361, "mkt1")
        assert cost is None

    def test_cost_none_when_no_buy_fills(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        monitor.begin_status_tick()
        cost, lots = monitor._cost_lots("tok1", 361, "mkt1")
        assert cost is None

    def test_cost_uses_market_filter_when_asset_filter_broken(self):
        # 真实事故(2026-05-29):Polymarket /trades 的服务端 asset_id 过滤失效——
        # 同一 asset 明明有成交,按 asset_id 查却返回空;按 market(conditionId)查
        # 或全量查能拿到。_cost_lots 必须按 market 过滤,否则成本永远算不出 -> 全仓
        # 裸奔。本测试用 side_effect 复现该 server 行为:asset_id 查询返回空,market
        # 查询返回真实成交,断言成本仍能被重建(0.45 / 263 股)。
        monitor, api, db = _make_monitor()
        buy = self._buy_trade("tok1", 0.45, 263, ts="1")  # market="mkt1"

        def fake_get_trades(params):
            if getattr(params, "asset_id", None):  # 服务端 asset 过滤:坏的
                return []
            if getattr(params, "market", None) == "mkt1":  # market 过滤:可用
                return [buy]
            return []

        api.get_trades.side_effect = fake_get_trades
        monitor.begin_status_tick()
        cost, lots = monitor._cost_lots("tok1", 263, "mkt1")
        assert cost == pytest.approx(0.45)


class TestStep3Blacklist:
    def test_check_compliance_cancels_blacklisted_buy_no_replace(self):
        monitor, api, db = _make_monitor()
        db.get_blacklist_ids.return_value = {"cid1"}
        o = {
            "asset_id": "t1",
            "market": "cid1",
            "id": "o1",
            "price": "0.40",
            "original_size": "100",
            "size_matched": "0",
        }
        monitor._check_compliance(o, {"cid1"}, {}, {}, {})
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()
        api.get_orderbook.assert_not_called()  # 命中黑名单提前返回,不取盘口


class TestMonitorReadsTemplate:
    def test_check_exit_reads_template(self):
        from engine.monitor import OrderMonitor
        from unittest.mock import MagicMock

        db = MagicMock()
        db.get_template_for.return_value = {
            "theta_loss_cents": 2,
            "theta_stop_cents": 5,
            "case_a_mode": "ask",
        }
        db.get_settings.return_value = {
            "cooldown_minutes": 20,
            "rewards_cache_ttl_sec": 600,
        }
        api = MagicMock()
        api.get_user_positions.return_value = []
        mon = OrderMonitor(api, db, "0xW")
        mon.check_exit()
        db.get_template_for.assert_called_with("0xW")


class TestCheckExit:
    def _setup(
        self,
        cost,
        size,
        bids,
        asks,
        sells=None,
        mode="ask",
        theta_loss=2,
        theta_stop=5,
        cur_price=None,
        stop_mode="fixed",
        stop_percent=20,
        tick="0.01",
    ):
        monitor, api, db = _make_monitor(
            settings={
                "theta_loss_cents": theta_loss,
                "theta_stop_cents": theta_stop,
                "stop_loss_mode": stop_mode,
                "stop_loss_percent": stop_percent,
                "case_a_mode": mode,
            }
        )
        api.get_user_positions.return_value = [
            {
                "asset": "A-y",
                "size": size,
                "curPrice": (
                    cur_price if cur_price is not None else (bids[0][0] if bids else 0)
                ),
                "conditionId": "A",
            }
        ]
        api.get_open_orders.return_value = sells or []
        api.get_orderbook.return_value = {
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
            "tick_size": tick,
        }
        monitor._cost_lots = lambda a, s, c: (
            cost,
            [{"price": cost, "take": s, "ts": 0, "trade_id": "t"}],
        )
        return monitor, api, db

    def test_case_a_rests_one_sell_at_ask(self):
        # 成本 0.30 ≤ 买一 0.31(盈利)-> 挂卖一 0.33 做 maker 捕价差(不再按成本/买一甩)。
        monitor, api, db = self._setup(0.30, 100, [(0.31, 500)], [(0.33, 500)])
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        a, k = api.place_limit_sell.call_args
        assert a[0] == "A-y" and abs(a[1] - 0.33) < 1e-9 and a[2] == 100
        api.place_market_sell.assert_not_called()

    def test_case_a_mode_ignored_still_rests_at_ask(self):
        # case_a_mode 已死:即便 mode="market" 盈利侧也仍挂卖一,不市价。
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], mode="market"
        )
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        a, k = api.place_limit_sell.call_args
        assert abs(a[1] - 0.33) < 1e-9
        api.place_market_sell.assert_not_called()

    def test_b0_market_clear_and_record(self):
        monitor, api, db = self._setup(0.40, 100, [(0.35, 500)], [(0.36, 500)])
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100, tick_size="0.01")
        db.record_trade.assert_called_once()

    def test_b0_market_sell_passes_real_tick_size(self):
        # 0.1¢ 市场:B0 市价清仓必须把盘口真实 tick 传给下单接口。漏传时客户端按默认
        # 0.01 的价格网格校验,算出的 0.001 被判 invalid price(min 0.01),止损单根本
        # 发不出去、持仓一直裸奔(2026-07-29 实盘 385 次)。
        monitor, api, db = self._setup(
            0.40, 100, [(0.35, 500)], [(0.36, 500)], tick="0.001"
        )
        monitor.check_exit()
        api.place_market_sell.assert_called_once()
        _, k = api.place_market_sell.call_args
        assert k.get("tick_size") == "0.001"

    def test_b0_percent_mode_triggers_at_cost_pct(self):
        # 按比例 20%:成本 0.40 -> 阈值 0.08。买一 0.31(亏 0.09 ≥ 0.08)-> B0 市价清仓。
        monitor, api, db = self._setup(
            0.40,
            100,
            [(0.31, 500)],
            [(0.32, 500)],
            stop_mode="percent",
            stop_percent=20,
        )
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100, tick_size="0.01")

    def test_b0_percent_mode_holds_within_threshold(self):
        # 按比例 20%:成本 0.40 -> 阈值 0.08。买一 0.35(亏 0.05 < 0.08)-> 不强平,挂成本价。
        # (同样的盘口在固定 5¢ 模式下亏 0.05≥0.05 会强平 -> 证明模式确实改变了行为。)
        monitor, api, db = self._setup(
            0.40,
            100,
            [(0.35, 500)],
            [(0.36, 500)],
            stop_mode="percent",
            stop_percent=20,
        )
        monitor.check_exit()
        api.place_market_sell.assert_not_called()
        api.place_limit_sell.assert_called_once()

    def test_b0_records_real_fill_not_curprice(self):
        # 市价清仓:成本 0.1133、买一 0.06(亏 5.33¢≥5¢ 触发 B0),但 Data API 现价抽风显示 0.13。
        # 记录价必须是真实成交价≈买一 0.06(显示为亏损),绝不能是现价 0.13(会把亏损显示成盈利)。
        monitor, api, db = self._setup(
            0.1133, 60, [(0.06, 500)], [(0.20, 500)], cur_price=0.13
        )
        rows = []
        monitor._status_add = lambda **kw: rows.append(kw)
        monitor.check_exit()
        api.place_market_sell.assert_called_once()
        _, k = db.record_trade.call_args
        assert abs(k["price"] - 0.06) < 1e-9  # 真实成交价≈买一,不是现价 0.13
        assert k["pnl"] < 0  # 必须显示为亏损
        # 状态行也用真实成交价,不用现价
        assert any(r.get("price") == "0.0600" for r in rows), rows

    def test_sell_rejection_emits_naked_warning_not_silent(self):
        # 止损卖被 CLOB 拒(OrderRejected)时,不能静默裸奔:check_exit 不抛,
        # 且必须留下含「裸奔/失败」的 ⚠️ 状态行,让用户在监控页看见未受保护。
        from api.polymarket_api import OrderRejected

        monitor, api, db = self._setup(0.40, 100, [(0.35, 500)], [(0.36, 500)])
        api.place_market_sell.side_effect = OrderRejected("市价卖单 被拒")
        rows = []
        monitor._status_add = lambda **kw: rows.append(kw)
        monitor.check_exit()  # 不应抛
        assert any(
            ("裸奔" in (r.get("action") or "")) or ("失败" in (r.get("action") or ""))
            for r in rows
        ), f"无告警状态行,静默裸奔: {rows}"

    def test_b0_cancels_existing_sell_before_market_clear(self):
        # 已有一笔挂卖单 -> B0 必须先撤再市价清仓(避免与残留卖单并存/重复卖)。
        sells = [
            {
                "id": "s1",
                "asset_id": "A-y",
                "side": "SELL",
                "price": "0.42",
                "original_size": "100",
                "size_matched": "0",
            }
        ]
        calls = []
        monitor, api, db = self._setup(
            0.40, 100, [(0.35, 500)], [(0.36, 500)], sells=sells
        )
        api.cancel_orders.side_effect = lambda ids: calls.append(("cancel", ids))
        api.place_market_sell.side_effect = lambda a, s, **kw: calls.append(
            ("market", a, s)
        )
        monitor.check_exit()
        assert calls[0][0] == "cancel" and "s1" in calls[0][1]
        assert calls[1][0] == "market"

    def test_profit_rest_does_not_record_trade(self):
        # 盈利挂成本价不是止损,不应落 stop_loss 记录。
        monitor, api, db = self._setup(0.30, 100, [(0.31, 500)], [(0.33, 500)])
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        db.record_trade.assert_not_called()

    def test_underwater_within_stop_rests_at_cost_no_sweep(self):
        # 成本 0.40 > 买一 0.39,亏损 0.01 < theta_stop -> 挂成本价 0.40(等回本,不跟卖一 0.41)。
        monitor, api, db = self._setup(0.40, 100, [(0.39, 500)], [(0.41, 500)])
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        a, k = api.place_limit_sell.call_args
        assert abs(a[1] - 0.40) < 1e-9
        api.place_marketable_limit_sell.assert_not_called()
        api.place_market_sell.assert_not_called()

    def test_b_park_rests_at_cost(self):
        # 套牢(成本 0.40 > 买一 0.37),亏损 0.03 < theta_stop -> 挂成本价 0.40 等回本。
        monitor, api, db = self._setup(0.40, 100, [(0.37, 500)], [(0.41, 500)])
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        a, k = api.place_limit_sell.call_args
        assert abs(a[1] - 0.40) < 1e-9
        api.place_market_sell.assert_not_called()

    def test_exit_never_places_sell_below_cost_crossed_book(self):
        # 硬底线:异常/幻影盘口——买一 0.16≥成本(误判盈利)、卖一 0.13<成本 0.14。
        # 挂卖价必须 ≥ 成本 0.14,绝不挂在 0.13(疑似 0.13 卖根因 B 的端到端护栏)。
        monitor, api, db = self._setup(0.14, 50, [(0.16, 500)], [(0.13, 500)])
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        a, k = api.place_limit_sell.call_args
        assert a[1] >= 0.14 - 1e-9, f"挂卖价 {a[1]} 低于成本 0.14"
        api.place_market_sell.assert_not_called()

    def test_naked_skips_no_sell(self):
        monitor, api, db = self._setup(0.30, 100, [(0.31, 500)], [(0.33, 500)])
        monitor._cost_lots = lambda a, s, c: (None, [])
        monitor.check_exit()
        api.place_limit_sell.assert_not_called()
        api.place_market_sell.assert_not_called()
        api.place_marketable_limit_sell.assert_not_called()

    def test_rest_keeps_existing_matching_sell(self):
        # 已有一笔挂在卖一 0.33 的卖单 -> 盈利挂卖一 want=0.33 与之吻合 -> 保持不动。
        sells = [
            {
                "id": "s1",
                "asset_id": "A-y",
                "side": "SELL",
                "price": "0.33",
                "original_size": "100",
                "size_matched": "0",
            }
        ]
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], sells=sells
        )
        monitor.check_exit()
        api.place_limit_sell.assert_not_called()
        api.cancel_orders.assert_not_called()


# ---------------------------------------------------------------------------
# UMA resolution guard: check_resolution — 有人在 UMA 提交即撤该市场全部买单
# ---------------------------------------------------------------------------


class TestCheckResolution:
    def _buy(self, oid, cid, asset="tok"):
        return {
            "id": oid,
            "side": "BUY",
            "asset_id": asset,
            "market": cid,
            "size_matched": "0",
            "price": "0.30",
            "original_size": "500",
        }

    def test_cancels_buys_only_for_market_in_resolution(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            self._buy("o1", "cid1"),
            self._buy("o2", "cid1"),
            self._buy("o3", "cid2"),
        ]
        api.gamma_resolution_status.return_value = {"cid1": "proposed", "cid2": None}
        monitor.check_resolution()
        api.cancel_orders.assert_called_once_with(["o1", "o2"])

    def test_records_action_for_resolution_cancel(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._buy("o1", "cid1")]
        api.gamma_resolution_status.return_value = {"cid1": "disputed"}
        monitor.check_resolution()
        ats = [c.kwargs.get("action_type") for c in db.record_action.call_args_list]
        assert "uma_resolution_cancel" in ats

    def test_does_not_cancel_when_not_in_resolution(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._buy("o1", "cid1")]
        api.gamma_resolution_status.return_value = {"cid1": None}
        monitor.check_resolution()
        api.cancel_orders.assert_not_called()

    def test_gamma_failure_fails_open_no_cancel(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._buy("o1", "cid1")]
        api.gamma_resolution_status.return_value = {}  # fail-open -> 不撤
        monitor.check_resolution()
        api.cancel_orders.assert_not_called()

    def test_no_buy_orders_skips_gamma_and_cancel(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "s1", "side": "SELL", "market": "cid1"}
        ]
        monitor.check_resolution()
        api.gamma_resolution_status.assert_not_called()
        api.cancel_orders.assert_not_called()

    def test_cancel_failure_does_not_record_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._buy("o1", "cid1")]
        api.gamma_resolution_status.return_value = {"cid1": "proposed"}
        api.cancel_orders.side_effect = Exception("network")
        monitor.check_resolution()
        ats = [c.kwargs.get("action_type") for c in db.record_action.call_args_list]
        assert "uma_resolution_cancel" not in ats


class TestExitTakeProfitMode:
    """离场 take_profit_mode=market + 止损关(stop_loss_mode=off)的接线行为。"""

    def _pos(self):
        return {
            "asset": "tok1",
            "size": 100.0,
            "curPrice": 0.30,
            "conditionId": "mkt1",
        }

    def test_market_mode_profit_market_sells(self):
        monitor, api, db = _make_monitor(
            {"take_profit_mode": "market", "stop_loss_mode": "off"}
        )
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = []
        monitor._cost_lots = MagicMock(return_value=(0.30, []))
        # 买一 0.35 > 成本 0.30 -> 浮盈。
        monitor._sell_book = MagicMock(return_value=(0.01, "0.01", 0.35, 0.40))
        monitor.check_exit()
        api.place_market_sell.assert_called_once()
        api.place_limit_sell.assert_not_called()

    def test_market_mode_underwater_stop_off_rests_at_cost(self):
        monitor, api, db = _make_monitor(
            {"take_profit_mode": "market", "stop_loss_mode": "off"}
        )
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = []
        monitor._cost_lots = MagicMock(return_value=(0.40, []))
        # 买一 0.30 < 成本 0.40 -> 套牢;止损关 -> 挂成本价,不 B0。
        monitor._sell_book = MagicMock(return_value=(0.01, "0.01", 0.30, 0.42))
        monitor.check_exit()
        api.place_market_sell.assert_not_called()
        api.place_limit_sell.assert_called_once()
        args, kwargs = api.place_limit_sell.call_args
        assert abs(args[1] - 0.40) < 1e-9


# ---------------------------------------------------------------------------
# 结算守卫(持仓侧):umaResolutionStatus 非空 -> 该持仓无视盈亏市价清仓
# ---------------------------------------------------------------------------


class TestResolutionExit:
    """结算(umaResolutionStatus 非空)时,该市场持仓无视盈亏市价清仓。"""

    def _setup(
        self,
        cost,
        size,
        bids,
        asks,
        sells=None,
        cur_price=None,
        gamma=None,
        tick="0.01",
    ):
        monitor, api, db = _make_monitor(
            settings={
                "theta_loss_cents": 2,
                "theta_stop_cents": 5,
                "stop_loss_mode": "fixed",
                "stop_loss_percent": 20,
                "case_a_mode": "ask",
            }
        )
        api.get_user_positions.return_value = [
            {
                "asset": "A-y",
                "size": size,
                "curPrice": (
                    cur_price if cur_price is not None else (bids[0][0] if bids else 0)
                ),
                "conditionId": "A",
            }
        ]
        api.get_open_orders.return_value = sells or []
        api.get_orderbook.return_value = {
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
            "tick_size": tick,
        }
        api.gamma_resolution_status.return_value = gamma if gamma is not None else {}
        monitor._cost_lots = lambda a, s, c: (
            cost,
            [{"price": cost, "take": s, "ts": 0, "trade_id": "t"}],
        )
        return monitor, api, db

    def test_resolving_market_market_sells_not_rests(self):
        # 结果已提交 + 盈利持仓:仍市价清仓(不再挂 maker 卖单等价差)。
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], gamma={"A": "proposed"}
        )
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100, tick_size="0.01")
        api.place_limit_sell.assert_not_called()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "exit_market" in ats
        db.record_trade.assert_called_once()  # 成本已知 -> 记 pnl

    def test_resolving_market_sell_passes_real_tick_size(self):
        # 0.1¢ 市场的结算清仓同样要传真实 tick,否则市价卖被客户端拦下 -> 仓等着被结算成 0。
        monitor, api, db = self._setup(
            0.30,
            100,
            [(0.31, 500)],
            [(0.33, 500)],
            gamma={"A": "proposed"},
            tick="0.001",
        )
        monitor.check_exit()
        api.place_market_sell.assert_called_once()
        _, k = api.place_market_sell.call_args
        assert k.get("tick_size") == "0.001"

    def test_resolving_cancels_resting_sell_before_market(self):
        # 已有挂卖单 -> 必须先撤再市价卖(否则挂卖单占用份额,市价卖没份额可卖)。
        sells = [
            {
                "id": "s1",
                "asset_id": "A-y",
                "side": "SELL",
                "price": "0.42",
                "original_size": "100",
                "size_matched": "0",
            }
        ]
        calls = []
        monitor, api, db = self._setup(
            0.30,
            100,
            [(0.31, 500)],
            [(0.33, 500)],
            sells=sells,
            gamma={"A": "proposed"},
        )
        api.cancel_orders.side_effect = lambda ids: calls.append(("cancel", ids))
        api.place_market_sell.side_effect = lambda a, s, **kw: calls.append(
            ("market", a, s)
        )
        monitor.check_exit()
        assert calls[0][0] == "cancel" and "s1" in calls[0][1]
        assert calls[1][0] == "market"

    def test_resolving_cost_unknown_still_market_sells(self):
        # 成本重建失败(get_trades 无买入成交):正常离场会「跳过·裸奔」,但结算市场必须照卖。
        monitor, api, db = self._setup(
            0.30, 100, [(0.06, 500)], [(0.20, 500)], gamma={"A": "proposed"}
        )
        monitor._cost_lots = lambda a, s, c: (None, [])
        rows = []
        monitor._status_add = lambda **kw: rows.append(kw)
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100, tick_size="0.01")
        db.record_trade.assert_not_called()  # 成本未知 -> 不记 pnl
        assert any("结算" in (r.get("action") or "") for r in rows), rows
        assert not any("跳过" in (r.get("action") or "") for r in rows), rows

    def test_resolving_records_real_fill_not_curprice(self):
        # 记录价必须是真实成交价≈买一 0.06,绝不用抽风现价 0.13。
        monitor, api, db = self._setup(
            0.1133,
            60,
            [(0.06, 500)],
            [(0.20, 500)],
            cur_price=0.13,
            gamma={"A": "proposed"},
        )
        monitor.check_exit()
        api.place_market_sell.assert_called_once()
        _, k = db.record_trade.call_args
        assert abs(k["price"] - 0.06) < 1e-9

    def test_gamma_empty_falls_back_to_normal_exit(self):
        # fail-open:Gamma 返回 {} -> 走原离场(盈利挂 maker 卖一,不市价)。
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], gamma={}
        )
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        api.place_market_sell.assert_not_called()

    def test_non_resolving_status_none_normal_exit(self):
        # umaResolutionStatus 为 None(正常交易)-> 原离场,不市价。
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], gamma={"A": None}
        )
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        api.place_market_sell.assert_not_called()

    def test_market_sell_rejection_emits_naked_warning(self):
        # 市价清仓被 CLOB 拒 -> 不静默裸奔:不抛,且留含「裸奔」的 ⚠️ 状态行。
        from api.polymarket_api import OrderRejected

        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], gamma={"A": "proposed"}
        )
        api.place_market_sell.side_effect = OrderRejected("市价卖单 被拒")
        rows = []
        monitor._status_add = lambda **kw: rows.append(kw)
        monitor.check_exit()  # 不应抛
        assert any("裸奔" in (r.get("action") or "") for r in rows), rows

    def test_resolving_no_bid_skips_without_phantom_record(self):
        # 结算市场但盘口无买盘:市价卖不出,不能记幻影成交、更不能退回 Data API 现价。
        # 不卖不记,发 ⚠️ 无买盘状态行,待有买盘再清。
        monitor, api, db = self._setup(
            0.30, 100, [], [(0.33, 500)], cur_price=0.13, gamma={"A": "proposed"}
        )
        rows = []
        monitor._status_add = lambda **kw: rows.append(kw)
        monitor.check_exit()
        api.place_market_sell.assert_not_called()
        db.record_trade.assert_not_called()
        assert any("无买盘" in (r.get("action") or "") for r in rows), rows

    def test_resolving_zero_bid_also_skips(self):
        # 买一==0(退化盘口):market_fill_price 仅在 best_bid>0 才取买一,否则退回被禁现价。
        # 守卫用 <=0(不只是 is None)拦下,同样不卖不记,避免幻影成交按现价落库。
        monitor, api, db = self._setup(
            0.30,
            100,
            [(0, 500)],
            [(0.33, 500)],
            cur_price=0.13,
            gamma={"A": "proposed"},
        )
        rows = []
        monitor._status_add = lambda **kw: rows.append(kw)
        monitor.check_exit()
        api.place_market_sell.assert_not_called()
        db.record_trade.assert_not_called()
        assert any("无买盘" in (r.get("action") or "") for r in rows), rows


class TestLowBalance:
    def _mon(
        self,
        balance,
        positions,
        daily_rewards,
        costs,
        bids,
        threshold=4,
        target_usd=4,
        mode="balance",
        tick="0.01",
    ):
        monitor, api, db = _make_monitor(
            settings={
                "low_balance_threshold_usd": threshold,
                "low_reward_threshold_usd": 30,
                "small_position_shares": 20,
                "liquidate_target_mode": mode,
                "liquidate_target_usd": target_usd,
            }
        )
        api.get_balance.return_value = balance
        api.get_user_positions.return_value = positions
        api.get_open_orders.return_value = []
        db.get_market_daily_reward.side_effect = lambda cid: daily_rewards.get(cid)
        db.get_min_order_cost.return_value = 2.0
        monitor._cost_lots = lambda a, s, c: (
            costs.get(a),
            [{"price": costs.get(a) or 0, "take": s, "ts": 0, "trade_id": "t"}],
        )
        monitor._sell_book = lambda a: (
            float(tick),
            tick,
            bids.get(a),
            (bids.get(a) or 0) + 0.02,
        )
        return monitor, api, db

    def _pos(self, asset, size, cid, cur=0.1):
        return {"asset": asset, "size": size, "conditionId": cid, "curPrice": cur}

    def test_no_action_when_balance_ok(self):
        m, api, db = self._mon(
            10, [self._pos("A", 100, "cA")], {"cA": 10}, {"A": 0.1}, {"A": 0.1}
        )
        m.check_low_balance()
        api.place_market_sell.assert_not_called()

    def test_sells_low_reward_first_until_target(self):
        # 余额2<4;A 低奖励(档1)先卖,best_bid 0.05×100=5 到手 -> est 2+5=7≥4,只卖一笔。
        m, api, db = self._mon(
            2,
            [self._pos("A", 100, "cA"), self._pos("B", 100, "cB")],
            {"cA": 10, "cB": 50},
            {"A": 0.1, "B": 0.1},
            {"A": 0.05, "B": 0.05},
        )
        m.check_low_balance()
        api.place_market_sell.assert_called_once_with("A", 100, tick_size="0.01")

    def test_low_balance_dump_passes_real_tick_size(self):
        # 0.1¢ 市场的低余额清仓同样要传真实 tick,否则市价卖被拦、余额腾不出来。
        m, api, db = self._mon(
            2,
            [self._pos("A", 100, "cA")],
            {"cA": 10},
            {"A": 0.1},
            {"A": 0.05},
            tick="0.001",
        )
        m.check_low_balance()
        api.place_market_sell.assert_called_once()
        _, k = api.place_market_sell.call_args
        assert k.get("tick_size") == "0.001"

    def test_skips_no_bid(self):
        m, api, db = self._mon(
            2, [self._pos("A", 100, "cA")], {"cA": 10}, {"A": 0.1}, {"A": None}
        )
        m.check_low_balance()
        api.place_market_sell.assert_not_called()

    def test_disabled_when_threshold_zero(self):
        m, api, db = self._mon(
            1,
            [self._pos("A", 100, "cA")],
            {"cA": 10},
            {"A": 0.1},
            {"A": 0.05},
            threshold=0,
        )
        m.check_low_balance()
        api.get_balance.assert_not_called()  # 阈值0秒退,连余额都不查
        api.place_market_sell.assert_not_called()

    def test_skips_cost_unknown(self):
        # 成本没重建出(_cost_lots 返回 None):推迟,不盲目清仓
        m, api, db = self._mon(
            2, [self._pos("A", 100, "cA")], {"cA": 10}, {"A": None}, {"A": 0.05}
        )
        m.check_low_balance()
        api.place_market_sell.assert_not_called()

    def test_cooldown_after_dump_prevents_immediate_resweep(self):
        # 卖一笔后设冷却:冷却期内再调不重扫(防余额结算滞后时跨 tick 过卖)
        m, api, db = self._mon(
            2,
            [self._pos("A", 100, "cA"), self._pos("B", 100, "cB")],
            {"cA": 10, "cB": 10},
            {"A": 0.1, "B": 0.1},
            {"A": 0.05, "B": 0.05},
        )
        m.check_low_balance()  # 卖 A(0.05×100=5 -> est 7≥4 停)-> 设冷却
        n1 = api.place_market_sell.call_count
        assert n1 == 1
        m.check_low_balance()  # 冷却期内 -> 秒退
        assert api.place_market_sell.call_count == n1

    def test_early_return_when_balance_already_at_target(self):
        # target<threshold 时余额在 [target,threshold):触发但已达目标 -> 不拉持仓、不卖
        m, api, db = self._mon(
            3,
            [self._pos("A", 100, "cA")],
            {"cA": 10},
            {"A": 0.1},
            {"A": 0.05},
            threshold=4,
            target_usd=2,
        )
        m.check_low_balance()
        api.get_user_positions.assert_not_called()
        api.place_market_sell.assert_not_called()

    def test_dumped_recorded_for_check_exit_skip(self):
        m, api, db = self._mon(
            2, [self._pos("A", 100, "cA")], {"cA": 10}, {"A": 0.1}, {"A": 0.05}
        )
        m.check_low_balance()
        assert "A" in m._just_dumped  # 供 check_exit 同 tick 跳过

    def test_check_exit_skips_just_dumped(self):
        # check_exit 同 tick 跳过刚被低余额清仓的仓(否则对已卖仓挂卖被拒、报假裸奔)
        m, api, db = self._mon(
            2, [self._pos("A", 100, "cA")], {"cA": 10}, {"A": 0.3}, {"A": 0.05}
        )
        m._just_dumped = {"A"}
        m.check_exit()
        api.place_limit_sell.assert_not_called()


def _monitor_with_one_position(low_balance=0):
    """离场夹具:一个仓(tokA / cid1,100 份,成交重建成本 0.20)+ 盘口 0.30/0.31。

    low_balance>0 时同时打开低余额清仓(阈值与停手目标都取这个数,余额固定 1.0)。
    """
    monitor, api, db = _make_monitor(
        settings={
            "low_balance_threshold_usd": low_balance,
            "liquidate_target_usd": low_balance,
            "low_reward_threshold_usd": 30,
            "small_position_shares": 20,
        }
    )
    api.get_balance.return_value = 1.0
    api.get_user_positions.return_value = [
        {"asset": "tokA", "size": "100", "conditionId": "cid1", "curPrice": "0.30"}
    ]
    api.get_orderbook.return_value = {
        "bids": [{"price": "0.30", "size": "500"}],
        "asks": [{"price": "0.31", "size": "500"}],
        "tick_size": "0.01",
    }
    api.get_trades.return_value = [
        {
            "id": "t-tokA",
            "market": "cid1",
            "match_time": "1",
            "maker_orders": [
                {
                    "order_id": "o-tokA",
                    "maker_address": "0xFUNDER",
                    "side": "BUY",
                    "matched_amount": "100",
                    "price": "0.20",
                    "asset_id": "tokA",
                }
            ],
        }
    ]
    db.get_market_daily_reward.return_value = 50  # 不落进低奖励档,与本组用例无关
    monitor.begin_status_tick()
    return monitor, api, db


class TestDumpRejectedLeavesNoUnprotectedPosition:
    """低余额清仓被拒(撤单已成功、市价卖没成)-> 同一 tick 的 check_exit 必须把卖单挂回来。

    步骤 1-4 共用 tick 开头那份挂单快照,而清仓是在拍完快照之后才撤的卖单——快照里它还在。
    不把已撤的单滤掉,plan_take_profit 就判「目标价上已有一张卖单」-> keep,于是持仓既没被
    卖掉也没有卖单保护,状态行还谎报「保持…挂卖单0.3100」指着一张不存在的单。
    触发面不冷门:FAK(市价卖)打进薄奖励市场没吃到就被杀 = status unmatched = OrderRejected,
    而低余额清仓正是在没钱的钱包上干这件事;失败不设冷却,每 tick 复发。
    """

    SNAPSHOT_SELL = {
        "id": "sell1",
        "asset_id": "tokA",
        "side": "SELL",
        "price": "0.31",  # 正是 plan_exit 这轮要挂的价 -> 不滤掉必判 keep
        "original_size": "100",
        "size_matched": "0",
    }

    def test_exit_replaces_sell_after_failed_liquidation(self):
        monitor, api, db = _monitor_with_one_position(low_balance=4)
        api.place_market_sell.side_effect = RuntimeError("unmatched: FAK 未成交即被杀")
        snapshot = [self.SNAPSHOT_SELL]

        monitor.check_low_balance(snapshot)
        # 清仓失败:卖单已撤、仓没卖掉,也没进 _just_dumped
        api.cancel_orders.assert_called_once_with(["sell1"])
        assert "tokA" not in monitor._just_dumped

        monitor.check_exit(snapshot)
        api.place_limit_sell.assert_called_once()
        args, kwargs = api.place_limit_sell.call_args
        assert args[0] == "tokA"
        assert abs(args[1] - 0.31) < 1e-9  # A 档挂卖一,且 ≥ 成本 0.20
        assert abs(args[2] - 100.0) < 1e-9
        # 撤过的单不能再撤一次(重复撤已撤单会刷 already-canceled)
        assert api.cancel_orders.call_count == 1

    def test_status_row_does_not_claim_keep_when_sell_was_cancelled(self):
        monitor, api, db = _monitor_with_one_position(low_balance=4)
        api.place_market_sell.side_effect = RuntimeError("unmatched: FAK 未成交即被杀")
        snapshot = [self.SNAPSHOT_SELL]

        monitor.check_low_balance(snapshot)
        monitor.check_exit(snapshot)
        actions = [str(r.get("action", "")) for r in monitor._status_rows]
        assert not any("保持" in a for a in actions), actions
        assert any("挂卖单" in a for a in actions), actions


class TestExitPrefetchFailureDegradesInsteadOfKillingTheTick:
    """_exit_prefetch 抛出去 = 整轮离场判定不执行(持仓全无保护、无 ⚠️ 状态行、什么都不发布)。

    parallel_map 可能在任何一个任务开跑之前就抛 —— 最现实的是线程池建不出线程
    (N 个钱包 × 6 路 + 采集的 4 路同进程)。降级是免费的:预取表为空 = 逐仓回退自取,
    也就是并发化之前的串行行为。
    """

    def test_check_exit_completes_when_prefetch_raises(self):
        monitor, api, db = _monitor_with_one_position()
        monitor._exit_prefetch = MagicMock(
            side_effect=RuntimeError("can't start new thread")
        )
        monitor.check_exit([])
        api.place_limit_sell.assert_called_once()  # 回退自取,该挂的卖单照挂
        assert monitor._status_rows  # 状态行照出,监控表不冻

    def test_check_low_balance_completes_when_prefetch_raises(self):
        monitor, api, db = _monitor_with_one_position(low_balance=4)
        monitor._exit_prefetch = MagicMock(
            side_effect=RuntimeError("can't start new thread")
        )
        monitor.check_low_balance([])
        api.place_market_sell.assert_called_once_with("tokA", 100.0, tick_size="0.01")


def test_last_position_count_is_unknown_when_exit_returns_early():
    # tick 耗时日志的持仓数不能骗人:离场早退(取持仓失败)时置 None(日志渲染成 ?),
    # 绝不把上一轮的持仓数当成这一轮报出来。
    monitor, api, db = _make_monitor()
    monitor.last_position_count = 7  # 上一轮的数
    api.get_user_positions.side_effect = RuntimeError("data api down")
    monitor.check_exit()
    assert monitor.last_position_count is None


class TestMarketRewards:
    """一次取数拿到 rewards_max_spread 与每日奖励;TTL=0 时每次都重新联网。"""

    def _payload(self, max_spread=3, rate=120):
        return [
            {
                "rewards_max_spread": max_spread,
                "rewards_config": [{"rate_per_day": rate}],
            }
        ]

    def test_returns_max_spread_and_daily_rate(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = self._payload()
        assert monitor._market_rewards("cid1", 0) == (3.0, 120.0)

    def test_empty_condition_id_returns_none_pair(self):
        monitor, api, db = _make_monitor()
        assert monitor._market_rewards("", 0) == (None, None)
        api.get_rewards_for_market.assert_not_called()

    def test_api_failure_returns_none_pair(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.side_effect = RuntimeError("network")
        assert monitor._market_rewards("cid1", 0) == (None, None)

    def test_missing_rewards_config_gives_none_rate(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 4}]
        assert monitor._market_rewards("cid1", 0) == (4.0, None)

    def test_cached_within_ttl_fetches_once(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = self._payload()
        monitor._market_rewards("cid1", 600)
        monitor._market_rewards("cid1", 600)
        assert api.get_rewards_for_market.call_count == 1

    def test_ttl_zero_refetches_every_call(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = self._payload()
        monitor._market_rewards("cid1", 0)
        monitor._market_rewards("cid1", 0)
        assert api.get_rewards_for_market.call_count == 2

    def test_nothing_parsable_is_not_cached(self):
        # 一无所获不写缓存,下轮重试(沿用旧 _market_max_spread 的语义)
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [{"condition_id": "cid1"}]
        monitor._market_rewards("cid1", 600)
        monitor._market_rewards("cid1", 600)
        assert api.get_rewards_for_market.call_count == 2

    def test_rate_present_but_max_spread_missing_not_cached(self):
        # max_spread 取不到就不写缓存(与旧 _market_max_spread 一致);rate 有值也不例外。
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [
            {"rewards_config": [{"rate_per_day": 120}]}
        ]
        monitor._market_rewards("cid1", 600)
        monitor._market_rewards("cid1", 600)
        assert api.get_rewards_for_market.call_count == 2

    def test_does_not_touch_db(self):
        # worker 线程会调它,而每线程一条 sqlite 连接且建了不回收 —— 碰 db 就是
        # 每 5 秒一轮地泄漏连接。
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = self._payload()
        db.reset_mock()
        monitor._market_rewards("cid1", 0)
        assert db.method_calls == []


class TestStep3RewardDrop:
    """在挂单市场的每日奖励跌破 min_reward_usd → 立即撤买单不重挂 + 写回候选池。"""

    SETTINGS = {"min_reward_usd": 100.0}

    def _ob(self, best_bid="0.30", best_ask="0.31", tick="0.01"):
        return {
            "bids": [{"price": best_bid, "size": "1000"}],
            "asks": [{"price": best_ask, "size": "1000"}],
            "tick_size": tick,
        }

    def _order(self):
        return {
            "id": "o1",
            "side": "BUY",
            "asset_id": "tok1",
            "market": "cid1",
            "size_matched": "0",
            "price": "0.30",
            "original_size": "500",
        }

    def _rewards(self, rate, max_spread=3):
        return [
            {
                "rewards_max_spread": max_spread,
                "rewards_config": [{"rate_per_day": rate}],
            }
        ]

    def _run(self, rate, settings=None, cb=None):
        monitor, api, db = _make_monitor(settings or self.SETTINGS)
        if cb is not None:
            monitor.on_reward_update = cb
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        if isinstance(rate, Exception):
            api.get_rewards_for_market.side_effect = rate
        else:
            api.get_rewards_for_market.return_value = rate
        monitor.check_sell_orders()
        return monitor, api, db

    def test_cancels_when_reward_below_threshold(self):
        monitor, api, db = self._run(self._rewards(5))
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["reward_drop_cancel"]
        kw = db.record_action.call_args_list[0].kwargs
        assert kw["side"] == "-"
        assert kw["price"] == -1
        assert kw["size"] == 500
        assert "5.00" in kw["reason"] and "100.00" in kw["reason"]

    def test_cancels_when_reward_is_zero(self):
        # 0 是「奖励真归零」,必须撤单——不能被当成「取不到」跳过。
        monitor, api, db = self._run(self._rewards(0))
        api.cancel_orders.assert_called_once_with(["o1"])

    def test_keeps_when_reward_equals_threshold(self):
        # 门槛是「低于才撤」,与扫描阶段 prefilter 的 < 口径一致。
        monitor, api, db = self._run(self._rewards(100))
        api.cancel_orders.assert_not_called()

    def test_keeps_when_reward_above_threshold(self):
        monitor, api, db = self._run(self._rewards(300))
        api.cancel_orders.assert_not_called()
        db.record_action.assert_not_called()

    def test_fetch_failure_does_not_cancel(self):
        # 接口抖一下就撤光正在赚奖励的单是最坏结果:取不到一律跳过。
        monitor, api, db = self._run(RuntimeError("network"))
        api.cancel_orders.assert_not_called()
        db.record_action.assert_not_called()

    def test_missing_rewards_config_does_not_cancel(self):
        monitor, api, db = self._run([{"rewards_max_spread": 3}])
        api.cancel_orders.assert_not_called()

    def test_writes_back_realtime_reward(self):
        cb = MagicMock()
        monitor, api, db = self._run(self._rewards(5), cb=cb)
        cb.assert_called_once_with("cid1", 5.0)

    def test_cancel_failure_skips_writeback_and_action(self):
        cb = MagicMock()
        monitor, api, db = _make_monitor(self.SETTINGS)
        monitor.on_reward_update = cb
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = self._rewards(5)
        api.cancel_orders.side_effect = RuntimeError("network")
        monitor.check_sell_orders()  # must not raise
        cb.assert_not_called()
        db.record_action.assert_not_called()

    def test_writeback_failure_does_not_break_cancel(self):
        cb = MagicMock(side_effect=RuntimeError("db down"))
        monitor, api, db = self._run(self._rewards(5), cb=cb)
        api.cancel_orders.assert_called_once_with(["o1"])
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["reward_drop_cancel"]

    def test_status_row_published(self):
        monitor, api, db = _make_monitor(self.SETTINGS)
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = self._rewards(5)
        monitor.check_sell_orders()
        rows = [r for r in monitor._status_rows if r.get("stage") == "Step3"]
        assert rows and rows[0]["action"] == "撤单(奖励下降)"

    # --- 闸门位置:奖励判定不看盘口、也不依赖 max_spread ---

    def test_cancels_when_max_spread_missing(self):
        # 奖励闸门必须排在「取不到 max_spread 就跳过」之前:两个值来自同一次响应,
        # 响应里缺 rewards_max_spread 时,奖励判定不该被一起跳过。
        monitor, api, db = self._run([{"rewards_config": [{"rate_per_day": 5}]}])
        api.cancel_orders.assert_called_once_with(["o1"])
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["reward_drop_cancel"]

    def test_cancels_when_orderbook_empty(self):
        # 奖励闸门必须排在「盘口为空就跳过」之前:薄奖励市场常缺卖单一侧,那些在挂
        # 买单照样会被别人的市价卖单吃掉;闸门若在守卫之后,这类市场奖励归零时买单
        # 一直挂着、还不写回候选池,下单轮继续拿旧快照重挂 —— 4 小时窗口原样敞开。
        cb = MagicMock()
        monitor, api, db = _make_monitor(self.SETTINGS)
        monitor.on_reward_update = cb
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        api.get_rewards_for_market.return_value = self._rewards(5)
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["reward_drop_cancel"]
        cb.assert_called_once_with("cid1", 5.0)  # 写回照做,下单轮不会重挂

    def test_cancels_when_only_asks_missing(self):
        # 只缺卖单一侧(买单侧健在)——最贴近实盘的薄市场形态,同样要撤。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = {
            "bids": [{"price": "0.30", "size": "1000"}],
            "asks": [],
            "tick_size": "0.01",
        }
        api.get_rewards_for_market.return_value = self._rewards(5)
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])

    def test_empty_book_still_skips_when_reward_ok(self):
        # 闸门前移不能让空盘口的原有语义丢掉:奖励达标时仍是「本轮跳过」,不撤。
        monitor, api, db = _make_monitor(self.SETTINGS)
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        api.get_rewards_for_market.return_value = self._rewards(300)
        monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()
        rows = [r for r in monitor._status_rows if r.get("stage") == "Step3"]
        assert rows and rows[0]["action"] == "跳过(盘口为空)"

    # --- 本轮取数备忘:同一市场一轮只取一次 ---

    def test_both_sides_of_market_fetch_rewards_once(self):
        # 同一 condition_id 的 YES/NO 两侧各有一笔在挂买单时,一轮只发一次
        # /rewards/markets 请求(TTL=0 挡不住同轮重复)。
        monitor, api, db = _make_monitor({**self.SETTINGS, "rewards_cache_ttl_sec": 0})
        yes = self._order()
        no = {**self._order(), "id": "o2", "asset_id": "tok2"}
        api.get_open_orders.return_value = [yes, no]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = self._rewards(300)
        monitor.check_sell_orders()
        assert api.get_rewards_for_market.call_count == 1

    def test_memo_is_reset_between_rounds(self):
        # 备忘只活一轮:下一轮必须重新联网,否则奖励变化再也看不到。
        monitor, api, db = _make_monitor({**self.SETTINGS, "rewards_cache_ttl_sec": 0})
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = self._rewards(300)
        monitor.check_sell_orders()
        monitor.check_sell_orders()
        assert api.get_rewards_for_market.call_count == 2

    def test_memo_caches_failure_so_both_sides_agree(self):
        # 取数失败也进备忘:同一轮里两侧得到同一个判定(都跳过),不会一侧撤一侧不撤。
        monitor, api, db = _make_monitor({**self.SETTINGS, "rewards_cache_ttl_sec": 0})
        no = {**self._order(), "id": "o2", "asset_id": "tok2"}
        api.get_open_orders.return_value = [self._order(), no]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.side_effect = RuntimeError("network")
        monitor.check_sell_orders()
        assert api.get_rewards_for_market.call_count == 1
        api.cancel_orders.assert_not_called()


class TestExitPrefetch:
    """离场判定的成交/盘口并发预取:按市场/asset 去重、逐项容错、不重复取。"""

    def _pos(self, asset, cid, size="100"):
        return {"asset": asset, "conditionId": cid, "size": size, "curPrice": "0.30"}

    def _ob(self):
        return {
            "bids": [{"price": "0.30", "size": "1000"}],
            "asks": [{"price": "0.31", "size": "1000"}],
            "tick_size": "0.01",
        }

    def test_dedups_trades_by_condition_id(self):
        # 同市场 YES/NO 两个仓:成交只取一次,盘口按 asset 取两次
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = [{"id": "t1"}]
        monitor._exit_prefetch(
            [self._pos("tokYes", "cid1"), self._pos("tokNo", "cid1")]
        )
        assert api.get_trades.call_count == 1
        assert api.get_orderbook.call_count == 2
        assert monitor._trades_by_cid == {"cid1": [{"id": "t1"}]}
        assert set(monitor._book_cache) == {"tokYes", "tokNo"}

    def test_takes_all_books_in_one_batch(self):
        """N 个持仓的盘口一次批量拿完,不再逐仓单发(与 Step3 同因)。"""
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_trades.return_value = []
        api.get_orderbooks.side_effect = None  # 覆盖 _make_monitor 的单发转发
        api.get_orderbooks.return_value = {"tokA": self._ob(), "tokB": self._ob()}

        monitor._exit_prefetch([self._pos("tokA", "cid1"), self._pos("tokB", "cid2")])

        assert api.get_orderbook.call_count == 0, "不该再逐仓单发盘口"
        assert api.get_orderbooks.call_count == 1
        assert set(api.get_orderbooks.call_args[0][0]) == {"tokA", "tokB"}

    def test_trades_failure_recorded_as_none(self):
        # 取数失败记 None,与「取到了但没有成交」([])区分开
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        api.get_trades.side_effect = RuntimeError("network")
        monitor._exit_prefetch([self._pos("tokA", "cid1")])
        assert monitor._trades_by_cid == {"cid1": None}

    def test_empty_trades_list_is_not_none(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor._exit_prefetch([self._pos("tokA", "cid1")])
        assert monitor._trades_by_cid == {"cid1": []}

    def test_book_failure_recorded_as_none(self):
        # 批量取到了、但这个 token 没在返回里(实测 /books 静默丢弃取不到的 token)
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_trades.return_value = []
        api.get_orderbooks.side_effect = lambda ids: {t: None for t in ids}
        monitor._exit_prefetch([self._pos("tokA", "cid1")])
        assert monitor._book_cache == {"tokA": None}

    def test_whole_batch_crash_leaves_table_empty_for_self_fetch(self):
        """整个批量调用崩掉时**什么都不写**,让 _sell_book 逐仓自取。

        写 None 会关掉 _sell_book 的自取退路(None = 预取失败、不自取),把一次抖动
        放大成整轮拿不到盘口。留空(_MISSING)才退回并发化之前的串行行为。
        """
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_trades.return_value = []
        api.get_orderbooks.side_effect = RuntimeError("boom")

        monitor._exit_prefetch([self._pos("tokA", "cid1")])

        assert monitor._book_cache == {}, "留空才有自取退路,不能写成 None"

    def test_one_failure_does_not_affect_others(self):
        # 同 Step3:批量对取不到的 token 静默丢弃,该仓记 None、同批其余照常
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_trades.return_value = []
        api.get_orderbooks.side_effect = lambda ids: {
            t: (None if t == "tokBad" else self._ob()) for t in ids
        }
        monitor._exit_prefetch(
            [self._pos("tokBad", "cid1"), self._pos("tokOk", "cid2")]
        )
        assert monitor._book_cache["tokBad"] is None
        assert monitor._book_cache["tokOk"] == self._ob()

    def test_second_call_only_fetches_the_difference(self):
        # check_low_balance 先预取过一轮时,check_exit 这轮只补差集
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor._exit_prefetch([self._pos("tokA", "cid1")])
        monitor._exit_prefetch([self._pos("tokA", "cid1"), self._pos("tokB", "cid2")])
        assert api.get_trades.call_count == 2  # cid1 一次 + cid2 一次
        assert api.get_orderbook.call_count == 2  # tokA 一次 + tokB 一次

    def test_begin_status_tick_clears_both_caches(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor._exit_prefetch([self._pos("tokA", "cid1")])
        monitor.begin_status_tick()
        assert monitor._trades_by_cid == {}
        assert monitor._book_cache == {}

    def test_trades_wave_completes_before_books_wave(self):
        # 顺序不可调:盘口驱动挂价与 B0 判定,必须是更新鲜的那份
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        seen = []
        api.get_trades.side_effect = lambda *a, **k: seen.append("trades") or []
        api.get_orderbook.side_effect = (
            lambda *a, **k: seen.append("book") or self._ob()
        )
        monitor._exit_prefetch([self._pos("tokA", "cid1"), self._pos("tokB", "cid2")])
        assert seen.count("trades") == 2 and seen.count("book") == 2
        assert seen.index("book") > max(i for i, s in enumerate(seen) if s == "trades")

    def test_skips_blank_ids(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._exit_prefetch([{"asset": "", "conditionId": "", "size": "10"}])
        assert api.get_trades.call_count == 0
        assert api.get_orderbook.call_count == 0

    def test_cost_lots_uses_prefetched_trades(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._trades_by_cid["cid1"] = []
        monitor._cost_lots("tokA", 100.0, "cid1")
        assert api.get_trades.call_count == 0

    def test_cost_lots_falls_back_when_not_prefetched(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_trades.return_value = []
        monitor._cost_lots("tokA", 100.0, "cid1")
        assert api.get_trades.call_count == 1

    def test_cost_lots_prefetch_failure_retries_self_fetch(self):
        # 预取值 None(没取到)与空列表(确实没成交)必须区别对待:空列表不自取(上一个
        # 用例),None 自取重试一次。这里原先断言的是「不重试、直接判成本未知」,实盘代价
        # 是 2026-07-29 那天 810 次 ⚠️裸奔里有 789 次(97.4%)其实只是一次网络抖动。
        # 自取也失败时仍然成本未知——不确定的成本绝不拿去卖,那条不变量没变。
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._trades_by_cid["cid1"] = None
        api.get_trades.side_effect = Exception("boom")
        assert monitor._cost_lots("tokA", 100.0, "cid1") == (None, [])
        assert api.get_trades.call_count == 1

    def test_sell_book_uses_prefetched_orderbook(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._book_cache["tokA"] = self._ob()
        tick, tick_str, bid, ask = monitor._sell_book("tokA")
        assert api.get_orderbook.call_count == 0
        assert (tick, tick_str, bid, ask) == (0.01, "0.01", 0.30, 0.31)

    def test_sell_book_falls_back_when_not_prefetched(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        assert monitor._sell_book("tokA")[2] == 0.30
        assert api.get_orderbook.call_count == 1

    def test_sell_book_prefetch_failure_degrades_like_fetch_failure(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._book_cache["tokA"] = None
        assert monitor._sell_book("tokA") == (0.01, "0.01", None, None)
        assert api.get_orderbook.call_count == 0

    def test_check_exit_prefetches_before_looping(self):
        # 3 个仓分属 2 个市场:成交按市场取 2 次,盘口按 asset 取 3 次
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_user_positions.return_value = [
            self._pos("tokA", "cid1"),
            self._pos("tokB", "cid1"),
            self._pos("tokC", "cid2"),
        ]
        api.get_open_orders.return_value = []
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor.check_exit()
        assert api.get_trades.call_count == 2
        assert api.get_orderbook.call_count == 3
        assert monitor.last_position_count == 3

    def test_check_exit_does_not_prefetch_just_dumped(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._just_dumped.add("tokA")
        api.get_user_positions.return_value = [
            self._pos("tokA", "cid1"),
            self._pos("tokB", "cid2"),
        ]
        api.get_open_orders.return_value = []
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor.check_exit()
        assert api.get_orderbook.call_count == 1
        assert "tokA" not in monitor._book_cache

    def test_low_balance_prefetches_and_check_exit_reuses(self):
        # 低余额先预取过,check_exit 不再重复取同一批。用一笔与持仓 size 匹配的真实成交
        # 让成本重建得出来(0.20),循环才会真的走到 _sell_book——否则(get_trades=[])
        # 两边都在 cost is None 处提前 return,断言测不到"缓存复用"这件事本身
        # (评审 2026-07-28 指出的 vacuous test)。买盘留空:让 check_low_balance 的
        # 清仓判定因无买盘跳过(不卖出、不进 _just_dumped),这样 check_exit 才会正常
        # 处理这个仓、真正调用 _sell_book 读缓存,而不是因为"已被清掉"而整个跳过。
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        db.get_template_for.return_value = {
            "low_balance_threshold_usd": 4,
            "liquidate_target_usd": 4,
            "cooldown_minutes": 20,
        }
        db.get_market_daily_reward.return_value = 50  # 不落进低奖励档,与本用例无关
        api.get_balance.return_value = 1.0
        api.get_user_positions.return_value = [self._pos("tokA", "cid1")]
        api.get_open_orders.return_value = []
        api.get_orderbook.return_value = {
            "bids": [],  # 无买盘 -> 低余额清仓判定跳过,不会把这个仓卖掉
            "asks": [{"price": "0.31", "size": "1000"}],
            "tick_size": "0.01",
        }
        api.get_trades.return_value = [
            {
                "id": "t-tokA",
                "market": "cid1",
                "match_time": "1",
                "maker_orders": [
                    {
                        "order_id": "o-tokA",
                        "maker_address": "0xFUNDER",
                        "side": "BUY",
                        "matched_amount": "100",
                        "price": "0.20",
                        "asset_id": "tokA",
                    }
                ],
            }
        ]
        monitor.check_low_balance()
        assert "tokA" not in monitor._just_dumped  # 无买盘没被清掉,仍走正常离场
        before = api.get_orderbook.call_count
        monitor.check_exit()
        assert api.get_orderbook.call_count == before  # 复用,没再取


class TestStep3Prefetch:
    """Step3 本轮盘口/奖励的并发预取:按 token/市场去重、逐项容错、带钱包代理。"""

    def _buy(self, oid, token, cid, matched="0"):
        return {
            "id": oid,
            "side": "BUY",
            "asset_id": token,
            "market": cid,
            "size_matched": matched,
            "price": "0.30",
            "original_size": "500",
        }

    def _ob(self):
        return {
            "bids": [{"price": "0.30", "size": "1000"}],
            "asks": [{"price": "0.31", "size": "1000"}],
            "tick_size": "0.01",
        }

    def _ready(self, monitor, api):
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 120}]}
        ]

    def test_dedups_orderbook_by_token(self):
        # 同一 token 上两笔买单只取一次盘口
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        orders = [self._buy("o1", "tokA", "cid1"), self._buy("o2", "tokA", "cid1")]
        books, rewards = monitor._prefetch(orders, set(), 0)
        assert api.get_orderbook.call_count == 1
        assert set(books) == {"tokA"}
        assert books["tokA"] == self._ob()

    def test_dedups_rewards_by_market(self):
        # 同市场 YES/NO 两侧:盘口按 token 取两次,奖励按市场只取一次
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        orders = [self._buy("o1", "tokYes", "cid1"), self._buy("o2", "tokNo", "cid1")]
        books, rewards = monitor._prefetch(orders, set(), 0)
        assert api.get_orderbook.call_count == 2
        assert api.get_rewards_for_market.call_count == 1
        assert rewards == {"cid1": (3.0, 120.0)}

    def test_orderbook_failure_becomes_none(self):
        # 取数失败记 None(调用方据此跳过该单),绝不外抛、绝不拖垮其它单
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        api.get_orderbook.side_effect = RuntimeError("network")
        books, rewards = monitor._prefetch([self._buy("o1", "tokA", "cid1")], set(), 0)
        assert books == {"tokA": None}

    def test_one_failure_does_not_affect_others(self):
        # 批量取数下这仍然成立:实测 /books 对取不到的 token **静默丢弃**(HTTP 200,
        # 返回条数少于请求条数),该 token 记 None、同批其余照常返回。
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        api.get_orderbooks.side_effect = lambda ids: {
            t: (None if t == "tokBad" else self._ob()) for t in ids
        }
        orders = [self._buy("o1", "tokBad", "cid1"), self._buy("o2", "tokOk", "cid2")]
        books, rewards = monitor._prefetch(orders, set(), 0)
        assert books["tokBad"] is None
        assert books["tokOk"] == self._ob()

    def test_rewards_failure_becomes_none_pair(self):
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        api.get_rewards_for_market.side_effect = RuntimeError("network")
        books, rewards = monitor._prefetch([self._buy("o1", "tokA", "cid1")], set(), 0)
        assert rewards == {"cid1": (None, None)}

    def test_rewards_parse_error_becomes_none_pair(self):
        # rewards_config 字段形状不对(真值但不可迭代)时解析会抛 TypeError,这段异常
        # 必须也被吞掉,不能从 worker 里外抛拖垮整轮 Step3。
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": 7}
        ]
        books, rewards = monitor._prefetch([self._buy("o1", "tokA", "cid1")], set(), 0)
        assert rewards == {"cid1": (None, None)}

    def test_rewards_parse_error_does_not_affect_other_markets(self):
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)

        def _rewards_or_boom(condition_id):
            if condition_id == "cidBad":
                return [{"rewards_max_spread": 3, "rewards_config": 7}]
            return [
                {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 120}]}
            ]

        api.get_rewards_for_market.side_effect = _rewards_or_boom
        orders = [
            self._buy("o1", "tokBad", "cidBad"),
            self._buy("o2", "tokOk", "cidOk"),
        ]
        books, rewards = monitor._prefetch(orders, set(), 0)
        assert rewards["cidBad"] == (None, None)
        assert rewards["cidOk"] == (3.0, 120.0)

    def test_skips_blacklisted_market(self):
        # 黑名单单在判定里于取数之前就 return,不必为它发请求
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        books, rewards = monitor._prefetch(
            [self._buy("o1", "tokA", "cid1")], {"cid1"}, 0
        )
        api.get_orderbook.assert_not_called()
        api.get_rewards_for_market.assert_not_called()
        assert (books, rewards) == ({}, {})

    def test_skips_sell_orders_and_partially_filled(self):
        # 卖单和部分成交单不进判定,不为它们取数
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        orders = [
            {
                "id": "s1",
                "side": "SELL",
                "asset_id": "tokS",
                "market": "cidS",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            },
            self._buy("o2", "tokP", "cidP", matched="100"),
        ]
        books, rewards = monitor._prefetch(orders, set(), 0)
        api.get_orderbook.assert_not_called()
        assert (books, rewards) == ({}, {})

    def test_no_orders_makes_no_requests(self):
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        books, rewards = monitor._prefetch([], set(), 0)
        assert (books, rewards) == ({}, {})
        api.get_orderbook.assert_not_called()
        api.get_rewards_for_market.assert_not_called()

    def test_carries_wallet_proxy_into_workers(self):
        # worker 线程默认 current_proxy=None 会直连、泄露真实 IP;预取必须把调用线程
        # 的代理带进去,否则并发化就破了 IP 隔离铁律。
        from api.proxy import current_proxy, use_proxy

        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        seen = []

        def _ob_recording(token_id):
            seen.append(current_proxy.get())
            return self._ob()

        api.get_orderbook.side_effect = _ob_recording
        with use_proxy("http://user:pass@host:9999"):
            monitor._prefetch([self._buy("o1", "tokA", "cid1")], set(), 0)
        assert seen == ["http://user:pass@host:9999"]

    def test_does_not_touch_db(self):
        # 硬约束:worker 碰 db 会让每 5 秒一轮的 Step3 无限泄漏 sqlite 连接
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        db.reset_mock()
        monitor._prefetch([self._buy("o1", "tokA", "cid1")], set(), 0)
        assert db.method_calls == []

    def test_rewards_wave_runs_before_orderbook_wave(self):
        # 两拨串行,先取的那拨要背上后取那拨的全部耗时。盘口驱动撤单判定,必须最新鲜;
        # 每日奖励是小时级才变的量。所以奖励在前、盘口在后,别调回来。
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        order = []

        def _rewards_recording(condition_id):
            order.append("奖励")
            return [
                {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 120}]}
            ]

        def _ob_recording(token_id):
            order.append("盘口")
            return self._ob()

        api.get_rewards_for_market.side_effect = _rewards_recording
        api.get_orderbook.side_effect = _ob_recording
        monitor._prefetch(
            [self._buy("o1", "tokA", "cidA"), self._buy("o2", "tokB", "cidB")], set(), 0
        )
        assert order.count("奖励") == 2 and order.count("盘口") == 2
        # 全部奖励调用都排在全部盘口调用之前(拨内顺序不定,拨间必须有序)
        assert order == ["奖励", "奖励", "盘口", "盘口"]

    def test_logs_round_summary(self, caplog):
        # 并发化的收益(耗时)和最大风险(6 路把娇气的奖励端点打崩)只有这条汇总看得见:
        # 逐项 WARNING 看不出面,奖励系统性取不到还会静默退化成 fail-open 跳过。
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)

        def _rewards_or_boom(condition_id):
            if condition_id == "cidBad":
                raise RuntimeError("network")
            return [
                {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 120}]}
            ]

        api.get_orderbooks.side_effect = lambda ids: {
            t: (None if t == "tokBad" else self._ob()) for t in ids
        }
        api.get_rewards_for_market.side_effect = _rewards_or_boom
        orders = [
            self._buy("o1", "tokBad", "cidBad"),
            self._buy("o2", "tokOk", "cidOk"),
        ]
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor._prefetch(orders, set(), 0)
        lines = [r.getMessage() for r in caplog.records if "预取完成" in r.getMessage()]
        assert len(lines) == 1
        assert "盘口 1/2" in lines[0] and "奖励 1/2" in lines[0]
        assert "耗时" in lines[0]

    def test_no_work_round_logs_nothing(self, caplog):
        # 没东西要取的轮一声不响:5 个钱包空闲过夜每分钟 60 条 0/0,这条汇总本身就废了。
        # 门槛是「本轮有没有要取的东西」,不是「两张表是否都空」——见下一个测试。
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        orders = [
            {
                "id": "s1",
                "side": "SELL",
                "asset_id": "tokS",
                "market": "cidS",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            },
            self._buy("o2", "tokP", "cidP", matched="100"),
            self._buy("o3", "tokB", "cidBlack"),
        ]
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor._prefetch(orders, {"cidBlack"}, 0)
        assert "预取完成" not in caplog.text

    def test_all_fetches_failing_still_logs(self, caplog):
        # 问了却一个都没取回来 —— 正是最该看见的那种轮,不能被静音门槛顺手吞掉
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        api.get_orderbook.side_effect = RuntimeError("network")
        api.get_rewards_for_market.side_effect = RuntimeError("network")
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor._prefetch([self._buy("o1", "tokA", "cidA")], set(), 0)
        lines = [r.getMessage() for r in caplog.records if "预取完成" in r.getMessage()]
        assert len(lines) == 1
        assert "盘口 0/1" in lines[0] and "奖励 0/1" in lines[0]


class TestStep3PrefetchWiring:
    """接线后的语义等价:取数失败 vs 盘口为空是两回事;DB 读收到主线程一次。"""

    def _buy(self, oid="o1", token="tokA", cid="cid1"):
        return {
            "id": oid,
            "side": "BUY",
            "asset_id": token,
            "market": cid,
            "size_matched": "0",
            "price": "0.30",
            "original_size": "500",
        }

    def _rewards(self):
        return [{"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 300}]}]

    def test_orderbook_failure_skips_order_without_status_row(self):
        # 等价于旧版 get_orderbook 抛异常被外层吞掉:跳过该单、不留状态行、不撤单
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [self._buy()]
        api.get_orderbook.side_effect = RuntimeError("network")
        api.get_rewards_for_market.return_value = self._rewards()
        monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()
        assert [r for r in monitor._status_rows if r.get("stage") == "Step3"] == []

    def test_empty_book_still_records_status_row(self):
        # 「取到了但买卖盘为空」是另一回事:走原分支并留状态行
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [self._buy()]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        api.get_rewards_for_market.return_value = self._rewards()
        monitor.check_sell_orders()
        rows = [r for r in monitor._status_rows if r.get("stage") == "Step3"]
        assert rows and rows[0]["action"] == "跳过(盘口为空)"

    def test_failed_order_does_not_block_the_next_one(self):
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [
            self._buy("o1", "tokBad", "cid1"),
            self._buy("o2", "tokOk", "cid2"),
        ]

        api.get_orderbooks.side_effect = lambda ids: {
            t: (
                None
                if t == "tokBad"
                else {
                    "bids": [{"price": "0.30", "size": "1000"}],
                    "asks": [{"price": "0.31", "size": "1000"}],
                    "tick_size": "0.01",
                }
            )
            for t in ids
        }
        api.get_rewards_for_market.return_value = self._rewards()
        monitor.check_sell_orders()
        rows = [r for r in monitor._status_rows if r.get("stage") == "Step3"]
        assert len(rows) == 1 and rows[0]["market"] == "cid2"

    def test_db_reads_are_once_per_round_not_per_order(self):
        # 黑名单和模板原来是每单各读一遍;60 个挂单就是 60 次查询
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        api.get_open_orders.return_value = [
            self._buy("o1", "tokA", "cid1"),
            self._buy("o2", "tokB", "cid2"),
            self._buy("o3", "tokC", "cid3"),
        ]
        api.get_orderbook.return_value = {
            "bids": [{"price": "0.30", "size": "1000"}],
            "asks": [{"price": "0.31", "size": "1000"}],
            "tick_size": "0.01",
        }
        api.get_rewards_for_market.return_value = self._rewards()
        db.reset_mock()
        monitor.check_sell_orders()
        assert db.get_blacklist_ids.call_count == 1
        assert db.get_template_for.call_count == 1
        assert db.get_settings.call_count == 1

    def test_db_read_failure_does_not_escape(self, caplog):
        # 轮级 DB 读失败(锁超时/磁盘满)必须只让 Step3 本轮降级:异常冒出
        # check_sell_orders 会跳过 publish_status 把监控状态表冻住(manager._run 的 F4)。
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [self._buy()]
        db.get_blacklist_ids.side_effect = RuntimeError("database is locked")
        with caplog.at_level(logging.ERROR, logger="engine.monitor"):
            monitor.check_sell_orders()  # 不抛
        api.cancel_orders.assert_not_called()
        assert "本轮取数准备失败" in caplog.text

    def test_prefetch_failure_does_not_escape(self, caplog):
        # 预取本身抛(如交易所回了非数字 size_matched)同理:降级不冻结
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [self._buy()]
        with patch.object(
            monitor, "_prefetch", side_effect=ValueError("could not convert string")
        ):
            with caplog.at_level(logging.ERROR, logger="engine.monitor"):
                monitor.check_sell_orders()  # 不抛
        api.cancel_orders.assert_not_called()
        assert "本轮取数准备失败" in caplog.text

    def test_still_iterates_all_orders_for_status_rows(self):
        # 遍历必须是全量 open_orders:卖单和部分成交单的状态行不能因为预取只挑买单
        # 而凭空消失。
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [
            {
                "id": "s1",
                "side": "SELL",
                "asset_id": "tokS",
                "market": "cidS",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            },
            {
                "id": "p1",
                "side": "BUY",
                "asset_id": "tokP",
                "market": "cidP",
                "size_matched": "100",
                "price": "0.30",
                "original_size": "500",
            },
        ]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        api.get_rewards_for_market.return_value = self._rewards()
        monitor.check_sell_orders()
        stages = [r.get("stage") for r in monitor._status_rows]
        assert "止盈卖单" in stages and "Step1" in stages
