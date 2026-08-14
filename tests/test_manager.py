"""tests/test_manager.py"""

import logging
import time
import pytest
from unittest.mock import MagicMock, patch
from engine.manager import EngineManager, WalletWorker


class TestRunLoopResilience:
    """F4: 监控主循环必须隔离单次 tick 故障——任一未捕获异常都不能杀死该钱包的
    监控线程(否则该钱包从此不再检测成交/不再止损,且不会自动重启)。"""

    def test_run_survives_tick_exception_and_continues(self):
        api, db = MagicMock(), MagicMock()
        worker = WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5})
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            worker._stop_event.set()  # 让循环这轮之后退出
            raise RuntimeError("tick blew up")

        worker._tick = boom
        worker._run()  # 必须正常返回,不能把异常抛出来杀死线程
        assert calls["n"] == 1


def _make_manager():
    db = MagicMock()
    db.get_settings.return_value = {
        "scan_interval_sec": 30,
        "fill_check_interval_sec": 5,
        "cooldown_minutes": 20,
        "discovery_interval_sec": 14400,
        "reward_scan_max_pages": 20,
        "min_reward_usd": 100.0,
        "max_spread_cents": 3.0,
        "min_price_cents": 10.0,
        "max_price_cents": 50.0,
        "min_settlement_days": 4,
    }
    db.list_wallets.return_value = [
        {"address": "0xABC", "encrypted_key": "enc1", "enabled": 1},
        {"address": "0xDEF", "encrypted_key": "enc2", "enabled": 1},
    ]
    db.get_open_buy_orders.return_value = []
    db.get_template_for.return_value = {
        "included_categories": ["politics"],
        "include_other": True,
        "min_reward_usd": 100.0,
        "max_buy_orders_per_wallet": 5,
        "order_size_mode": "min",
        "order_size_custom_usd": 0.0,
    }
    db.get_template.return_value = {
        "included_categories": ["politics"],
        "include_other": True,
        "min_reward_usd": 100.0,
    }
    db.get_default_template_id.return_value = 1
    manager = EngineManager(db, encryption_key=b"x" * 32)
    return manager, db


class TestEngineLifecycle:
    def test_start_creates_threads(self):
        manager, db = _make_manager()
        with patch("engine.manager.decrypt", return_value="0x_fake_key"):
            with patch("engine.manager.PolymarketAPI"):
                manager.start_all()
                assert len(manager.engines) == 2
                manager.stop_all()

    def test_stop_cancels_buy_orders(self):
        manager, db = _make_manager()
        mock_api = MagicMock()
        mock_api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY"},
            {"id": "o2", "side": "SELL"},
        ]
        with patch("engine.manager.decrypt", return_value="0x_fake_key"):
            with patch("engine.manager.PolymarketAPI", return_value=mock_api):
                manager.start_all()
                # Reset mocks after start_all so the assertion only counts cancels from stop()
                mock_api.cancel_orders.reset_mock()
                manager.stop_all()
                # Batched cancel: cancel_orders called once per wallet (2 wallets),
                # each call with only the BUY order ids
                assert mock_api.cancel_orders.call_count == 2
                assert all(
                    c.args == (["o1"],) for c in mock_api.cancel_orders.call_args_list
                )

    def test_get_status(self):
        manager, db = _make_manager()
        status = manager.get_status()
        assert isinstance(status, dict)
        assert "engines" in status

    def test_reset_discovery_cache_forces_rediscovery(self):
        # 清空内存候选池后,_should_discover 立刻为 True(即便刚扫过、离 4h 还远)。
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "stale"}]
        manager.last_scan_time = time.time()
        assert manager._should_discover(time.time()) is False  # 复用旧池:不发现
        manager._reset_discovery_cache()
        assert manager.eligible_markets == []
        assert manager._should_discover(time.time()) is True  # 清空后:强制发现

    def test_start_all_clears_stale_pool(self):
        # 「启动/重启引擎」须清空上一轮内存候选池,否则扫描线程因 _should_discover=False
        # 复用旧结果、市场发现一直不刷新(用户实报的「重启不刷新市场」)。
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "stale"}]
        manager.last_scan_time = time.time()
        with patch("engine.manager.decrypt", return_value="k"):
            with patch("engine.manager.PolymarketAPI"):
                with patch.object(manager, "_scanner_loop"):  # 不真跑循环,免它又填充池
                    manager.start_all()
                    assert manager.eligible_markets == []
                    manager.stop_all()


