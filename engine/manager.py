"""engine/manager.py — Multi-wallet engine manager.

Architecture:
- One shared scanner thread: scans markets once, produces eligible list
- Per-wallet threads: each wallet uses the shared eligible list to place orders,
  then monitors fills/stop-loss independently
"""

import functools
import logging
import threading
import time
from datetime import datetime, timedelta
from api.polymarket_api import PolymarketAPI
from api.proxy import use_proxy
from config import CATEGORY_CATALOG
from engine.scanner import MarketScanner, ScanSuperseded
from engine.monitor import OrderMonitor
from engine.positions import held_side_info
from engine.resolution import in_resolution
from engine.tiers import tier_for
from engine.legacy_wall import (
    compute_market_legacy_orders,
    explain_legacy_order,
    legacy_price_basis,
    legacy_reason,
)
from engine.pnl import beijing_day
from engine.pnl_ledger import rebuild_wallet_pnl
from utils.crypto import decrypt

logger = logging.getLogger(__name__)


def _tmpl_num(tmpl: dict, key: str, default: float) -> float:
    """读模板里的数字参数,`None` 兜底成默认值。

    配置页的策略表单把清空的数字输入框送成 `null`(parseFloat('') → NaN →
    JSON null),后端白名单照单全收落库。不兜底的话下一轮 `int(None)` 抛
    TypeError,而 place_orders 的调用点只有 logger.error —— 该钱包每轮静默
    失败,UI 上完全看不出来。

    **只认 None,不能写成 `x or default`**:0 是好几个键的合法取值
    (cliff_probe_cents=0 关闭悬崖否决、order_size_custom_usd=0),被 `or`
    吞掉会把用户的显式配置换成默认值。
    """
    v = tmpl.get(key, default)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# 盈亏台账补漏起点(北京日)。启动/重启后从这天补到今天;每日跨天再重算。
# = 项目首次提交日(27cc9bc, 2026-05-17):全量 activity/trades 本来就全拉,前移只是多写
# 几十行本地记录,没有网络代价。
PNL_START_DATE = "2026-05-17"


def _worker_proxied(method):
    """WalletWorker 网络入口装饰器:整个操作期把代理上下文设为本钱包(self.api.proxy_url),
    使其中的静态公共数据调用(rewards/gamma)也走该钱包代理;实例方法已各自 _proxied。
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with use_proxy(getattr(self.api, "proxy_url", None)):
            return method(self, *args, **kwargs)

    return wrapper


class WalletWorker:
    """Per-wallet worker: places orders from shared eligible list, monitors fills."""

    def __init__(
        self,
        api: PolymarketAPI,
        db,
        wallet_address: str,
        settings: dict,
        on_reward_update=None,
    ):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        self.settings = settings
        self.monitor = OrderMonitor(
            api, db, wallet_address, on_reward_update=on_reward_update
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        # gap_single 「判成不挂」的去重:token -> 上次记录的原因(None=上次是挂单),
        # 只在判断变化时记 gap_skip,避免每轮下单都往历史刷同一条跳过。
        self._last_gap_skip: dict = {}
        # 盈亏台账:上次重算的北京日期(None=本进程还没算过)。首次(含每次重启)全量补漏
        # 2026-05-17 起,之后跨北京日再重算(幂等 upsert,近几天自然刷新)。**在后台线程跑**,
        # 绝不阻塞 _tick(离场/止损检查在同一 tick 里,不能被全量重算的网络往返拖住)。
        self._last_pnl_date = None
        self._pnl_rebuilding = False
        self._pnl_thread = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.running = True
        logger.info("Wallet worker started for %s", self.wallet_address)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)
        self.running = False
        self._cancel_buy_orders()
        logger.info("Wallet worker stopped for %s", self.wallet_address)

    def _cancel_buy_orders(self):
        """Cancel all open buy orders on the exchange in one batched request."""
        try:
            open_orders = self.api.get_open_orders()
            buy_ids = [o["id"] for o in open_orders if o.get("side") == "BUY"]
            if buy_ids:
                self.api.cancel_orders(buy_ids)
                logger.info(
                    "Cancelled %d buy orders for %s", len(buy_ids), self.wallet_address
                )
        except Exception as e:
            logger.error(
                "Error cancelling buy orders for %s: %s", self.wallet_address, e
            )

    def _run(self):
        """Monitor loop: run one tick at each check_interval.

        Each tick is isolated: an unhandled exception in any step is logged and
        the loop continues. Without this guard a single transient error killed
        the wallet's monitor thread for good — no fill detection, no stop-loss,
        no restart, the only symptom a frozen monitor-status table (audit F4).
        """
        check_interval = self.settings["fill_check_interval_sec"]
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.exception(
                    "Monitor tick crashed for %s (continuing): %s",
                    self.wallet_address,
                    e,
                )
            self._stop_event.wait(timeout=check_interval)

    @_worker_proxied
    def _tick(self):
        """One monitor pass: detect fills, UMA resolution guard, three-tier
        exit, strategy compliance. check_resolution runs right after fill
        detection to cancel buys in any market whose UMA resolution was just
        proposed; check_exit then reflects the latest fills.

        步骤 2-4(结算/低余额/离场)共用一份 tick 开头取的挂单快照。**Step1 与 Step3 各自
        自取**:Step1 的挂单是「成交后这单还在不在挂」这条防重放洪水护栏的判据,共用快照
        比它要判定的成交旧一个往返、会把护栏磨钝,而那次取数本来就只在真有成交时才发生;
        Step3 跑在最后、共用的那份最陈旧,而它会对已经消失的单再撤一次并往 actions 写
        幻影记录。两处省的都是一个低频往返,不值。快照取不到时传 None,各步自行降级。
        """
        t0 = time.time()
        marks: list = []

        def _step(name, fn, *a):
            t = time.time()
            try:
                fn(*a)
            finally:
                marks.append(f"{name}{time.time() - t:.1f}s")

        self._maybe_rebuild_pnl()
        self.monitor.begin_status_tick()
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.warning("tick 开头取挂单失败(各步自行降级): %s", e)
            open_orders = None
        try:
            _step("成交", self.monitor.check_buy_orders)
            _step("结算", self.monitor.check_resolution, open_orders)
            _step("低余额", self.monitor.check_low_balance, open_orders)
            _step("离场", self.monitor.check_exit, open_orders)
            _step("合规", self.monitor.check_sell_orders)
            self.monitor.publish_status()
        finally:
            # 监控周期 = 本轮耗时 + fill_check_interval_sec,而撤单/止损的响应延迟上限
            # 就是这个周期。哪一步在吃时间只有这条日志看得见,放 finally 是因为抛异常的
            # 那一轮往往正是最慢的那一轮,证据不能跟着异常一起丢。
            # 持仓数 None = 本轮离场早退没判定成(取持仓/挂单失败),渲染成 ?,
            # 绝不拿上一轮的数冒充这一轮。
            pos_n = self.monitor.last_position_count
            logger.info(
                "[tick] %s 持仓%s 挂单%s 总计%.1fs | %s",
                self.wallet_address[:8],
                pos_n if pos_n is not None else "?",
                len(open_orders) if open_orders is not None else "?",
                time.time() - t0,
                " ".join(marks),
            )

    def _maybe_rebuild_pnl(self):
        """每日盈亏台账:首个 tick(含每次重启)从 2026-05-17 全量补漏到今天;之后跨北京日
        再重算一次(幂等 upsert,近几天奖励次日发放/成交滞后自然刷新)。

        **在后台守护线程跑**,_tick 立即返回——全量重算要拉全历史 get_trades/activity(随
        账户增长、可能走抖动代理),绝不能坐在同一 tick 的离场/止损检查前面把它们拖慢。
        `_pnl_rebuilding` 防重入(一次只跑一个);失败只 WARNING、不置日期 -> 下拍重试。
        台账仅展示,不进任何交易决策。
        """
        today = beijing_day(time.time())
        if self._last_pnl_date == today or self._pnl_rebuilding:
            return
        self._pnl_rebuilding = True
        self._pnl_thread = threading.Thread(
            target=self._rebuild_pnl_now,
            args=(today,),
            daemon=True,
            name=f"pnl-{self.wallet_address[:8]}",
        )
        self._pnl_thread.start()

    def _rebuild_pnl_now(self, today):
        """后台线程体:全量重算并 upsert;成功才置 _last_pnl_date。DB 用每线程独立连接
        (惰性 conn property),API 实例方法各自 _proxied 自 route 钱包代理,均线程安全。"""
        try:
            rebuild_wallet_pnl(
                self.api, self.db, self.wallet_address, PNL_START_DATE, today
            )
            self._last_pnl_date = today
        except Exception as e:
            logger.warning("台账重算失败 %s: %s", self.wallet_address, e)
        finally:
            self._pnl_rebuilding = False

    @_worker_proxied
    def place_orders(
        self,
        eligible_markets: list[dict],
        limit: int | None = None,
        cancel_dropouts: bool = False,
    ):
        """断层单档挂单:按市场分组,每市场按断层分级选一档挂一单,整市场敞口两边共享。

        eligible_markets = filter_for_template 的轻量 per-token 条目。
        limit 设置时,达到该数量的成功下单后停止。
        """
        from engine.laddering import (
            compute_market_single_orders,
            explain_gap_single_order,
            gap_single_reason,
            gap_single_price_basis,
            reconcile_buy_orders,
        )
        from engine.strategy import reward_price_range

        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            return
        try:
            positions = self.api.get_user_positions(self.api.get_funder())
        except Exception as e:
            logger.error(
                "get_user_positions failed for %s, skip placement: %s",
                self.wallet_address,
                e,
            )
            return
        held_assets, held_value, held_shares = held_side_info(positions)
        blacklist = self.db.get_blacklist_ids()
        tmpl = self.db.get_template_for(self.wallet_address)
        size_tiers = tmpl.get("size_tiers") or []
        gap_wide_cents = _tmpl_num(tmpl, "gap_wide_cents", 10)
        gap_mid_cents = _tmpl_num(tmpl, "gap_mid_cents", 5)
        cliff_probe_cents = _tmpl_num(tmpl, "cliff_probe_cents", 2)
        gap_veto_cents = _tmpl_num(tmpl, "gap_veto_cents", 0)
        # max_exposure_usd 是「单市场」敞口上限(YES+NO 合计);跨市场不设全局
        # 美元锁(maker 买单不锁仓,一笔余额垫付所有挂单),总量由 max_concurrent
        # _markets × 单市场敞口 约束。
        max_exposure_usd = _tmpl_num(tmpl, "max_exposure_usd", 250)
        max_exposure_shares = int(_tmpl_num(tmpl, "max_exposure_shares", 500))
        max_concurrent = int(_tmpl_num(tmpl, "max_concurrent_markets", 10))

        buy_orders = [o for o in open_orders if o.get("side") == "BUY"]
        buys_by_token, markets_with_open = {}, set()
        for o in buy_orders:
            buys_by_token.setdefault(o.get("asset_id", ""), []).append(o)
            mkt = o.get("market", "")
            if mkt:
                markets_with_open.add(mkt)

        grouped, order = {}, []
        for e in eligible_markets:
            mid = e["market_id"]
            if mid not in grouped:
                grouped[mid] = []
                order.append(mid)
            grouped[mid].append(e)

        # SP5a-1 跌出 eligible 整仓撤买单：有在挂买单、不在本轮 eligible、且不在
        # 冷却的市场 -> 撤掉该市场全部 BUY（持仓/卖单不动，仍由 check_exit 卖出）。
        # 只在真正下单轮(_do_scan / place_all_orders)开启；冷却市场只是「暂不挂新单」
        # 故豁免，避免撤掉正赚奖励的旧买单、并不与 SP5b「另一侧照常运行」冲突。
        if cancel_dropouts:
            eligible_mids = set(grouped.keys())
            dropped = {
                o.get("market", "")
                for o in buy_orders
                if o.get("market")
                and o.get("market") not in eligible_mids
                and not self.db.is_in_cooldown(self.wallet_address, o.get("market"))
            }
            drop_ids = [
                o["id"]
                for o in buy_orders
                if o.get("market") in dropped and o.get("id")
            ]
            if drop_ids:
                try:
                    self.api.cancel_orders(drop_ids)
                    markets_with_open -= dropped
                    for mkt in dropped:
                        self.db.record_action(
                            wallet=self.wallet_address,
                            market_id=mkt,
                            action_type="dropout_cancel",
                            side="-",
                            price=-1,
                            size=0,
                            reason="市场跌出 eligible（不再满足筛选门槛），撤掉该市场全部买单；持仓仍由离场卖出",
                            price_basis="跌出 eligible；来源:CLOB get_open_orders + filter_for_template",
                        )
                except Exception as ex:
                    logger.warning("Dropout cancel failed: %s", ex)

        # UMA 结算守卫:跳过已有人在 UMA 提交 resolution 的市场,避免「监控线程撤、
        # 下单线程又挂」反复打架(发现每 4h 一次,市场不会立刻跌出 eligible)。Gamma
        # 失败 -> resolving 空 -> 不跳过(fail-open,与监控侧一致)。
        status_map = self.api.gamma_resolution_status(order)
        resolving = {c for c in order if in_resolution(status_map.get(c))}
        if resolving:
            logger.info(
                "place_orders skip %d markets in UMA resolution for %s",
                len(resolving),
                self.wallet_address,
            )

        placed = 0
        for mid in order:
            if mid in blacklist:
                continue
            if mid in resolving:
                continue
            if self.db.is_in_cooldown(self.wallet_address, mid):
                continue
            # 档位模块精确匹配(筛选层已挡;这里双点防御,防配置变更/共享列表时差)。
            tier = tier_for(size_tiers, grouped[mid][0].get("rewards_min_size"))
            if tier is None:
                continue
            amount_value_table = tier.get("amount_value_table") or None
            gap_high_coeff_sum_min = float(tier.get("gap_high_coeff_sum_min", 20))
            rule1_min_coeff = float(tier.get("rule1_min_coeff", 0))
            rule2_min_coeff = float(tier.get("rule2_min_coeff", 0))
            rule3_min_coeff = float(tier.get("rule3_min_coeff", 0))
            rule2_high_gate = float(tier.get("rule2_high_coeff_sum_min", 0))
            rule3_high_gate = float(tier.get("rule3_high_coeff_sum_min", 0))
            # shares 缺失/0 -> None:laddering 回退按 min_size 挂(奖励资格安全),
            # 绝不能把 0 当合法份数(plan 为空 -> reconcile 撤掉现有买单且不补,静默摘牌)。
            tier_shares = int(tier.get("shares", 0) or 0) or None
            placement_mode = str(tmpl.get("placement_mode", "gap_single"))
            legacy_wall_threshold = int(_tmpl_num(tmpl, "legacy_wall_threshold", 2000))
            legacy_cum_threshold = int(
                _tmpl_num(tmpl, "legacy_cumulative_threshold", 6000)
            )
            order_size_mode = str(tmpl.get("order_size_mode", "min"))
            order_size_custom_usd = _tmpl_num(tmpl, "order_size_custom_usd", 0)
            if (
                mid not in markets_with_open
                and len(markets_with_open) >= max_concurrent
            ):
                continue

            sides = []
            for e in grouped[mid]:
                token_id = e["token_id"]
                try:
                    ob = self.api.get_orderbook(token_id)
                except Exception as ex:
                    logger.warning(
                        "Orderbook failed for %s: %s", e.get("market_name", ""), ex
                    )
                    continue
                bids = sorted(
                    ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True
                )
                asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
                if not bids or not asks:
                    continue
                best_bid, best_ask = float(bids[0]["price"]), float(asks[0]["price"])
                midpoint = (best_bid + best_ask) / 2
                max_spread = float(e.get("rewards_max_spread", 2))
                rmin, rmax = reward_price_range(midpoint, max_spread)
                sides.append(
                    {
                        "token_id": token_id,
                        "outcome": e.get("outcome", ""),
                        "neg_risk": e.get("neg_risk", False),
                        "tick_size_str": ob.get("tick_size", "0.01"),
                        "tick_size": float(ob.get("tick_size", "0.01") or 0.01),
                        "min_size": int(e.get("rewards_min_size", 0) or 0),
                        "bids": bids,
                        "reward_range_min": rmin,
                        "reward_range_max": rmax,
                        "max_spread": max_spread,
                    }
                )
            if not sides:
                continue

            side_a = sides[0]
            side_b = sides[1] if len(sides) > 1 else None
            try:
                # 循环里唯一的余额读取(每市场重读一次,防并发成交超卖——别改成缓存)。
                # 失败只跳过这个市场:整轮抛出会让排序靠后的市场既不下单也不撤单,而市场
                # 顺序按竞争度固定,等于被系统性饿死(2026-07-29 实盘 159 次整轮早退)。
                balance = self.api.get_balance()
            except Exception as ex:
                logger.warning(
                    "get_balance failed for %s, skip market %s: %s",
                    self.wallet_address,
                    mid,
                    ex,
                )
                continue
            budget = max(0.0, min(balance, max_exposure_usd) - held_value.get(mid, 0.0))
            shares_budget = max(0, max_exposure_shares - int(held_shares.get(mid, 0.0)))
            budget_ok = budget > 0 and shares_budget > 0
            ladders = {"a": [], "b": []}
            # gap_single 每边的完整判断(供 place_buy 记真实原因、判成不挂时记 gap_skip)。
            gap_explains = {"a": None, "b": None}
            if budget_ok:
                ca = None if side_a["token_id"] in held_assets else side_a
                cb = None if (side_b and side_b["token_id"] in held_assets) else side_b
                if placement_mode == "legacy_wall":
                    ladders = compute_market_legacy_orders(
                        ca,
                        cb,
                        budget,
                        shares_budget,
                        balance,
                        order_size_mode,
                        order_size_custom_usd,
                        wall_threshold=legacy_wall_threshold,
                        cumulative_threshold=legacy_cum_threshold,
                        cliff_probe_cents=cliff_probe_cents,
                    )
                    for gkey, gside in (("a", ca), ("b", cb)):
                        if gside is not None:
                            gap_explains[gkey] = explain_legacy_order(
                                gside["bids"],
                                int(gside["max_spread"]),
                                gside["tick_size"],
                                gside["reward_range_min"],
                                gside["reward_range_max"],
                                gside["min_size"],
                                order_size_mode,
                                balance,
                                order_size_custom_usd,
                                wall_threshold=legacy_wall_threshold,
                                cumulative_threshold=legacy_cum_threshold,
                                cliff_probe_cents=cliff_probe_cents,
                            )
                else:
                    ladders = compute_market_single_orders(
                        ca,
                        cb,
                        budget,
                        shares_budget,
                        amount_value_table,
                        gap_wide_cents,
                        gap_mid_cents,
                        gap_high_coeff_sum_min,
                        rule1_min_coeff,
                        rule2_min_coeff,
                        rule3_min_coeff,
                        cliff_probe_cents,
                        shares=tier_shares,
                        rule2_high_coeff_sum_min=rule2_high_gate,
                        rule3_high_coeff_sum_min=rule3_high_gate,
                        gap_veto_cents=gap_veto_cents,
                    )
                    for gkey, gside in (("a", ca), ("b", cb)):
                        if gside is not None:
                            gap_explains[gkey] = explain_gap_single_order(
                                gside["bids"],
                                gside["reward_range_min"],
                                gside["reward_range_max"],
                                gside["min_size"],
                                amount_value_table,
                                gap_wide_cents,
                                gap_mid_cents,
                                gap_high_coeff_sum_min,
                                rule1_min_coeff,
                                rule2_min_coeff,
                                rule3_min_coeff,
                                cliff_probe_cents,
                                shares=tier_shares,
                                rule2_high_coeff_sum_min=rule2_high_gate,
                                rule3_high_coeff_sum_min=rule3_high_gate,
                                gap_veto_cents=gap_veto_cents,
                            )

            for key, side in (("a", side_a), ("b", side_b)):
                if side is None:
                    continue
                token_id = side["token_id"]
                resting = buys_by_token.get(token_id, [])
                if token_id in held_assets:
                    # 成交后单侧暂停:撤光该侧全部在挂买单、不挂新单(SP5b Q1)
                    cancel_ids, to_place = reconcile_buy_orders([], resting)
                    cancel_reason = "成交后单侧暂停:撤掉该侧全部买单,直至该侧持仓平掉"
                    cancel_action = "side_pause_cancel"
                elif budget_ok:
                    # 老策略的 balance/custom 份数按实时余额算,做市成交会让余额不断
                    # 漂移(实测余额 100→95 就够让目标份数跨出 1% 容差)。默认容差会
                    # 变成「余额动一点就全撤重挂」,反复丢掉队列位置、少吃奖励。这两种
                    # 模式放宽到 10%;min 模式与 gap_single 的份数是死数,维持 1%。
                    tol = (
                        0.10
                        if (
                            placement_mode == "legacy_wall"
                            and order_size_mode in ("balance", "custom")
                        )
                        else 0.01
                    )
                    cancel_ids, to_place = reconcile_buy_orders(
                        ladders.get(key, []), resting, size_tolerance_pct=tol
                    )
                    cancel_reason = "撤改收敛:撤掉价漂移/量不符的旧买单(目标挂价已变)"
                    cancel_action = "buy_reconcile_cancel"
                else:
                    # 预算不足(扣减后):活跃侧保持不动
                    continue
                if cancel_ids:
                    try:
                        self.api.cancel_orders(cancel_ids)
                        self.db.record_action(
                            wallet=self.wallet_address,
                            market_id=mid,
                            action_type=cancel_action,
                            side="-",
                            price=-1,
                            size=0,
                            reason=cancel_reason,
                            price_basis=f"撤 {len(cancel_ids)} 笔 BUY；来源：CLOB get_open_orders",
                        )
                    except Exception as ex:
                        logger.warning(
                            "Reconcile/pause cancel %s failed: %s", token_id, ex
                        )
                gap_d = gap_explains.get(key)
                for price, shares in to_place:
                    try:
                        self.api.place_limit_buy(
                            token_id,
                            price,
                            shares,
                            tick_size=side["tick_size_str"],
                            # 奖励端点(get_rewards_markets)不含 neg_risk 字段,候选里的
                            # neg_risk 恒为 False -> 负风险市场买单会被 CLOB 拒(invalid
                            # POLY_GNOSIS_SAFE signature)。传 None 让客户端按 token_id 自查
                            # 真实 neg_risk,与卖单一致(2026-07-05 事故)。
                            neg_risk=None,
                        )
                        placed += 1
                        self._touch_active()
                        markets_with_open.add(mid)
                        if gap_d and gap_d.get("action") == "place":
                            if placement_mode == "legacy_wall":
                                r_fn, b_fn = legacy_reason, legacy_price_basis
                            else:
                                r_fn, b_fn = gap_single_reason, gap_single_price_basis
                            # 文案必须报**实际挂出去**的份数:决策 dict 里的 shares 是
                            # 未经预算/敞口封顶的理论值(老策略 balance 模式下按钱包总
                            # 余额算,实测能出现「文案 3448 份、实挂 500 份」),写进历史
                            # 就是审计说谎。用 to_place 里的真实份数覆盖后再格式化。
                            text_d = dict(gap_d, shares=shares)
                            self._record_place_buy_tier(
                                mid,
                                side,
                                price,
                                shares,
                                reason=r_fn(text_d),
                                price_basis=b_fn(
                                    text_d,
                                    side["reward_range_min"],
                                    side["reward_range_max"],
                                ),
                            )
                            self._last_gap_skip[token_id] = None  # 挂成功->清跳过去重
                        else:
                            self._record_place_buy_tier(mid, side, price, shares)
                        if limit is not None and placed >= limit:
                            return
                    except Exception as ex:
                        logger.error("place_limit_buy failed %s: %s", token_id, ex)
                # ② 判成不挂:记 gap_skip(按 token 去重,判断变化才记)。
                self._maybe_record_gap_skip(
                    mid, side, gap_explains.get(key), placement_mode
                )

    def _touch_active(self):
        """记一次「上次活跃」(纯展示)。写失败绝不能打断下单,吞掉只打 warning。"""
        try:
            self.db.touch_wallet_active(self.wallet_address)
        except Exception as e:
            logger.warning("touch_wallet_active failed %s: %s", self.wallet_address, e)

    def _record_place_buy_tier(
        self, market_id, side, price, shares, reason=None, price_basis=None
    ):
        """记一档买单到 actions(不抛异常)。reason/price_basis 缺省用通用文案;
        正常路径由调用方传入断层单档的真实原因/价格依据。"""
        try:
            self.db.record_action(
                wallet=self.wallet_address,
                market_id=market_id,
                action_type="place_buy",
                side="买入",
                price=price,
                size=shares,
                reason=reason or "断层单档:在奖励区间内按风险系数选一档挂买单",
                price_basis=price_basis
                or (
                    f"档价 {price:.4f}（{side.get('outcome','')}）；"
                    f"奖励区间[{side['reward_range_min']:.4f},{side['reward_range_max']:.4f}]；"
                    f"来源：CLOB get_orderbook"
                ),
            )
        except Exception as e:
            logger.warning("record_action(place_buy) failed: %s", e)

    def _maybe_record_gap_skip(
        self, market_id, side, decision, placement_mode="gap_single"
    ):
        """判成不挂时记一条 gap_skip(按 token 去重:同一原因只记一次,避免每轮下单
        往历史刷同一条)。decision=None/非 skip -> 不记。两种挂单模式共用这一条动作
        类型,只是价格依据的格式化函数不同。"""
        from engine.laddering import gap_single_price_basis

        basis_fn = (
            legacy_price_basis
            if placement_mode == "legacy_wall"
            else gap_single_price_basis
        )

        token_id = side["token_id"]
        if not decision or decision.get("action") != "skip":
            return
        reason = decision.get("skip_reason") or "断层单档判定不挂"
        if self._last_gap_skip.get(token_id) == reason:
            return
        self._last_gap_skip[token_id] = reason
        try:
            self.db.record_action(
                wallet=self.wallet_address,
                market_id=market_id,
                action_type="gap_skip",
                side=side.get("outcome", "-") or "-",
                price=-1,
                size=0,
                reason=reason,
                price_basis=basis_fn(
                    decision,
                    side["reward_range_min"],
                    side["reward_range_max"],
                ),
            )
        except Exception as e:
            logger.warning("record_action(gap_skip) failed: %s", e)


class EngineManager:
    """Manages the shared scanner and per-wallet workers.

    Architecture:
    - One scanner thread runs periodically, producing an eligible markets list
    - Per-wallet workers use that list to place orders
    - Each wallet also has its own monitor thread for fills/stop-loss
    """

    def __init__(self, db, encryption_key: bytes):
        self.db = db
        self.encryption_key = encryption_key
        self.engines: dict[str, WalletWorker] = {}
        self._scanner_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._scanner_api: PolymarketAPI | None = None  # Shared API for scanning
        self._catalog_cache = None
        self._catalog_cache_ts = 0.0
        self.eligible_markets: list[dict] = []  # Latest scan results
        self.last_scan_time: float = 0
        self.scan_status: str = "idle"  # idle, scanning, done
        self.scan_progress: str = ""  # e.g. "Checking 5/120..."
        self.scan_total: int = 0
        self.scan_checked: int = 0
        self._scan_lock = threading.Lock()  # 守卫手动扫描不重复开线程
        self._scan_generation = 0  # 每次启动手动扫描 +1;旧扫描据此合作式让位

    # === Auto mode: full engine lifecycle ===

    def _reset_discovery_cache(self):
        """清空内存候选池,使下一轮扫描线程强制重新发现(_should_discover 转 True)。

        (重)启动引擎时调用。restart_all/start_all 复用同一 manager 实例,若不清空,
        4h 发现间隔内 _should_discover 恒为 False,「市场发现」永远停在上次结果——即
        用户实报的「点重启引擎但市场发现不刷新」。last_scan_time 保留供仪表盘扫描时效
        显示,清空 eligible_markets 一项即足以触发重新发现。
        """
        self.eligible_markets = []

    def start_all(self):
        """Start everything: wallet workers + recovery + auto scanner loop."""
        self._reset_discovery_cache()  # (重)启动强制重新发现,不复用旧候选池
        wallets = self.db.list_wallets()
        for w in wallets:
            if w["enabled"]:
                self.start_wallet(w["address"], w["encrypted_key"])

        self.startup_recovery()

        # Start auto scanner loop
        self._stop_event.clear()
        self._scanner_thread = threading.Thread(target=self._scanner_loop, daemon=True)
        self._scanner_thread.start()
        logger.info("Engine started: %d wallets + auto scanner", len(self.engines))

    def stop_all(self):
        """Stop everything: scanner + all wallet workers, cancel all buy orders."""
        self._stop_event.set()
        if self._scanner_thread:
            self._scanner_thread.join(timeout=30)
            self._scanner_thread = None

        for address in list(self.engines.keys()):
            self.stop_wallet(address)
        logger.info("Engine stopped")

    def restart_all(self):
        """Restart with fresh settings."""
        self.stop_all()
        self.start_all()

    # === Manual mode: step-by-step controls ===

    def start_monitors(self):
        """Start wallet workers (monitor only, no auto scanning)."""
        wallets = self.db.list_wallets()
        for w in wallets:
            if w["enabled"]:
                self.start_wallet(w["address"], w["encrypted_key"])

        self.startup_recovery()
        logger.info("Started %d wallet monitors", len(self.engines))

    def cancel_all_buy_orders(self):
        """Cancel all buy orders across all wallets."""
        for address, worker in self.engines.items():
            if worker.running:
                try:
                    worker._cancel_buy_orders()
                except Exception as e:
                    logger.error("Error cancelling orders for %s: %s", address, e)
        logger.info("Cancelled all buy orders across all wallets")

    def _ensure_scanner_api(self):
        """确保 _scanner_api 就绪:优先复用运行中 worker 的 API,否则用首个启用钱包新建。
        无可用钱包时保持 None。"""
        if self._scanner_api:
            return
        if self.engines:
            self._scanner_api = next(iter(self.engines.values())).api
            return
        wallets = self.db.list_wallets()
        enabled = [w for w in wallets if w["enabled"]]
        if not enabled:
            logger.error("No wallets available for scanning")
            return
        pk = decrypt(enabled[0]["encrypted_key"], self.encryption_key)
        self._scanner_api = PolymarketAPI(
            pk,
            signature_type=enabled[0].get("signature_type", 2),
            funder=enabled[0].get("funder") or None,
            proxy=enabled[0].get("proxy") or None,
        )
        logger.info("Created scanner API from wallet %s", enabled[0]["address"])

    def start_scan_async(self, force: bool = False) -> bool:
        """启动一次后台扫描,返回是否真的启动了。
        - 未在扫描:直接启动。
        - 已在扫描且 force=False:返回 False(防误触/双击开出互相踩的扫描)。
        - 已在扫描且 force=True:代际 +1,旧扫描在最近检查点合作式让位、结果作废,
          本轮用最新配置重来(前端「弹确认再重启」走这条)。
        同步把 scan_status 置 'scanning',使前端下一拍轮询立即可见。"""
        with self._scan_lock:
            if self.scan_status == "scanning" and not force:
                return False
            self._scan_generation += 1
            gen = self._scan_generation
            self.scan_status = "scanning"
        threading.Thread(target=self._run_scan, args=(gen,), daemon=True).start()
        return True

    def _run_scan(self, gen: int):
        """后台扫描线程体:跑 scan_markets,并兜底收敛 scan_status。被更新一轮接管
        (ScanSuperseded)时静默退出;无可用钱包早退时也不让 scan_status 卡在 'scanning'。"""
        try:
            self.scan_markets(gen)
        except ScanSuperseded:
            logger.info("扫描被新一轮接管,放弃本轮 gen=%s", gen)
        except Exception as e:
            logger.error("扫描线程异常: %s", e)
        finally:
            # 只有「我仍是当前代」才收敛状态;已被接管则由新线程负责。
            with self._scan_lock:
                if gen == self._scan_generation and self.scan_status == "scanning":
                    self.scan_status = "done"

    def scan_markets(self, gen: int = None):
        """Run a single scan to produce the eligible markets list.

        Updates scan_status/scan_progress in real-time for frontend polling.
        gen: 手动扫描的代际(用于合作式让位);None=自动模式/直接调用,不做让位。
        """
        self._ensure_scanner_api()
        if not self._scanner_api:
            return

        eligible = self._scan_with_status(gen=gen)

        # 被更新一轮接管时不落库(否则会用一份被作废的结果覆盖 DB);仅当仍是当前代才存。
        if gen is None or gen == self._scan_generation:
            self.db.save_eligible_markets(eligible)
            logger.info("Saved %d eligible markets to database", len(eligible))

    def _static_catalog(self) -> dict:
        """无任何计数快照时的兜底:纯静态品类清单(不联网),计数留空,列表照样秒显。"""
        return {
            "ready": False,
            "categories": [
                {"slug": c["slug"], "label": c["label"]} for c in CATEGORY_CATALOG
            ],
            "other_count": 0,
        }

    def _persist_catalog(self, payload: dict) -> dict:
        """把一份计数快照写入内存缓存 + 本地库(供跨重启秒显)。"""
        result = {
            "categories": payload.get("categories", []),
            "other_count": payload.get("other_count", 0),
            "ready": True,
            "updated_at": time.time(),
        }
        self._catalog_cache = result
        self._catalog_cache_ts = time.time()
        try:
            self.db.save_category_catalog(result)
        except Exception as e:
            logger.warning("保存品类计数快照失败: %s", e)
        return result

    def _compute_category_catalog(self) -> dict:
        """手动刷新:联网重算各品类计数并写回本地。无可用钱包时退回静态清单。"""
        self._ensure_scanner_api()
        if not self._scanner_api:
            return self._static_catalog()
        scanner = MarketScanner(self._scanner_api, self.db, "")
        with use_proxy(getattr(self._scanner_api, "proxy_url", None)):
            payload = scanner.category_counts(
                CATEGORY_CATALOG,
                max_pages=self.db.get_settings()["reward_scan_max_pages"],
            )
        return self._persist_catalog(payload)

    def category_catalog(self, refresh: bool = False) -> dict:
        """配置页勾选用:各品类市场数 + 「其他」数。
        默认读本地快照(内存 10 分钟,否则本地库)——秒开、跨重启不丢、不阻塞联网;
        计数在每次「扫描市场」后自动刷新。refresh=True(手动刷新按钮)才即时联网重算。
        从没扫描/刷新过时返回纯静态品类清单(无计数),列表仍立即可勾。"""
        if refresh:
            return self._compute_category_catalog()
        now = time.time()
        if self._catalog_cache and (now - self._catalog_cache_ts) < 600:
            return self._catalog_cache
        stored = self.db.get_category_catalog()
        if stored:
            self._catalog_cache = stored
            self._catalog_cache_ts = now
            return stored
        return self._static_catalog()

    def place_all_orders(self):
        """每钱包按自己模板从候选池精筛后下单。"""
        if not self.eligible_markets:
            logger.warning("No candidate pool to place orders on")
            return
        for address, worker in self.engines.items():
            if not worker.running:
                continue
            try:
                tmpl = self.db.get_template_for(address)
                scanner = MarketScanner(self._scanner_api, self.db, "")
                eligible = scanner.filter_for_template(
                    self.eligible_markets, tmpl, address
                )
                eligible.sort(
                    key=lambda m: float(m.get("market_competitiveness", 0) or 0)
                )
                worker.place_orders(eligible, cancel_dropouts=True)
            except Exception as e:
                logger.error("Error placing orders for %s: %s", address, e)

    def test_place_orders(self) -> dict:
        """Place up to 3 strategy-compliant test buys on the first enabled
        wallet, iterating eligible markets until 3 succeed.

        Does not require the monitor to be running: if the first enabled
        wallet already has a running worker it is reused (so fills get
        monitored and written to history); otherwise a transient API/worker
        is constructed just to place the orders (no monitor thread)."""
        if not self.eligible_markets:
            return {"ok": False, "message": "请先扫描市场"}

        wallet = next((w for w in self.db.list_wallets() if w["enabled"]), None)
        if wallet is None:
            return {"ok": False, "message": "没有启用的钱包"}

        address = wallet["address"]
        existing = self.engines.get(address)
        if existing and existing.running:
            worker = existing
        else:
            try:
                private_key = decrypt(wallet["encrypted_key"], self.encryption_key)
                funder = wallet.get("funder", "")
                api = PolymarketAPI(
                    private_key,
                    signature_type=wallet.get("signature_type", 2),
                    funder=funder or None,
                    proxy=wallet.get("proxy") or None,
                )
                settings = self.db.get_settings()
                worker = WalletWorker(api, self.db, address, settings)
            except Exception as e:
                logger.error("Error building API for test orders: %s", e)
                return {"ok": False, "message": f"测试挂单失败：{e}"}

        # eligible_markets 现为候选池(按市场、无 token_id/价格);必须先按该钱包
        # 模板精筛成逐 token 的可下单 eligible,否则 place_orders 取 token_id 会 KeyError。
        tmpl = self.db.get_template_for(address)
        scanner = MarketScanner(self._scanner_api, self.db, "")
        sorted_markets = scanner.filter_for_template(
            self.eligible_markets, tmpl, address
        )
        sorted_markets.sort(
            key=lambda m: float(m.get("market_competitiveness", 0) or 0)
        )

        try:
            worker.place_orders(sorted_markets, limit=3)
        except Exception as e:
            logger.error("Error placing test orders: %s", e)
            return {"ok": False, "message": f"测试挂单失败：{e}"}
        return {
            "ok": True,
            "message": "已对符合策略的市场提交最多 3 个测试买单，请到订单管理查看",
        }

    def start_wallet(self, address: str, encrypted_key: str = None):
        if address in self.engines and self.engines[address].running:
            return

        wallets = self.db.list_wallets()
        wallet = next((w for w in wallets if w["address"] == address), None)
        if not wallet:
            return
        encrypted_key = wallet["encrypted_key"]
        funder = wallet.get("funder", "")

        private_key = decrypt(encrypted_key, self.encryption_key)
        api = PolymarketAPI(
            private_key,
            signature_type=wallet.get("signature_type", 2),
            funder=funder or None,
            proxy=wallet.get("proxy") or None,
        )
        settings = self.db.get_settings()
        worker = WalletWorker(
            api,
            self.db,
            address,
            settings,
            on_reward_update=self.update_market_reward,
        )
        self.engines[address] = worker
        worker.start()

        # Use first wallet's API for scanning (shared, avoids extra auth)
        if self._scanner_api is None:
            self._scanner_api = api

    def stop_wallet(self, address: str):
        worker = self.engines.pop(address, None)
        if worker:
            worker.stop()

    def _scanner_loop(self):
        """快节奏循环:按 discovery_interval 发现新市场(慢),每轮刷簿+下单(快)。"""
        settings = self.db.get_settings()
        place_interval = settings["scan_interval_sec"]

        while not self._stop_event.is_set():
            if self._scanner_api and self.engines:
                try:
                    if self._should_discover(time.time()):
                        self._discover()
                except Exception as e:
                    logger.error("Discovery error: %s", e)
                # 发现失败也不拖垮下单:_scan_with_status 失败保留上一份缓存池,
                # 本轮仍用它刷簿下单;首启失败则池空,_place_round 空池 guard 跳过。
                try:
                    self._place_round()
                except Exception as e:
                    logger.error("Place round error: %s", e)

            self._stop_event.wait(timeout=place_interval)

    def _active_templates(self) -> list[dict]:
        """所有启用钱包绑定模板(按采集器实际用到的维度去重),供采集器算并集/交集。"""
        from engine.tiers import enabled_sizes

        try:
            wallets = self.db.list_wallets()
        except Exception:
            wallets = []
        seen = {}
        for w in wallets:
            if not w.get("enabled"):
                continue
            tmpl = self.db.get_template_for(w["address"])
            # 去重键须含采集器实际用到的每个维度:品类包含集 + 是否含其他 + 奖励下限
            # (决定预筛 min_floor) + 结算窗口 + 档位 sizes + 新市场开关/小时数/保护品类
            # (后四者决定发现阶段的并集门控:窗口/档位/新市场门槛不同的模板不能被去重
            # 成一个,否则另一个的门槛没进并集就会误剔)。
            key = (
                tuple(sorted(tmpl.get("included_categories", []) or [])),
                bool(tmpl.get("include_other", False)),
                tmpl.get("min_reward_usd", 0),
                tmpl.get("min_settlement_days"),
                tmpl.get("max_settlement_days"),
                tuple(sorted(enabled_sizes(tmpl.get("size_tiers") or []))),
                bool(tmpl.get("skip_new_markets", False)),
                tmpl.get("new_market_hours"),
                tuple(sorted(tmpl.get("skip_new_categories", []) or [])),
                bool(tmpl.get("skip_new_other", False)),
            )
            seen[key] = tmpl
        if seen:
            return list(seen.values())
        return [self.db.get_template(self.db.get_default_template_id())]

    def _scan_with_status(self, gen: int = None, skip_orderbook: bool = False) -> list:
        """Run one scan, reporting scan_status/progress; shared by manual and
        auto paths. On success sets eligible_markets/last_scan_time and
        scan_status='done' and returns the eligible list. On failure resets
        scan_status to 'done' (never left 'scanning') WITHOUT touching
        last_scan_time (a failed round did not complete), then re-raises.

        gen: 手动扫描的代际。只有「仍是当前代」(gen 为 None 或等于 _scan_generation)
        才动共享状态(进度/eligible_markets/落库);被更新一轮接管后本轮不再改任何共享
        状态,并向采集器传 cancel() 令其在最近检查点抛 ScanSuperseded 让位。"""
        import time as _time

        def is_current():
            return gen is None or gen == self._scan_generation

        prev_eligible = self.eligible_markets
        if is_current():
            self.scan_status = "scanning"
            self.scan_progress = "Starting..."
            self.scan_checked = 0
            self.scan_total = 0
            self.eligible_markets = []

        def on_progress(checked, total, message):
            if is_current():
                self.scan_checked = checked
                self.scan_total = total
                self.scan_progress = message

        def on_found(entry):
            if is_current():
                self.eligible_markets.append(entry)

        cancel = (lambda: gen != self._scan_generation) if gen is not None else None

        try:
            scanner = MarketScanner(self._scanner_api, self.db, "")
            templates = self._active_templates()
            # 采集器的静态 rewards/gamma 发现调用走「采集所用钱包」的代理(订单簿抓取
            # 经 _scanner_api 实例方法已各自走代理)。
            with use_proxy(getattr(self._scanner_api, "proxy_url", None)):
                candidate_pool = scanner.fetch_candidates(
                    templates,
                    on_progress=on_progress,
                    on_found=on_found,
                    skip_orderbook=skip_orderbook,
                    cancel=cancel,
                    max_pages=self.db.get_settings()["reward_scan_max_pages"],
                )
        except ScanSuperseded:
            raise  # 被接管:不碰共享状态,交给 _run_scan 静默处理
        except Exception:
            if is_current():
                self.eligible_markets = prev_eligible  # don't blank on failure
                self.scan_status = "done"  # not 'scanning': progress bar won't stick
            raise
        if not is_current():
            return candidate_pool  # 被接管:不提交结果、不改状态
        self.eligible_markets = candidate_pool
        self.last_scan_time = _time.time()
        self.scan_status = "done"
        self.scan_progress = f"Done: {len(candidate_pool)} candidates"
        logger.info("Scanner found %d candidates", len(candidate_pool))
        # 扫描顺带刷新品类计数快照(复用发现阶段的 tag 数据,不额外联网)
        catalog = getattr(scanner, "last_catalog", None)
        if catalog:
            self._persist_catalog(catalog)
        return candidate_pool

    def update_market_reward(self, condition_id: str, reward: float):
        """实时复查到的每日奖励写回候选池(内存+DB),下单轮 prefilter 立刻用新值。

        监控 Step3 撤掉「奖励跌破门槛」的买单后调用。不写回的话,30 秒后的下单轮会
        拿最长 4 小时前的快照把单挂回来、5 秒后监控再撤,来回打架。只写 market_reward
        就足以让市场跌出 eligible(prefilter 判的是 or 条件),daily_reward 是展示键。
        """
        pool = self.eligible_markets  # 先取本地引用:扫描会整体重绑该属性
        for m in pool:
            if m.get("condition_id") == condition_id:
                m["market_reward"] = reward
                m["daily_reward"] = reward
        self.db.update_eligible_reward(condition_id, reward)

    def _should_discover(self, now: float) -> bool:
        """无缓存池、或距上次发现 >= discovery_interval -> 该重新发现。"""
        interval = self.db.get_settings()["discovery_interval_sec"]
        return (not self.eligible_markets) or (now - self.last_scan_time) >= interval

    def _discover(self):
        """慢节奏:全量奖励发现(不抓订单簿),刷新缓存候选池 + 持久化。"""
        self._scan_with_status(skip_orderbook=True)

    def _place_round(self):
        """快节奏:无簿门槛精筛 -> 只给幸存市场刷订单簿 -> 每钱包下单(跌出撤单)。空池跳过。

        刷簿是整轮最贵的一步(每市场每 token 一次网络请求),所以先用不需要订单簿的门槛
        (品类/奖励/结算/冷却/档位/价带)把候选池筛一遍,只抓真正可能下单的那几十个市场的
        簿——整池 300+ 全刷时,绝大多数簿抓回来只是被 filter 当场丢掉。各钱包幸存集取并集
        刷一次(共有市场不重复抓)。

        先把候选池快照到本地 pool:并发的手动扫描会重绑 self.eligible_markets
        (扫描中先清空再重建),本轮持快照不受影响,免得中途看到空池误跳过。"""
        pool = self.eligible_markets
        if not pool:
            return
        scanner = MarketScanner(self._scanner_api, self.db, "")

        survivors, needed = {}, {}
        for address, worker in self.engines.items():
            if not worker.running:
                continue
            try:
                tmpl = self.db.get_template_for(address)
                subset = scanner.prefilter_for_template(pool, tmpl, address)
            except Exception as e:
                logger.error("Prefilter failed for wallet %s: %s", address, e)
                continue
            survivors[address] = (tmpl, subset)
            for market in subset:
                needed.setdefault(market.get("condition_id", ""), market)
        if not needed:
            logger.info("本轮无市场通过无簿门槛,跳过刷订单簿")
        scanner.refresh_orderbooks(list(needed.values()))

        for address, (tmpl, subset) in survivors.items():
            worker = self.engines.get(address)
            if not worker or not worker.running:
                continue
            try:
                # subset 已过无簿门槛(prefilter),直接跑簿门槛即可,不重复走 prefilter。
                eligible = []
                for market in subset:
                    eligible.extend(scanner.book_eligible(market, tmpl))
                eligible.sort(
                    key=lambda m: float(m.get("market_competitiveness", 0) or 0)
                )
                worker.place_orders(eligible, cancel_dropouts=True)
            except Exception as e:
                logger.error("Error distributing to wallet %s: %s", address, e)

    def startup_recovery(self):
        """API-driven recovery: seed each monitor's trade watermark from DB history.

        Offline fills are caught next tick by get_trades(after=watermark) +
        id-dedup. Stale resting orders are reconciled by the monitor's
        compliance step. No DB orders/positions reconciliation.
        """
        for worker in self.engines.values():
            try:
                worker.monitor.init_watermark()
            except Exception as e:
                logger.error(
                    "Watermark init failed for %s: %s", worker.wallet_address, e
                )

    def get_status(self) -> dict:
        """Get status of all engines."""
        return {
            "engines": {
                addr: {"running": eng.running} for addr, eng in self.engines.items()
            }
        }