class TestWalletWorkerTick:
    def test_tick_runs_check_exit_between_fills_and_compliance(self):
        api, db = MagicMock(), MagicMock()
        db.get_template_for.return_value = {"fast_exit_enabled": False}
        worker = WalletWorker(
            api, db, "0xABC", {"fill_check_interval_sec": 5}
        )
        worker.monitor = MagicMock()

        worker._tick()

        worker.monitor.begin_status_tick.assert_called_once()
        worker.monitor.check_buy_orders.assert_called_once()
        worker.monitor.check_exit.assert_called_once()
        worker.monitor.check_sell_orders.assert_called_once()
        worker.monitor.publish_status.assert_called_once()


class TestTestPlaceOrders:
    class _FakeScanner:
        """filter_for_template 原样返回候选池(测试里候选已是可下单形状)。

        eligible_markets 现为候选池;test_place_orders 会先 filter_for_template
        再 place_orders。这些测试不验证精筛逻辑本身(那在 test_scanner.py),
        故用假精筛原样透传,聚焦 test_place_orders 的钱包选择/下单/异常路径。
        """

        def __init__(self, api, db, addr):
            pass

        def filter_for_template(self, pool, tmpl, addr):
            return list(pool)

    def test_no_eligible_markets_returns_scan_hint(self):
        manager, db = _make_manager()
        manager.eligible_markets = []
        result = manager.test_place_orders()
        assert result == {"ok": False, "message": "请先扫描市场"}

    def test_no_enabled_wallet_returns_error(self):
        manager, db = _make_manager()
        db.list_wallets.return_value = [
            {"address": "0xABC", "encrypted_key": "e1", "enabled": 0},
        ]
        manager.eligible_markets = [{"market_competitiveness": 0.5}]
        result = manager.test_place_orders()
        assert result == {"ok": False, "message": "没有启用的钱包"}

    def test_no_running_worker_builds_transient_api_and_places(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.eligible_markets = [{"market_competitiveness": 0.5, "name": "m"}]
        # engines empty -> first enabled wallet has no running worker;
        # must construct a transient API/worker just to place the orders.
        fake_worker = MagicMock()
        with patch("engine.manager.decrypt", return_value="0xkey"), patch(
            "engine.manager.PolymarketAPI"
        ) as mock_api_cls, patch(
            "engine.manager.WalletWorker", return_value=fake_worker
        ), patch(
            "engine.manager.MarketScanner", self._FakeScanner
        ):
            result = manager.test_place_orders()
        assert result["ok"] is True
        mock_api_cls.assert_called_once()
        fake_worker.place_orders.assert_called_once()
        _, kwargs = fake_worker.place_orders.call_args
        assert kwargs.get("limit") == 3
        # transient worker must NOT start a monitor thread
        fake_worker.start.assert_not_called()

    def test_places_on_first_enabled_running_worker_with_limit_3(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.eligible_markets = [
            {"market_competitiveness": 0.9, "name": "high"},
            {"market_competitiveness": 0.1, "name": "low"},
        ]
        worker = MagicMock()
        worker.running = True
        # db.list_wallets()[0] is 0xABC (enabled) per _make_manager
        manager.engines = {"0xABC": worker}
        with patch("engine.manager.MarketScanner", self._FakeScanner):
            result = manager.test_place_orders()
        assert result["ok"] is True
        worker.place_orders.assert_called_once()
        args, kwargs = worker.place_orders.call_args
        passed_markets = args[0]
        # sorted ascending by competitiveness: low (0.1) before high (0.9)
        assert [m["name"] for m in passed_markets] == ["low", "high"]
        assert kwargs.get("limit") == 3

    def test_place_orders_exception_returns_error_dict(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.eligible_markets = [{"market_competitiveness": 0.5}]
        worker = MagicMock()
        worker.running = True
        worker.place_orders.side_effect = RuntimeError("boom")
        manager.engines = {"0xABC": worker}
        with patch("engine.manager.MarketScanner", self._FakeScanner):
            result = manager.test_place_orders()
        assert result["ok"] is False
        assert "boom" in result["message"]

    def test_skips_disabled_or_not_running_picks_first_valid(self):
        manager, db = _make_manager()
        db.list_wallets.return_value = [
            {"address": "0xABC", "encrypted_key": "e1", "enabled": 0},
            {"address": "0xDEF", "encrypted_key": "e2", "enabled": 1},
        ]
        manager.eligible_markets = [{"market_competitiveness": 0.5, "name": "m"}]
        stopped = MagicMock()
        stopped.running = False
        good = MagicMock()
        good.running = True
        manager.engines = {"0xABC": stopped, "0xDEF": good}
        with patch("engine.manager.MarketScanner", self._FakeScanner):
            result = manager.test_place_orders()
        assert result["ok"] is True
        good.place_orders.assert_called_once()
        stopped.place_orders.assert_not_called()

    def test_filters_candidate_pool_per_wallet_before_placing(self):
        # 回归:eligible_markets 是候选池(按市场、无 token_id);test_place_orders
        # 必须先 filter_for_template 精筛成逐 token eligible,否则 place_orders
        # 取 token_id 会 KeyError(此前直接把候选池喂给 place_orders)。
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.eligible_markets = [{"condition_id": "A", "tokens": [], "tags": []}]
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}

        filtered = [{"market_id": "A", "token_id": "A-y", "market_competitiveness": 0}]

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def filter_for_template(self, pool, tmpl, addr):
                return list(filtered)

        with patch("engine.manager.MarketScanner", FakeScanner):
            res = manager.test_place_orders()

        assert res["ok"] is True
        worker.place_orders.assert_called_once()
        passed = worker.place_orders.call_args[0][0]
        assert passed == filtered  # 收到的是精筛后的逐 token eligible,而非候选池
        assert worker.place_orders.call_args[1].get("limit") == 3


class TestScanMarketsLastScanTime:
    def test_last_scan_time_only_updates_at_round_completion(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        observed = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                on_found({"market_id": "m1"})
                observed.append(manager.last_scan_time)
                on_found({"market_id": "m2"})
                observed.append(manager.last_scan_time)
                return [{"market_id": "m1"}, {"market_id": "m2"}]

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        assert manager.last_scan_time == 0
        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()
        assert observed == [0, 0]
        assert manager.last_scan_time > 0
        assert manager.scan_status == "done"
        assert manager.eligible_markets == [{"market_id": "m1"}, {"market_id": "m2"}]


class TestSharedScanWithStatus:
    def test_manual_scan_sets_scanning_then_done(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        seen = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                on_progress(1, 2, "checking")
                seen.append(manager.scan_status)
                on_found({"market_id": "m1"})
                return [{"market_id": "m1"}]

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()
        assert seen == ["scanning"]
        assert manager.scan_status == "done"
        assert manager.last_scan_time > 0
        assert manager.eligible_markets == [{"market_id": "m1"}]
        db.save_eligible_markets.assert_called_once_with([{"market_id": "m1"}])

    def test_place_round_filters_per_wallet_and_places(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = [{"market_id": "m9", "tags": []}]

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def prefilter_for_template(self, pool, tmpl, addr):
                return list(pool)

            def refresh_orderbooks(self, pool):
                pass

            def filter_for_template(self, pool, tmpl, addr):
                return pool

            def book_eligible(self, market, tmpl):
                return [market]

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._place_round()
        worker.place_orders.assert_called_once_with(
            [{"market_id": "m9", "tags": []}], cancel_dropouts=True
        )

    def test_place_round_empty_pool_skips_placement(self):
        # 空候选池 -> 不下单,避免 cancel_dropouts 误撤全部买单。
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = []
        manager._place_round()
        worker.place_orders.assert_not_called()

    def test_place_round_distributes_sorted_by_competitiveness(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = [
            {"market_id": "hi", "market_competitiveness": 0.9},
            {"market_id": "lo", "market_competitiveness": 0.1},
            {"market_id": "mid", "market_competitiveness": 0.5},
        ]

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def prefilter_for_template(self, pool, tmpl, addr):
                return list(pool)

            def refresh_orderbooks(self, pool):
                pass

            def filter_for_template(self, pool, tmpl, addr):
                return list(pool)

            def book_eligible(self, market, tmpl):
                return [market]

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._place_round()
        distributed = worker.place_orders.call_args[0][0]
        assert [m["market_id"] for m in distributed] == ["lo", "mid", "hi"]

    def test_should_discover_empty_pool_true(self):
        manager, db = _make_manager()
        manager.eligible_markets = []
        manager.last_scan_time = 1000.0
        assert manager._should_discover(1000.0) is True

    def test_should_discover_recent_false(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "m1"}]
        manager.last_scan_time = 1000.0
        assert manager._should_discover(1030.0) is False

    def test_should_discover_stale_true(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "m1"}]
        manager.last_scan_time = 1000.0
        assert manager._should_discover(1000.0 + 14401) is True

    def test_discover_skips_orderbook(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        captured = {}

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                captured.update(kw)
                return [{"market_id": "m1"}]

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._discover()
        assert captured.get("skip_orderbook") is True
        assert manager.eligible_markets == [{"market_id": "m1"}]
        assert manager.last_scan_time > 0

    def test_should_discover_at_interval_boundary_true(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "m1"}]
        manager.last_scan_time = 1000.0
        # 恰好等于间隔 -> >= -> 该发现
        assert manager._should_discover(1000.0 + 14400) is True

    def test_discovery_fail_then_place_uses_prev_pool(self):
        # 发现失败保留上一份缓存池,本轮 _place_round 仍用它刷簿下单。
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = [{"market_id": "prev", "tags": []}]
        manager.last_scan_time = 1000.0
        refreshed = {}

        class FlakyScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                raise RuntimeError("discovery down")

            def prefilter_for_template(self, pool, tmpl, addr):
                return list(pool)

            def refresh_orderbooks(self, pool):
                refreshed["pool"] = pool

            def filter_for_template(self, pool, tmpl, addr):
                return pool

            def book_eligible(self, market, tmpl):
                return [market]

        with patch("engine.manager.MarketScanner", FlakyScanner):
            try:
                manager._discover()  # raises; _scan_with_status 恢复 prev_eligible
            except RuntimeError:
                pass
            manager._place_round()
        assert manager.eligible_markets == [{"market_id": "prev", "tags": []}]
        assert refreshed["pool"] == [{"market_id": "prev", "tags": []}]
        worker.place_orders.assert_called_once_with(
            [{"market_id": "prev", "tags": []}], cancel_dropouts=True
        )

    def test_scan_failure_resets_status_and_keeps_last_scan_time(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.last_scan_time = 12345.0
        manager.eligible_markets = [{"market_id": "prev"}]

        class BoomScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                raise RuntimeError("scanner blew up")

        with patch("engine.manager.MarketScanner", BoomScanner):
            with pytest.raises(RuntimeError):
                manager._scan_with_status()
        assert manager.scan_status == "done"
        assert manager.last_scan_time == 12345.0
        assert manager.eligible_markets == [{"market_id": "prev"}]


class TestCategoryCatalog:
    """品类计数快照:本地持久化 + 秒显、扫描/手动刷新更新,普通打开绝不联网。"""

    def _mgr(self, stored):
        db = MagicMock()
        db.get_category_catalog.return_value = stored
        return EngineManager(db, encryption_key=b"x" * 32), db

    def test_no_snapshot_returns_static_without_network(self):
        from config import CATEGORY_CATALOG

        mgr, db = self._mgr(None)
        with patch("engine.manager.MarketScanner") as MS:
            out = mgr.category_catalog()
            MS.assert_not_called()  # 普通打开绝不联网
        assert out["ready"] is False
        assert {c["slug"] for c in out["categories"]} == {
            c["slug"] for c in CATEGORY_CATALOG
        }

    def test_stored_snapshot_served_without_network(self):
        snap = {
            "ready": True,
            "categories": [{"slug": "x", "label": "X", "count": 2}],
            "other_count": 1,
            "updated_at": 5.0,
        }
        mgr, db = self._mgr(snap)
        with patch("engine.manager.MarketScanner") as MS:
            out = mgr.category_catalog()
            MS.assert_not_called()
        assert out == snap

    def test_persist_catalog_caches_and_saves(self):
        mgr, db = self._mgr(None)
        out = mgr._persist_catalog(
            {"categories": [{"slug": "x", "label": "X", "count": 1}], "other_count": 2}
        )
        assert out["ready"] is True and "updated_at" in out and out["other_count"] == 2
        db.save_category_catalog.assert_called_once_with(out)
        # 之后普通打开命中内存缓存,不读库也不联网
        db.get_category_catalog.reset_mock()
        with patch("engine.manager.MarketScanner") as MS:
            assert mgr.category_catalog() is out
            MS.assert_not_called()
        db.get_category_catalog.assert_not_called()

    def test_scan_persists_last_catalog(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        snap = {
            "categories": [{"slug": "x", "label": "X", "count": 3}],
            "other_count": 0,
        }

        class OkScanner:
            def __init__(self, api, db, addr):
                self.last_catalog = snap

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                return [{"market_id": "m1"}]

        with patch("engine.manager.MarketScanner", OkScanner):
            manager._scan_with_status()
        db.save_category_catalog.assert_called_once()
        saved = db.save_category_catalog.call_args.args[0]
        assert saved["ready"] is True
        assert saved["categories"] == snap["categories"]


class TestScanGuard:
    """手动扫描是后台线程;守卫不重复开、并同步置位 scan_status 供前端立即可见。"""

    def test_start_scan_async_starts_when_idle(self):
        manager, db = _make_manager()
        manager.scan_status = "idle"
        with patch("engine.manager.threading.Thread") as T:
            assert manager.start_scan_async() is True
            assert manager.scan_status == "scanning"  # 同步置位
            T.assert_called_once()
            assert T.call_args.kwargs.get("target") == manager._run_scan
        T.return_value.start.assert_called_once()

    def test_start_scan_async_guards_double_scan(self):
        manager, db = _make_manager()
        manager.scan_status = "scanning"
        with patch("engine.manager.threading.Thread") as T:
            assert manager.start_scan_async() is False
        T.assert_not_called()  # 已在扫描,不再开第二个线程

    def test_run_scan_resets_stuck_status(self):
        # 无钱包时 scan_markets 在 _ensure_scanner_api 就早退,不进 _scan_with_status;
        # _run_scan 的 finally 必须把 scan_status 从 scanning 收敛掉,别永远卡住。
        manager, db = _make_manager()
        manager.scan_status = "scanning"
        gen = manager._scan_generation
        with patch.object(manager, "scan_markets"):  # no-op,不触碰 scan_status
            manager._run_scan(gen)
        assert manager.scan_status == "done"

    def test_start_scan_async_force_supersedes(self):
        manager, db = _make_manager()
        manager.scan_status = "scanning"
        manager._scan_generation = 5
        with patch("engine.manager.threading.Thread") as T:
            assert (
                manager.start_scan_async(force=True) is True
            )  # force 无视「已在扫描」
            assert manager._scan_generation == 6  # 代际 +1,旧扫描据此让位
            assert manager.scan_status == "scanning"
            assert T.call_args.kwargs.get("args") == (6,)  # 新线程带新代际
        T.return_value.start.assert_called_once()

    def test_scan_with_status_discards_when_superseded(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager._scan_generation = 2
        manager.eligible_markets = [{"market_id": "keep"}]

        class FakeScanner:
            def __init__(self, api, db, addr):
                self.last_catalog = None

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, cancel=None, **kw
            ):
                return [{"market_id": "new"}]

        with patch("engine.manager.MarketScanner", FakeScanner):
            out = manager._scan_with_status(gen=1)  # gen 1 != 当前代 2 -> 非当前代
        assert out == [{"market_id": "new"}]  # 结果仍返回上层
        assert manager.eligible_markets == [{"market_id": "keep"}]  # 但共享状态没被覆盖
        db.save_category_catalog.assert_not_called()

    def test_scan_markets_skips_save_when_superseded(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager._scan_generation = 2

        class FakeScanner:
            def __init__(self, api, db, addr):
                self.last_catalog = None

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, cancel=None, **kw
            ):
                return [{"market_id": "new"}]

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets(gen=1)  # gen 1 != 当前代 2
        db.save_eligible_markets.assert_not_called()  # 被接管:不落库(否则覆盖成废结果)

    def test_scan_threads_reward_scan_max_pages_from_settings(self):
        from config import ENGINE_DEFAULTS

        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        settings = dict(ENGINE_DEFAULTS)
        settings["reward_scan_max_pages"] = 42
        db.get_settings.return_value = settings
        seen = {}

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                seen["max_pages"] = kw.get("max_pages")
                return []

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()
        assert seen["max_pages"] == 42


class TestPlaceRoundRefreshesOnlySurvivors:
    """下单轮只给「无簿门槛已通过」的市场刷订单簿。

    整池 300+ 市场全刷、每 token 一次网络请求,是首单等待时间的大头——而真正能下单的
    只有几十个,其余市场的簿抓回来只是被 filter 当场丢掉(2026-07-14)。
    """

    def _fake_scanner(self, survivors_by_wallet, refreshed):
        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def prefilter_for_template(self, pool, tmpl, addr):
                keep = survivors_by_wallet[addr]
                return [m for m in pool if m["condition_id"] in keep]

            def refresh_orderbooks(self, pool):
                refreshed.extend(m["condition_id"] for m in pool)

            def filter_for_template(self, pool, tmpl, addr):
                return list(pool)

            def book_eligible(self, market, tmpl):
                return [market]

        return FakeScanner

    def _pool(self, *cids):
        return [{"condition_id": c, "market_id": c} for c in cids]

    def test_refreshes_only_prefiltered_markets(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xA": worker}
        manager.eligible_markets = self._pool("m1", "m2", "m3")
        refreshed = []

        with patch(
            "engine.manager.MarketScanner",
            self._fake_scanner({"0xA": {"m2"}}, refreshed),
        ):
            manager._place_round()

        assert refreshed == ["m2"]  # m1/m3 连簿都不抓
        placed = worker.place_orders.call_args[0][0]
        assert [m["condition_id"] for m in placed] == ["m2"]

    def test_union_across_wallets_refreshed_once(self):
        # 两个钱包模板不同 -> 幸存集取并集刷一次(共有市场不重复抓簿)。
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        wa, wb = MagicMock(), MagicMock()
        wa.running = wb.running = True
        manager.engines = {"0xA": wa, "0xB": wb}
        manager.eligible_markets = self._pool("m1", "m2", "m3")
        refreshed = []

        with patch(
            "engine.manager.MarketScanner",
            self._fake_scanner({"0xA": {"m1", "m2"}, "0xB": {"m2", "m3"}}, refreshed),
        ):
            manager._place_round()

        assert sorted(refreshed) == ["m1", "m2", "m3"]
        assert len(refreshed) == 3  # m2 只刷一次
        assert [m["condition_id"] for m in wa.place_orders.call_args[0][0]] == [
            "m1",
            "m2",
        ]
        assert [m["condition_id"] for m in wb.place_orders.call_args[0][0]] == [
            "m2",
            "m3",
        ]

    def test_no_survivor_skips_orderbook_fetch_entirely(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xA": worker}
        manager.eligible_markets = self._pool("m1")
        refreshed = []

        with patch(
            "engine.manager.MarketScanner",
            self._fake_scanner({"0xA": set()}, refreshed),
        ):
            manager._place_round()

        assert refreshed == []
        worker.place_orders.assert_called_once_with([], cancel_dropouts=True)


class TestActiveTemplatesDedupKey:
    """_active_templates 去重键必须含发现阶段实际用到的每个维度。发现阶段现在也用结算
    窗口和档位 sizes 做并集门控,所以窗口/档位不同的模板不能被去重成一个(否则另一个的
    窗口/档位没进并集 -> 误剔本该被它要的市场)。"""

    def _mgr_with(self, tmpl_for):
        manager, db = _make_manager()
        db.list_wallets.return_value = [
            {"address": "0xA", "enabled": True},
            {"address": "0xB", "enabled": True},
        ]
        db.get_template_for.side_effect = tmpl_for
        return manager

    def _base(self, **over):
        t = {
            "included_categories": ["politics"],
            "include_other": False,
            "min_reward_usd": 100,
            "size_tiers": [],
            "min_settlement_days": 0,
            "max_settlement_days": None,
        }
        t.update(over)
        return t

    def test_window_variants_not_deduped(self):
        def tmpl_for(addr):
            return self._base(max_settlement_days=0 if addr == "0xA" else 5)

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 2

    def test_tier_variants_not_deduped(self):
        def tmpl_for(addr):
            sizes = (
                [{"size": 100, "enabled": True}]
                if addr == "0xA"
                else [{"size": 300, "enabled": True}]
            )
            return self._base(size_tiers=sizes)

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 2

    def test_skip_new_markets_variants_not_deduped(self):
        def tmpl_for(addr):
            return self._base(skip_new_markets=(addr == "0xA"))

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 2

    def test_new_market_hours_variants_not_deduped(self):
        def tmpl_for(addr):
            return self._base(
                skip_new_markets=True,
                new_market_hours=24 if addr == "0xA" else 72,
            )

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 2

    def test_identical_templates_still_deduped(self):
        def tmpl_for(addr):
            return self._base()

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 1

    def test_skip_new_categories_variants_not_deduped(self):
        def tmpl_for(addr):
            return self._base(
                skip_new_markets=True,
                new_market_hours=24,
                skip_new_categories=["politics"] if addr == "0xA" else ["crypto"],
            )

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 2

    def test_skip_new_other_variants_not_deduped(self):
        def tmpl_for(addr):
            return self._base(
                skip_new_markets=True,
                new_market_hours=24,
                skip_new_other=(addr == "0xA"),
            )

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 2


class TestUpdateMarketReward:
    """实时奖励写回候选池:内存池条目就地改写 + DB 同步,失败不外抛。"""

    def _mgr(self, pool):
        db = MagicMock()
        manager = EngineManager(db, encryption_key=b"x" * 32)
        manager.eligible_markets = pool
        return manager, db

    def test_rewrites_pool_entry_and_syncs_db(self):
        pool = [
            {"condition_id": "0xabc", "market_reward": 300.0, "daily_reward": 300.0},
            {"condition_id": "0xother", "market_reward": 300.0, "daily_reward": 300.0},
        ]
        manager, db = self._mgr(pool)
        manager.update_market_reward("0xabc", 5.0)
        # 命中的市场两个键都改写(prefilter 判 market_reward,前端显示 daily_reward)
        assert pool[0]["market_reward"] == 5.0
        assert pool[0]["daily_reward"] == 5.0
        # 其它市场不受影响
        assert pool[1]["market_reward"] == 300.0
        db.update_eligible_reward.assert_called_once_with("0xabc", 5.0)

    def test_market_not_in_pool_still_syncs_db(self):
        manager, db = self._mgr([])
        manager.update_market_reward("0xabc", 5.0)
        db.update_eligible_reward.assert_called_once_with("0xabc", 5.0)


class TestRewardUpdateCallbackWiring:
    """回调一路从 manager 注入到 monitor;monitor 侧调用永不外抛。"""

    def test_worker_passes_callback_to_monitor(self):
        cb = MagicMock()
        worker = WalletWorker(
            MagicMock(),
            MagicMock(),
            "0xW",
            {"fill_check_interval_sec": 5},
            on_reward_update=cb,
        )
        assert worker.monitor.on_reward_update is cb

    def test_worker_without_callback_defaults_to_none(self):
        worker = WalletWorker(
            MagicMock(), MagicMock(), "0xW", {"fill_check_interval_sec": 5}
        )
        assert worker.monitor.on_reward_update is None

    def test_notify_is_noop_without_callback(self):
        from engine.monitor import OrderMonitor

        monitor = OrderMonitor(MagicMock(), MagicMock(), "0xW")
        monitor._notify_reward_update("0xabc", 5.0)  # 不抛异常即可

    def test_notify_swallows_callback_failure(self):
        from engine.monitor import OrderMonitor

        cb = MagicMock(side_effect=RuntimeError("db down"))
        monitor = OrderMonitor(MagicMock(), MagicMock(), "0xW", on_reward_update=cb)
        monitor._notify_reward_update("0xabc", 5.0)  # 写回失败绝不能中断交易流程
        cb.assert_called_once_with("0xabc", 5.0)


def test_pnl_start_date_is_project_start():
    """台账补漏起点 = 项目首次提交日（27cc9bc，2026-05-17），不是当初的 2026-07-01。

    rebuild_wallet_pnl 本来就拉全量 activity/trades、不带时间过滤，_date_range 只决定
    往 daily_pnl 写哪些天 —— 前移没有网络代价，只是多写几十行本地记录。
    """
    from engine.manager import PNL_START_DATE

    assert PNL_START_DATE == "2026-05-17"


class TestTickSharesOpenOrders:
    """一个 tick 里 get_open_orders 取两次:步骤 2-4 共用一份,Step3 自取新鲜的。

    Step1(check_buy_orders)也自取,但它那次取数只在**真检测到成交**时才发生;本组用例
    的 get_trades 返回 [],没有成交,所以次数仍是两次(有成交的轮会是三次)。
    """

    def _worker(self):
        from engine.manager import WalletWorker

        api = MagicMock()
        db = MagicMock()
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []
        api.get_user_positions.return_value = []
        api.gamma_resolution_status.return_value = {}
        api.get_balance.return_value = 100.0
        db.get_settings.return_value = {"rewards_cache_ttl_sec": 0}
        db.get_template_for.return_value = {
            "low_balance_threshold_usd": 0,
            "fast_exit_enabled": False,
        }
        db.get_blacklist_ids.return_value = set()
        worker = WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5})
        # 不让台账重算线程掺进来:_maybe_rebuild_pnl 只在 _last_pnl_date == 今天(beijing_day)
        # 时才跳过,写死的 "9999-01-01" 永远不等于今天的日期字符串,起不到拦截作用——直接
        # 把方法本身替换成空操作,才能确保它真的不起后台线程碰共享 MagicMock。
        worker._maybe_rebuild_pnl = MagicMock()
        return worker, api, db

    def test_tick_fetches_open_orders_twice(self):
        worker, api, db = self._worker()
        worker._tick()
        assert api.get_open_orders.call_count == 2

    def test_shared_snapshot_failure_does_not_kill_the_tick(self):
        # 共用快照取不到时各步自己降级,tick 不能整个抛出去
        worker, api, db = self._worker()
        api.get_open_orders.side_effect = RuntimeError("network")
        worker._tick()  # 不抛异常即通过

    def test_tick_logs_per_step_timing(self, caplog):
        worker, api, db = self._worker()
        with caplog.at_level(logging.INFO, logger="engine.manager"):
            worker._tick()
        # 注意:不能用 r.message % r.args——caplog 的 handler 在捕获时已经跑过一次
        # Formatter.format(),record.message 此时已是完全替换过的最终字符串;再对它
        # 做一次 % r.args 会因为字符串里已经没有 % 占位符、但 args 非空而报
        # "not all arguments converted"(与本任务实现是否正确无关,任何带 %s 参数的
        # 日志调用都会这样,已用最小复现验证过)。直接用 r.message 即可。
        line = [r.message for r in caplog.records if "[tick]" in str(r.msg)]
        assert len(line) == 1
        for name in ("成交", "结算", "低余额", "离场", "合规"):
            assert name in line[0]

    def test_timing_log_emitted_even_when_a_step_raises(self, caplog):
        # 慢在哪一步的证据,不能因为那一步抛异常就丢掉
        worker, api, db = self._worker()
        worker.monitor.check_exit = MagicMock(side_effect=RuntimeError("boom"))
        with caplog.at_level(logging.INFO, logger="engine.manager"):
            with pytest.raises(RuntimeError):
                worker._tick()
        assert any("[tick]" in str(r.msg) for r in caplog.records)
