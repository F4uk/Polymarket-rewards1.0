"""engine/monitor.py — API-driven fill detection, stop-loss, strategy compliance."""

import logging
import time
from py_clob_client_v2.clob_types import TradeParams
from engine.fills import select_new_buy_fills, extract_fills
from engine.take_profit import (
    plan_take_profit,
    position_cost_with_lots,
    remaining_fifo_lots,
    describe_cost_basis,
    plan_exit,
    market_fill_price,
    effective_theta_stop,
    ceil_to_tick,
)
from engine.exit_router import executable_limit_price, executable_value, route_residual
from engine.eligibility import recheck_resting_buy
from engine.laddering import has_cliff_below
from engine.resolution import in_resolution
from engine.liquidation import plan_liquidation
from engine.strategy import reward_price_range
from engine.rewards import extract_max_spread, extract_daily_rate
from engine import monitor_status
from engine.merge import ordinary_binary_plan
from engine.positions import condition_key, positions_by_condition
from engine.take_profit import consume_fifo_lots
from engine.inventory_exit import InventoryExitEngine
from api.ctf import RelayerUnavailable, Type3MergeClient
from api.polymarket_api import OrderRejected
from api.proxy import parallel_map
from models.database import ActiveMergeOperationExists

logger = logging.getLogger(__name__)

# 低余额清仓成功后的冷却秒数:市价卖的到手余额链上结算有滞后,冷却内不再清仓,
# 防下个 tick(默认 5s)读到未结算的旧低余额而重扫、过卖不该卖的仓。
LIQUIDATE_COOLDOWN_SEC = 60

# Step3 预取并发上限。发现阶段用 4(实测奖励端点/代理娇气);Step3 只打盘口和奖励两个
# 轻接口、不跑分页,略高一档。⚠️这不是"单个代理只扛这个数":兼发现的钱包会在同一个
# 代理上同时背发现的 4 路 + Step3 的 6 路,峰值约 10 路并发。要回退并发化只改这一个常量。
_STEP3_MAX_WORKERS = 6

# 离场预取并发上限。与 Step3 取同一个数,但**理由不同**:离场的成交拨走 get_trades,
# 那是会自动翻页的接口(while cursor != END_CURSOR),不是"轻接口不翻页"。之所以 6 也够,
# 是因为单个市场的自有成交条数很少、通常一页就完;真遇到成交极多的市场,这一拨会明显变慢,
# 那时该往下调而不是往上加。要回退并发化只改这一个常量。
_EXIT_MAX_WORKERS = 6
# 缓存里 None 表示「取数失败」,所以「没这个键」需要另一个哨兵,不能用 None 表达。
_MISSING = object()
_SCORING_TTL_SEC = 60
_FOK_RECONCILE_GRACE_SEC = 30


class OrderMonitor:
    def __init__(
        self,
        api,
        db,
        wallet_address: str,
        on_reward_update=None,
        condition_lock=None,
        encryption_key: bytes = None,
    ):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        # Login-derived key for decrypting stored Relayer credentials.
        # None keeps legacy ENV-only behavior (and all existing tests).
        self.encryption_key = encryption_key
        # 实时奖励写回候选池的回调(manager 注入);None=不写回(测试/临时下单 worker)。
        self.on_reward_update = on_reward_update
        # WalletWorker owns the lock registry because placement and monitoring
        # are concurrent. A standalone monitor in tests can still operate with
        # no lock provider.
        self._condition_lock = condition_lock
        # Dedup processed buy fills by (trade_id, order_id).
        self._seen_fill_keys: set = set()
        # Watermark: lower bound for get_trades(after=) — bounds fetch size;
        # real idempotency is _seen_fill_keys.
        self._after_ts: float = 0.0
        # init_watermark priming 失败(启动瞬间网络抖)时置 True:check_buy_orders 每轮
        # 重试 priming,建好去重集前不处理成交,避免重放整批历史成交。
        self._prime_pending: bool = False
        # condition_id -> ((max_spread, daily_rate), fetched_at) TTL cache for Step 3.
        self._rewards_cache: dict = {}
        self._status_rows: list = []
        self._tick_ts: float = 0.0
        self._cost_cache: dict = {}  # asset_id -> 加权成本 or None(每 tick 重置)
        # 低余额清仓:成功清仓后设冷却(让市价卖的到手余额链上结算),冷却内不再清仓 ->
        # 防跨 tick 过卖(余额结算滞后时下个 tick 仍读到旧低余额会重扫、误卖不该卖的仓)。
        self._liquidate_cooldown_until: float = 0.0
        # 本 tick 刚被 check_low_balance 市价清掉的 asset:check_exit 同 tick 跳过它们
        # (Data API /positions 滞后仍显示满仓,否则会对已卖仓挂卖单被拒、报假「裸奔」)。
        self._just_dumped: set = set()
        # 本 tick 已被撤掉的卖单 id(begin_status_tick 重置)。步骤 2-4 共用 tick 开头那份
        # 挂单快照,快照里仍有这些单,但它们已经不在挂了:不滤掉就会让 check_exit 以为
        # 「已有一张挂在目标价的卖单」而返回 keep -> 持仓无卖单保护、状态行还谎报「保持」
        # (低余额清仓被拒时必现:撤单已成功、市价卖被拒,仓没了卖单也没被卖掉)。
        self._cancelled_sell_ids: set = set()
        # 本 tick 的离场取数预取表(begin_status_tick 重置)。
        # condition_id -> get_trades 原始返回;None = 本轮取数失败
        self._trades_by_cid: dict = {}
        # asset_id -> get_orderbook 原始返回;None = 本轮取数失败
        self._book_cache: dict = {}
        # 本轮 check_exit 判定的持仓数,只给 tick 耗时日志用,不进任何判定。
        # None = 本轮还没判定/早退了(日志渲染成 ?),别拿上一轮的数冒充这一轮。
        self.last_position_count = 0
        self._scoring_checked_at = 0.0
        # A CLOB response can arrive before the Data API reflects an accepted
        # FOK. Keep that narrow condition out of a direct B0 exit until the
        # persisted planned merge is reconciled (or its short grace expires).
        self._pending_fok_until: dict[str, float] = {}
        # V2 post-fill inventory engine (lazy, one per monitor).
        self._exit_engine: InventoryExitEngine | None = None

    def _fast_exit_enabled(self) -> bool:
        try:
            return bool(
                self.db.get_template_for(self.wallet_address).get(
                    "fast_exit_enabled", True
                )
            )
        except Exception:
            return False

    def exit_engine(self) -> InventoryExitEngine | None:
        """Public accessor so placement (manager) shares the same engine."""
        try:
            return self._inventory_exit()
        except Exception:
            return None

    def _inventory_exit(self) -> InventoryExitEngine:
        if self._exit_engine is None:
            self._exit_engine = InventoryExitEngine(
                self.api,
                self.db,
                self.wallet_address,
                encryption_key=self.encryption_key,
                condition_lock=self._condition_lock,
                cost_provider=self._cost_lots,
                book_provider=self._engine_book,
                trades_provider=self._engine_trades,
                status_add=self._status_add,
                record_action=self._record_action,
            )
        return self._exit_engine

    def _engine_book(self, asset_id: str):
        """Orderbook for the exit engine: per-tick prefetch cache first."""
        if asset_id in self._book_cache:
            return self._book_cache[asset_id]
        try:
            return self.api.get_orderbook(asset_id)
        except Exception:
            return None

    def _engine_trades(self, condition_id: str):
        """get_trades for the exit engine: per-tick prefetch cache first."""
        if condition_id in self._trades_by_cid:
            return self._trades_by_cid[condition_id]
        try:
            return self.api.get_trades(TradeParams(market=condition_id))
        except Exception:
            return None

    def begin_status_tick(self) -> None:
        self._status_rows = []
        self._tick_ts = time.time()
        self._cost_cache = {}
        self._just_dumped = set()
        self._cancelled_sell_ids = set()
        self._trades_by_cid = {}
        self._book_cache = {}

    def _status_add(self, **fields) -> None:
        try:
            row = {"ts": self._tick_ts, "wallet": self.wallet_address}
            row.update(fields)
            self._status_rows.append(row)
        except Exception as e:  # never break Step1/2/3
            logger.warning("status_add failed: %s", e)

    def publish_status(self) -> None:
        try:
            monitor_status.set_snapshot(
                self.wallet_address, self._status_rows, self._tick_ts
            )
        except Exception as e:
            logger.warning("publish_status failed: %s", e)

    def _record_action(
        self, market_id, action_type, side, price, size, reason, price_basis
    ) -> None:
        """Persist one order-mutating action. Never breaks Step1/2/3."""
        try:
            self.db.record_action(
                wallet=self.wallet_address,
                market_id=market_id,
                action_type=action_type,
                side=side,
                price=price,
                size=size,
                reason=reason,
                price_basis=price_basis,
            )
        except Exception as e:
            logger.warning("record_action failed: %s", e)

    def _try_condition_lock(self, condition_id):
        """Claim one condition for a CLOB/relayer mutation, or return None."""
        if not self._condition_lock:
            return None
        lock = self._condition_lock(condition_id)
        return lock if lock.acquire(blocking=False) else False

    @staticmethod
    def _release_condition_lock(lock):
        if lock:
            lock.release()

    def _notify_reward_update(self, condition_id: str, reward: float) -> None:
        """把实时每日奖励写回候选池。纯筛选/展示用,绝不能中断撤单流程。"""
        if not self.on_reward_update:
            return
        try:
            self.on_reward_update(condition_id, reward)
        except Exception as e:
            logger.warning("奖励写回失败 %s: %s", condition_id, e)

    def init_watermark(self):
        """Seed watermark AND prime the seen-fill-keys set on startup recovery.

        The watermark is the latest recorded trade/action ts — DB created_at is
        local record time while trade match_time is exchange time, so it is only
        a **conservative lower bound**: `get_trades(after=watermark)` deliberately
        re-fetches a recent window and relies on `(trade_id, order_id)` dedup.
        Actions are included so that clearing the trades table does not reset it
        to 0 (which would refetch all history and re-cooldown old markets).

        BUT `_seen_fill_keys` lives only in memory and is empty after a restart,
        so that dedup used to NOT survive a restart: the first ticks re-processed
        already-handled fills, re-recording `cancel_remainder` and re-setting
        cooldown for markets whose position was long closed (real 2026-05-29
        observation). So here we also **prime `_seen_fill_keys` with every fill
        already on record** — only fills that occur AFTER startup then trigger
        `_handle_fill`. (Offline fills are already protected position-side by the
        position-driven take-profit, independent of Step 1.)
        """
        rows = self.db.get_trade_history(self.wallet_address)
        actions = self.db.get_actions(self.wallet_address)
        times = [r.get("created_at", 0) or 0 for r in rows]
        times += [a.get("created_at", 0) or 0 for a in actions]
        self._after_ts = max(times) if times else 0.0
        # Prime dedup with ALL historical fills (get_trades 无 after,稳拿全部)so a
        # restart never re-handles pre-startup fills. after 时间过滤不可靠——水位在今天
        # 时 tick 的 after 仍可能把 6 月的历史成交捞回,若 priming 也用 after=水位 就会
        # 漏灌(0 条)、tick 照样重放(cancel_remainder 洪水,2026-07-04 实盘)。失败
        # (启动网络抖)则置 pending,check_buy_orders 重试、建好前不处理成交。
        if not self._prime_seen_keys():
            self._prime_pending = True

    def _prime_seen_keys(self) -> bool:
        """预灌去重集 + 推进水位。用 get_trades(**无 after**) 可靠拉回全部历史成交——
        Polymarket 的 after 时间过滤不可靠(实测:水位在今天时,tick 的 after 仍可能把
        6 月的历史成交捞回来,类似已知的 asset_id 过滤失效),不能靠它保证覆盖;只有无
        after 才稳拿全部。把全部成交灌进 _seen_fill_keys(dedup 才是真幂等),并把水位
        推进过其 match_time。成功返回 True 并清 _prime_pending;失败返回 False。"""
        funder = self._funder()
        try:
            trades = self.api.get_trades(TradeParams(maker_address=funder))
            for ev in select_new_buy_fills(trades, funder, set()):
                self._seen_fill_keys.add((ev.get("trade_id"), ev.get("order_id")))
                self._after_ts = max(self._after_ts, float(ev.get("ts", 0) or 0))
        except Exception as e:
            logger.warning("seen-fill priming 失败(下一轮重试): %s", e)
            return False
        self._prime_pending = False
        return True

    def _funder(self) -> str:
        """Proxy/funder address — used for get_trades maker filter and Data API."""
        return self.api.get_funder()

    def _cost_lots(self, asset_id: str, size: float, condition_id: str):
        """该持仓的加权成本 + 剩余逐笔构成(本 tick 缓存)。由 CLOB get_trades 的真实
        成交(买入∪卖出,maker∪taker)按时间回放、FIFO 对冲重建;重建持仓与 size 对不
        上(成交流滞后/外部转入)-> (None, [])。

        用 market=condition_id 过滤拉成交,再由 extract_fills 客户端按 asset 过滤:
        Polymarket /trades 的服务端 asset_id 过滤失效(同一 asset 明明有成交却返回空,
        2026-05-29 真实事故 -> 成本算不出、全仓裸奔),而 market(conditionId)过滤可用,
        且会一并带回我们在该市场的 taker 成交(止损市价单等),正是 extract_fills 所需。

        盘口/成交优先读本 tick 的预取表(_exit_prefetch 写入),表里没有该键才自取。"""
        if asset_id in self._cost_cache:
            return self._cost_cache[asset_id]
        funder = self._funder()
        trades = self._trades_by_cid.get(condition_id, _MISSING)
        if trades is _MISSING or trades is None:
            # _MISSING = 没走预取的调用点(_resolution_dump 等),照旧自取。
            # None = 预取那一路取数失败(已在预取里 WARNING 过)——同样自取重试一次,
            # 不能直接判成本未知:成本未知 = 整仓跳过 + ⚠️裸奔,等于把一次网络抖动
            # 翻译成一段无保护窗口(2026-07-29 实盘 810 次裸奔里 789 次紧跟一次预取
            # 失败)。自取走的是新连接,瞬时抖动多半能过;真取不到才认成本未知。
            try:
                trades = self.api.get_trades(TradeParams(market=condition_id))
            except Exception as e:
                logger.warning(
                    "get_trades(market=%s) for cost failed: %s", condition_id, e
                )
                self._cost_cache[asset_id] = (None, [])
                return None, []
        if trades is None:
            self._cost_cache[asset_id] = (None, [])
            return None, []
        fills = extract_fills(trades, funder, asset_id)
        merge_events = []
        try:
            for operation in self.db.get_confirmed_merges(self.wallet_address):
                consumed_lots = operation.get("consumed_lots") or []
                consumed = next(
                    (row for row in consumed_lots if row.get("asset_id") == asset_id),
                    None,
                )
                qty = sum(
                    float(lot.get("take", 0) or 0)
                    for lot in (consumed or {}).get("lots", [])
                )
                if qty > 0:
                    merge_events.append(
                        {
                            "side": "MERGE",
                            "size": qty,
                            "ts": operation.get("confirmed_at") or operation.get("created_at") or 0,
                        }
                    )
        except Exception as exc:
            logger.warning("merge cost ledger unavailable for %s: %s", asset_id, exc)
        result = position_cost_with_lots(fills, size, merge_events)
        self._cost_cache[asset_id] = result
        return result

    # --- Step 1: fills via get_trades (flatten maker_orders) ---
    def check_buy_orders(self):
        # 去重集未就绪(启动 priming 失败、网络还没恢复):先补 priming;仍失败就本轮
        # 不处理成交,避免把整批历史成交当"新成交"重放(cancel_remainder 洪水)。
        if self._prime_pending and not self._prime_seen_keys():
            return
        funder = self._funder()
        try:
            params = TradeParams(
                maker_address=funder,
                after=(str(int(self._after_ts)) if self._after_ts else None),
            )
            trades = self.api.get_trades(params)
        except Exception as e:
            logger.error("get_trades failed for %s: %s", self.wallet_address, e)
            return
        fills = select_new_buy_fills(trades, funder, self._seen_fill_keys)
        # 只对**当前仍在挂**的买单撤余量。成交回放里那些几个月前早已成交/消失的单根本
        # 不在挂单里,撤它们只会报 already-canceled 刷屏(get_trades 去重在并发下不可靠,
        # 不能只靠它)。「是否仍在挂」才是「撤剩余未成交量」的真实前提,且 get_open_orders
        # 可靠——这是根治重放洪水的护栏,不依赖 get_trades/水位。
        # **这里坚持自取,不吃 tick 开头的共用快照**:快照比它要判定的这批成交旧一个往返,
        # 「成交之后这单还在不在挂」正是护栏本身的判据,拿旧一拍的挂单去判会把护栏磨钝。
        # 代价也几乎为零——这次取数本来就只在真检测到成交时才发生(低频路径)。
        open_ids: set = set()
        if fills:
            try:
                open_ids = {o.get("id") for o in self.api.get_open_orders()}
            except Exception as e:
                logger.warning(
                    "get_open_orders failed in Step 1 (本轮跳过撤余量): %s", e
                )
        cancelled_orders: set = set()
        for ev in fills:
            try:
                self._handle_fill(ev, cancelled_orders, open_ids)
            except Exception as e:
                logger.error(
                    "Error handling fill %s/%s: %s",
                    ev.get("trade_id"),
                    ev.get("order_id"),
                    e,
                )
            finally:
                self._seen_fill_keys.add((ev.get("trade_id"), ev.get("order_id")))
                self._after_ts = max(self._after_ts, float(ev.get("ts", 0) or 0))

    def _handle_fill(self, ev: dict, cancelled_orders: set, open_ids: set):
        size = float(ev.get("size", 0) or 0)  # real data is fractional
        price = float(ev.get("price", 0) or 0)
        market_id = ev.get("market", "")
        order_id = ev.get("order_id")
        if size <= 0:
            return
        # No trades-table write: buy "history" is the live Data API position.
        # 离场卖单由 check_exit() 的两段式逻辑(plan_exit)按 get_trades 加权成本维护。
        # Step 1 only sets the cooldown and cancels the buy's remainder.
        # 「上次活跃」纯展示,写失败不影响成交处理。
        try:
            self.db.touch_wallet_active(self.wallet_address)
        except Exception as e:
            logger.warning("touch_wallet_active failed %s: %s", self.wallet_address, e)
        self.db.set_side_pause(
            self.wallet_address,
            market_id,
            ev.get("asset_id", ""),
            self.db.get_settings()["cooldown_minutes"],
        )
        if order_id and order_id in open_ids and order_id not in cancelled_orders:
            try:
                self.api.cancel_orders([order_id])
                cancelled_orders.add(order_id)
                self._record_action(
                    market_id=market_id,
                    action_type="cancel_remainder",
                    side="-",
                    price=-1,
                    size=size,  # fill size; remaining unfilled qty not known here
                    reason="该买单已成交，撤销同一买单剩余未成交量，避免超买",
                    price_basis=f"撤 order_id={order_id}；撤单操作无价格",
                )
            except Exception as e:
                logger.warning("Cancel remainder of %s failed: %s", order_id, e)
        self._status_add(
            market=market_id,
            side="买入",
            price=f"{price:.4f}",
            size=str(size),
            matched=str(size),
            stage="Step1",
            action="成交→撤余单",
            detail=f"成交{size}，止盈由持仓维护",
        )
        if self._fast_exit_enabled():
            try:
                self._inventory_exit().on_reward_fill(ev)
            except Exception as e:
                logger.error("inventory exit cycle create/join failed %s: %s", market_id, e)

    def check_inventory_exit(self, open_orders=None):
        """V2 Inventory Exit Engine pass (runs after check_merges).

        In V2 mode this step owns all post-fill inventory: complete sets are
        Merge-first (check_merges), one-sided residual gets the immediate
        FOK+Merge / bounded maker window / protected direct route decision.
        """
        if not self._fast_exit_enabled():
            return
        if open_orders is None:
            try:
                open_orders = self.api.get_open_orders()
            except Exception as e:
                logger.error(
                    "get_open_orders failed (skip inventory exit): %s", e
                )
                return
        try:
            positions = self.api.get_user_positions(self._funder())
        except Exception as e:
            logger.warning("Data API positions failed (skip inventory exit): %s", e)
            return
        try:
            self._exit_prefetch(
                [
                    p
                    for p in positions
                    if float(p.get("size", 0) or 0) > 0
                ]
            )
        except Exception as e:
            logger.warning("[库存退出] 预取失败(退回逐仓自取): %s", e)
        try:
            self._inventory_exit().run_tick(open_orders=open_orders, positions=positions)
        except Exception as e:
            logger.exception("inventory exit tick crashed: %s", e)

    def _merge_client(self):
        if int(getattr(self.api, "signature_type", -1)) != 3:
            raise RelayerUnavailable("automatic Merge requires a Type3 Deposit Wallet")
        from api.relayer_config import (
            RELAYER_NOT_CONFIGURED,
            load_relayer_runtime_config,
        )

        config = load_relayer_runtime_config(self.db, self.encryption_key)
        if config is None:
            raise RelayerUnavailable(
                "Relayer credentials are not configured",
                reason=RELAYER_NOT_CONFIGURED,
            )
        return Type3MergeClient(self.api.private_key, relayer_config=config)

    @staticmethod
    def _relayer_details(response):
        return (
            getattr(response, "transaction_id", "") or "",
            getattr(response, "transaction_hash", "") or getattr(response, "hash", "") or "",
        )

    @staticmethod
    def _transaction_state(payload):
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        return str((payload or {}).get("state", ""))

    def _quarantine_duplicate_active_merges(self, operations):
        """Persist a fail-closed marker for ambiguous legacy active rows."""
        active_by_condition: dict[str, list[dict]] = {}
        for operation in operations or []:
            active_by_condition.setdefault(
                condition_key(operation.get("condition_id", "")), []
            ).append(operation)
        duplicate_conditions = {
            cid
            for cid, grouped in active_by_condition.items()
            if cid and len(grouped) != 1
        }
        for cid in duplicate_conditions:
            logger.error(
                "multiple active Merge rows for condition %s; automatic submission disabled",
                cid,
            )
            # Persist quarantine *before* polling any submitted sibling.  If
            # that sibling confirms in this tick, the remaining planned row
            # must not suddenly look like a unique operation and dispatch.
            for duplicate in active_by_condition[cid]:
                if duplicate.get("status") == "planned" and not str(
                    duplicate.get("error", "")
                ).startswith("DUPLICATE_ACTIVE_QUARANTINE:"):
                    self.db.update_merge_operation(
                        duplicate["id"],
                        "planned",
                        error="DUPLICATE_ACTIVE_QUARANTINE: legacy active rows require manual reconciliation",
                    )
        return duplicate_conditions

    def _confirm_unresolved_merges(self, client, operations=None):
        if operations is None:
            operations = self.db.get_unresolved_merges(self.wallet_address)
        for operation in operations:
            if operation.get("status") != "submitted" or not operation.get("relayer_id"):
                continue
            try:
                state_payload = client.get_transaction(operation["relayer_id"])
                state = self._transaction_state(state_payload)
            except Exception as exc:
                logger.warning("merge status check %s failed: %s", operation["id"], exc)
                continue
            if state == "STATE_CONFIRMED":
                # Relayer confirmation is the funds-moving terminal boundary.
                # Mark it before optional FIFO accounting so a bookkeeping
                # retry can never masquerade as an in-flight transaction.
                self.db.update_merge_operation(
                    operation["id"],
                    "confirmed",
                    error="FIFO_LEDGER_PENDING: Relayer confirmed; reconciliation pending",
                )
                self._pending_fok_until.pop(
                    condition_key(operation.get("condition_id", "")), None
                )
                self._confirm_merge_operation(operation)
            elif state in ("STATE_FAILED", "STATE_INVALID"):
                self.db.update_merge_operation(
                    operation["id"], "failed", error=f"Relayer state {state}"
                )
                self._pending_fok_until.pop(
                    condition_key(operation.get("condition_id", "")), None
                )
                self._record_action(
                    operation["condition_id"], "merge_failed", "-", -1, operation["requested_qty"],
                    f"Merge failed: {state}", "Relayer transaction status",
                )

    def _retry_confirmed_merge_ledgers(self):
        """Retry accounting only; confirmed operations never re-enter funds flow."""
        for operation in self.db.get_confirmed_pending_merges(self.wallet_address):
            self._confirm_merge_operation(operation)

    def _reconcile_confirmed_merge_inventory(self):
        """Clear post-confirmation barriers only after consumption is visible.

        A submitted Merge is terminal at Relayer confirmation, so this barrier
        must not block residual SELLs.  It only prevents stale Data API rows
        from being interpreted as a brand-new complete set.
        """
        barriers = self.db.get_merge_inventory_barriers(self.wallet_address)
        if not barriers:
            return
        try:
            positions = self.api.get_user_positions(self._funder())
        except Exception as exc:
            logger.warning("confirmed Merge inventory reconciliation failed: %s", exc)
            return
        by_asset: dict[str, float] = {}
        for position in positions or []:
            try:
                by_asset[str(position.get("asset", ""))] = float(
                    position.get("size", 0) or 0
                )
            except (TypeError, ValueError):
                continue
        for operation in barriers:
            requested = float(operation.get("requested_qty", 0) or 0)
            visible_pair = min(
                by_asset.get(str(operation.get("yes_asset_id", "")), 0.0),
                by_asset.get(str(operation.get("no_asset_id", "")), 0.0),
            )
            # Immediately before submission both legs were >= requested.
            # Seeing either leg below that amount proves the Merge inventory
            # mutation has reached the Data API (ordinary V1 always merges the
            # entire smaller leg).
            if visible_pair < requested - 1e-9:
                self.db.mark_merge_inventory_reconciled(operation["id"])

    def _reconcile_planned_merges(self, client, condition_lock):
        """Turn durable planned operations into submissions only after inventory is visible.

        This covers a process interruption between persisting ``planned`` and
        relayer submission, plus the deliberate short wait after an accepted
        emergency FOK.  No quantity is sent merely because a FOK was accepted.
        """
        now = time.time()
        tmpl = self.db.get_template_for(self.wallet_address)
        minimum = float(tmpl.get("merge_min_shares", 1) or 0)
        unresolved = self.db.get_unresolved_merges(self.wallet_address)
        duplicate_conditions = self._quarantine_duplicate_active_merges(unresolved)
        for operation in unresolved:
            if operation.get("status") != "planned":
                continue
            if str(operation.get("error", "")).startswith(
                "DUPLICATE_ACTIVE_QUARANTINE:"
            ):
                logger.error(
                    "merge %s is quarantined after duplicate active rows; automatic submission is disabled",
                    operation.get("id"),
                )
                continue
            if str(operation.get("error", "")).startswith("relayer submission indeterminate"):
                # The server may have received the signed batch despite the
                # caller losing its response. There is no safe transaction id
                # to poll, so this must not become an automatic duplicate.
                logger.error(
                    "merge %s has indeterminate relayer submission; automatic retry is disabled",
                    operation.get("id"),
                )
                continue
            cid = operation.get("condition_id", "")
            if not cid:
                self.db.update_merge_operation(operation["id"], "failed", error="planned merge missing condition")
                continue
            # A legacy database can predate the partial unique index.  Locks
            # only serialize duplicate rows; they do not make either row the
            # rightful owner.  Fail closed instead of dispatching twice.
            if condition_key(cid) in duplicate_conditions:
                continue
            lock = condition_lock(cid)
            if not lock.acquire(blocking=False):
                continue
            try:
                try:
                    positions = self.api.get_user_positions(self._funder())
                    plan = ordinary_binary_plan(cid, positions, minimum)
                except Exception as exc:
                    logger.warning("planned merge inventory refresh %s failed: %s", operation["id"], exc)
                    continue
                if not plan:
                    age = now - float(operation.get("created_at", now) or now)
                    if age >= _FOK_RECONCILE_GRACE_SEC:
                        self.db.update_merge_operation(
                            operation["id"], "failed", error="confirmed complete set not visible after planned-merge grace"
                        )
                        self._pending_fok_until.pop(condition_key(cid), None)
                        self._record_action(
                            cid, "merge_failed", "-", -1, operation.get("requested_qty", 0),
                            "planned Merge inventory did not reconcile", "no relayer submission; inventory not assumed"
                        )
                    else:
                        self.db.update_merge_operation(
                            operation["id"], "planned", error="awaiting confirmed Data API inventory"
                        )
                    continue
                try:
                    open_orders = self.api.get_open_orders()
                    sells = [
                        o["id"] for o in open_orders
                        if o.get("side") == "SELL"
                        and o.get("asset_id") in (plan.yes_asset_id, plan.no_asset_id)
                        and o.get("id")
                    ]
                    if sells:
                        self.api.cancel_orders(sells)
                        refreshed_orders = self.api.get_open_orders()
                        if any(
                            o.get("side") == "SELL"
                            and o.get("asset_id") in (plan.yes_asset_id, plan.no_asset_id)
                            for o in refreshed_orders
                        ):
                            self.db.update_merge_operation(
                                operation["id"], "planned", error="SELL reservation cancellation not reconciled"
                            )
                            continue
                    refreshed = self.api.get_user_positions(self._funder())
                    capped_refreshed = self._managed_capped_group(
                        cid, refreshed, fresh=True
                    )
                    if capped_refreshed is None:
                        self.db.update_merge_operation(
                            operation["id"], "planned",
                            error="managed inventory unavailable; submission withheld",
                        )
                        continue
                    final = ordinary_binary_plan(cid, capped_refreshed, minimum)
                    if not final:
                        self.db.update_merge_operation(
                            operation["id"], "planned", error="complete set disappeared during planned reconciliation"
                        )
                        continue
                    response = client.submit_merge(self._funder(), cid, final.qty)
                    relayer_id, tx_hash = self._relayer_details(response)
                    if not relayer_id:
                        raise RuntimeError(
                            "relayer response missing transaction id after dispatch"
                        )
                    self.db.update_merge_operation(operation["id"], "submitted", relayer_id, tx_hash)
                    self._record_action(
                        cid, "merge_submitted", "-", 1.0, final.qty,
                        "planned Merge submitted after confirmed inventory refresh", f"relayer_id={relayer_id}"
                    )
                except RelayerUnavailable as exc:
                    # Type3MergeClient raises this only during validation/
                    # nonce preparation, before execute_deposit_wallet_batch.
                    self.db.update_merge_operation(operation["id"], "failed", error=str(exc))
                    self._pending_fok_until.pop(condition_key(cid), None)
                    self._record_action(
                        cid, "merge_failed", "-", -1, operation.get("requested_qty", 0),
                        f"planned Merge submission rejected: {exc}", "no inventory assumed merged"
                    )
                except Exception as exc:
                    # A network fault after request dispatch is indeterminate:
                    # do not retry a possibly accepted transaction. Retain the
                    # durable planned record for a deliberate later recovery.
                    self.db.update_merge_operation(
                        operation["id"], "planned", error=f"relayer submission indeterminate: {exc}"
                    )
                    self._record_action(
                        cid, "merge_submission_unknown", "-", -1, operation.get("requested_qty", 0),
                        "Relayer submission outcome unknown; automatic retry withheld", str(exc)
                    )
            finally:
                lock.release()

    def _confirm_merge_operation(self, operation):
        """Persist exact FIFO lots consumed by a confirmed merge and its PnL."""
        if operation.get("consumed_lots"):
            total_cost = sum(
                float(lot.get("price", 0) or 0) * float(lot.get("take", 0) or 0)
                for consumed in operation["consumed_lots"]
                for lot in consumed.get("lots", [])
            )
            pnl = float(operation["requested_qty"]) - total_cost
            self.db.update_merge_operation(operation["id"], "confirmed", realized_pnl=pnl)
            return
        qty = float(operation["requested_qty"])
        consumed, total_cost = [], 0.0
        # CLOB trade history has no on-chain Merge event. Reconstruct each
        # asset's pre-current-merge FIFO queue by replaying the exact lots that
        # earlier *confirmed* operations already consumed. Without this, a
        # second merge after restart could price the same oldest buy twice.
        prior_merge_events: dict[str, list[dict]] = {}
        try:
            earlier = self.db.get_confirmed_merges(self.wallet_address)
        except Exception as exc:
            logger.warning("prior confirmed merge journal unavailable: %s", exc)
            earlier = []
        for prior in earlier:
            if prior.get("id") == operation.get("id"):
                continue
            ts = prior.get("confirmed_at") or prior.get("created_at") or 0
            for consumed_entry in prior.get("consumed_lots", []) or []:
                asset = consumed_entry.get("asset_id")
                merged_qty = sum(
                    float(lot.get("take", 0) or 0)
                    for lot in consumed_entry.get("lots", []) or []
                )
                if asset and merged_qty > 0:
                    prior_merge_events.setdefault(asset, []).append(
                        {"side": "MERGE", "size": merged_qty, "ts": ts}
                    )
        for asset in (operation["yes_asset_id"], operation["no_asset_id"]):
            try:
                # The post-merge Data API size may already be reduced.  Rebuild
                # a pre-consumption FIFO queue from trade history directly.
                cid = operation["condition_id"]
                trades = self.api.get_trades(TradeParams(market=cid))
                fills = extract_fills(trades, self._funder(), asset)
                lots = [
                    {"price": lot["price"], "take": lot["remaining"], "ts": lot["ts"], "trade_id": lot["trade_id"]}
                    for lot in remaining_fifo_lots(
                        fills, prior_merge_events.get(asset, [])
                    )
                ]
                taken, cost = consume_fifo_lots(lots, qty)
                if not taken:
                    raise RuntimeError("unable to reconstruct merge FIFO consumption")
                consumed.append({"asset_id": asset, "lots": taken})
                total_cost += cost
            except Exception as exc:
                # The chain outcome is already confirmed.  Keep a terminal,
                # ledger-pending state so residual inventory can exit; the
                # confirmed-operation guard prevents Data API lag from causing
                # a second Merge until the FIFO journal is reconstructed.
                self.db.update_merge_operation(
                    operation["id"],
                    "confirmed",
                    error=f"FIFO_LEDGER_PENDING: confirmed transaction; retry required: {exc}",
                )
                logger.error("confirmed merge %s ledger failed: %s", operation["id"], exc)
                return
        pnl = qty - total_cost
        self.db.update_merge_operation(
            operation["id"], "confirmed", realized_pnl=pnl, consumed_lots=consumed
        )
        self._record_action(
            operation["condition_id"], "merge_confirmed", "-", 1.0, qty,
            f"Merge confirmed; realized PnL {pnl:.6f}", "confirmed Type3 Relayer transaction",
        )

    def check_merges(self, condition_lock):
        """Submit/reconcile ordinary binary Type3 merges, never assuming fills."""
        tmpl = self.db.get_template_for(self.wallet_address)
        if not tmpl.get("merge_enabled", True):
            return
        if not getattr(self.api, "trading_enabled", True):
            logger.warning("Merge disabled for %s: %s", self.wallet_address, getattr(self.api, "trading_block_reason", "validation failed"))
            return
        try:
            client = self._merge_client()
        except RelayerUnavailable as exc:
            logger.info("Merge unavailable for %s: %s", self.wallet_address, exc)
            return
        self._retry_confirmed_merge_ledgers()
        initial_unresolved = self.db.get_unresolved_merges(self.wallet_address)
        self._quarantine_duplicate_active_merges(initial_unresolved)
        self._confirm_unresolved_merges(client, initial_unresolved)
        self._reconcile_confirmed_merge_inventory()
        self._reconcile_planned_merges(client, condition_lock)
        # Reconciliation is wallet-wide, but blocking is condition-scoped: a
        # pending/indeterminate Merge for A must never stall new complete sets
        # in B or C.  The per-condition lock and SQLite active-operation guard
        # independently prevent a second A operation.
        active_conditions = {
            condition_key(operation.get("condition_id", ""))
            for operation in self.db.get_unresolved_merges(self.wallet_address)
            if operation.get("condition_id")
        }
        # A confirmed transaction can precede Data API inventory convergence.
        # While its accounting journal is still pending, never infer another
        # complete set for that same condition from stale position rows.
        active_conditions.update(
            condition_key(operation.get("condition_id", ""))
            for operation in self.db.get_confirmed_pending_merges(self.wallet_address)
            if operation.get("condition_id")
        )
        active_conditions.update(
            condition_key(operation.get("condition_id", ""))
            for operation in self.db.get_merge_inventory_barriers(self.wallet_address)
            if operation.get("condition_id")
        )
        try:
            positions = self.api.get_user_positions(self._funder())
            open_orders = self.api.get_open_orders()
        except Exception as exc:
            logger.warning("merge preparation fetch failed: %s", exc)
            return
        minimum = float(tmpl.get("merge_min_shares", 1) or 0)
        for cid, group in positions_by_condition(positions).items():
            if condition_key(cid) in active_conditions:
                continue
            group = self._managed_capped_group(cid, group)
            if group is None:
                logger.warning(
                    "[exit-v2] merge skip %s: managed inventory unavailable",
                    cid,
                )
                continue
            plan = ordinary_binary_plan(cid, group, minimum)
            if not plan:
                continue
            lock = condition_lock(cid)
            if not lock.acquire(blocking=False):
                continue
            try:
                sells = [
                    o["id"] for o in open_orders
                    if o.get("side") == "SELL" and o.get("asset_id") in (plan.yes_asset_id, plan.no_asset_id)
                    and o.get("id")
                ]
                if sells:
                    self.api.cancel_orders(sells)
                    self._record_action(cid, "merge_cancel_sell", "-", -1, plan.qty,
                                        "cancel SELL reservations before Merge", f"cancel {len(sells)} SELL")
                    # A cancellation response alone is not an inventory
                    # reconciliation. Do not submit a merge while the CLOB
                    # still reports a SELL reserving either leg.
                    refreshed_orders = self.api.get_open_orders()
                    conflicts = [
                        o.get("id")
                        for o in refreshed_orders
                        if o.get("side") == "SELL"
                        and o.get("asset_id") in (plan.yes_asset_id, plan.no_asset_id)
                    ]
                    if conflicts:
                        logger.warning(
                            "merge skip %s: SELL cancellation not reconciled (%s)",
                            cid,
                            conflicts,
                        )
                        continue
                # Refetch after every cancellation: merge only confirmed, free inventory.
                refreshed = self.api.get_user_positions(self._funder())
                capped_refreshed = self._managed_capped_group(
                    cid, refreshed, fresh=True
                )
                if capped_refreshed is None:
                    logger.warning(
                        "[exit-v2] merge skip %s: managed inventory unavailable "
                        "after cancellation",
                        cid,
                    )
                    continue
                final = ordinary_binary_plan(cid, capped_refreshed, minimum)
                if not final:
                    continue
                try:
                    op_id = self.db.create_merge_operation(
                        self.wallet_address, self._funder(), cid,
                        final.yes_asset_id, final.no_asset_id, final.qty,
                    )
                except ActiveMergeOperationExists as exc:
                    logger.info("merge already active for %s: %s", cid, exc)
                    active_conditions.add(condition_key(cid))
                    continue
                active_conditions.add(condition_key(cid))
                self._record_action(cid, "merge_planned", "-", 1.0, final.qty,
                                    "ordinary binary complete set planned", "confirmed positions after SELL cancellation")
                try:
                    response = client.submit_merge(self._funder(), cid, final.qty)
                    relayer_id, tx_hash = self._relayer_details(response)
                    if not relayer_id:
                        raise RuntimeError(
                            "relayer response missing transaction id after dispatch"
                        )
                    self.db.update_merge_operation(op_id, "submitted", relayer_id, tx_hash)
                    self._record_action(cid, "merge_submitted", "-", 1.0, final.qty,
                                        "Merge submitted; awaiting confirmation", f"relayer_id={relayer_id}")
                except RelayerUnavailable as exc:
                    self.db.update_merge_operation(op_id, "failed", error=str(exc))
                    self._record_action(cid, "merge_failed", "-", -1, final.qty,
                                        f"Merge submission rejected: {exc}", "no inventory assumed merged")
                except Exception as exc:
                    self.db.update_merge_operation(
                        op_id, "planned", error=f"relayer submission indeterminate: {exc}"
                    )
                    self._record_action(cid, "merge_submission_unknown", "-", -1, final.qty,
                                        "Relayer submission outcome unknown; automatic retry withheld", str(exc))
            finally:
                lock.release()

    def paired_reservations(self, positions) -> dict[str, float]:
        """Return per-asset quantities that must be kept for a potential Merge."""
        tmpl = self.db.get_template_for(self.wallet_address)
        minimum = float(tmpl.get("merge_min_shares", 1) or 0)
        reserved = {}
        for cid, group in positions_by_condition(positions).items():
            plan = ordinary_binary_plan(cid, group, minimum)
            if plan:
                reserved[plan.yes_asset_id] = plan.qty
                reserved[plan.no_asset_id] = plan.qty
        return reserved

    def _managed_capped_group(self, cid, group, fresh: bool = False):
        """Cap a position group to bot-managed quantities (fix pack 3).

        When fast_exit_enabled, automatic Merge may pair ONLY bot-owned
        YES/NO — manual/unproven inventory is never auto-merged.  Returns the
        capped group, or ``None`` when managed inventory cannot be confirmed
        (fail-closed: no Merge planning/submission from this snapshot).
        """
        if not self._fast_exit_enabled():
            return list(group or [])
        managed = self._inventory_exit().managed_asset_quantities(
            cid, positions=group, fresh=fresh
        )
        if managed is None:
            return None
        capped = []
        for pos in group or []:
            asset = str(pos.get("asset", "") or "")
            try:
                size = float(pos.get("size", 0) or 0)
            except (TypeError, ValueError):
                continue
            capped.append(
                {
                    **pos,
                    "size": min(size, managed.get(asset, 0.0)),
                }
            )
        return [p for p in capped if float(p.get("size", 0) or 0) > 0]

    def check_resolution(self, open_orders=None):
        """UMA 结算守卫:对已挂买单的市场,一旦 umaResolutionStatus 非空(有人在 UMA
        上提交了 resolution)即撤掉该市场全部 BUY,避免被成交买进一个即将结算的市场。
        持仓与卖单不动(卖单仍由 check_exit 在结算前正常离场)。

        Gamma 失败时 gamma_resolution_status 返回 {} -> 一律不撤(fail-open),绝不
        因一次接口抖动误撤全仓买单;下个 tick 自然重试。
        """
        if open_orders is None:
            try:
                open_orders = self.api.get_open_orders()
            except Exception as e:
                logger.error("get_open_orders failed (skip resolution guard): %s", e)
                return
        buys_by_cid: dict = {}
        for o in open_orders:
            if o.get("side") != "BUY":
                continue
            cid = o.get("market", "")
            if cid:
                buys_by_cid.setdefault(cid, []).append(o)
        if not buys_by_cid:
            return
        status_map = self.api.gamma_resolution_status(list(buys_by_cid.keys()))
        for cid, buys in buys_by_cid.items():
            status = status_map.get(cid)
            if not in_resolution(status):
                continue
            ids = [o.get("id") for o in buys]
            try:
                self.api.cancel_orders(ids)
            except Exception as e:
                logger.warning(
                    "UMA resolution cancel failed cid=%s: %s — buys 未撤", cid, e
                )
                self._status_add(
                    market=cid,
                    side="买入",
                    price="-",
                    size="-",
                    matched="-",
                    stage="结算守卫",
                    action="⚠️UMA撤单失败",
                    detail=f"umaResolutionStatus={status}，撤 {len(ids)} 笔买单失败：{e}",
                )
                continue
            self._record_action(
                market_id=cid,
                action_type="uma_resolution_cancel",
                side="-",
                price=-1,
                size=0,
                reason=(
                    f"市场进入 UMA 结算(状态={status})，撤销该市场全部买单，"
                    "避免买进将结算的市场"
                ),
                price_basis=(
                    f"status={status}；撤 {len(ids)} 笔 BUY；"
                    "来源：Gamma /markets condition_ids"
                ),
            )
            self._status_add(
                market=cid,
                side="买入",
                price="-",
                size="-",
                matched="-",
                stage="结算守卫",
                action="⚠️UMA已提交·撤买单",
                detail=f"umaResolutionStatus={status}，撤 {len(ids)} 笔买单",
            )

    def _exit_prefetch(self, positions) -> None:
        """本 tick 离场判定所需的成交/盘口并发预取,结果写进两张 per-tick 表。

        **成交先取、盘口后取,两拨串行,顺序不可调。** 先取的那拨等到判定时,已经陈旧了
        后取那拨的全部耗时;而盘口驱动挂卖价与 B0 强平判定(是钱路上的决定),成本则只在
        我们自己成交时才变。与 Step3 `_prefetch` 里「奖励先、盘口后」是同构的理由。

        成交拨按 condition_id 去重:同一市场的 YES/NO 两个持仓共用一次
        get_trades(market=cid)。盘口拨按 asset_id 去重。已经在表里的键一律跳过,所以
        check_low_balance 先预取过一轮之后,check_exit 这轮只补差集。

        表里的 None 表示**取数失败**,与「取到了但没有成交 / 盘口为空」不是一回事:
        前者让成本算不出来(跳过 + ⚠️裸奔),后者是正常的空结果。两者塌成一个会让监控
        状态表说谎。

        worker 线程只发网络请求、**绝不碰 db**(每线程一条 sqlite 连接且从不回收),
        每个任务自带异常处理、绝不外抛。判定、下单、记账、状态行全部留在主线程。
        """
        cids = [
            c
            for c in {p.get("conditionId", "") for p in positions}
            if c and c not in self._trades_by_cid
        ]
        assets = [
            a
            for a in {p.get("asset", "") for p in positions}
            if a and a not in self._book_cache
        ]

        def _trades(cid):
            try:
                return self.api.get_trades(TradeParams(market=cid))
            except Exception as e:
                logger.warning("[离场预取] get_trades(market=%s) 失败: %s", cid, e)
                return None

        if cids:
            self._trades_by_cid.update(
                zip(cids, parallel_map(_trades, cids, _EXIT_MAX_WORKERS))
            )
        if assets:
            try:
                self._book_cache.update(self.api.get_orderbooks(assets))
            except Exception as e:
                # get_orderbooks 内部已按批 try(失败批记 None),走到这里是整个调用崩了。
                # **什么都不写**:表里留空(_MISSING)让 _sell_book 逐仓自取,即并发化
                # 之前的串行行为。写 None 反而会关掉那条自取退路,把一次抖动变成整轮
                # 拿不到盘口 —— 与 _cost_lots 对预取失败改自取重试是同一个道理。
                logger.warning("[离场预取] 批量订单簿失败,退回逐仓自取: %s", e)

    def _sell_book(self, asset_id: str):
        """(tick_float, tick_str, best_bid, best_ask);失败/空时缺的位回 None。

        优先读本 tick 的预取表(_exit_prefetch 写入);表里没有该 asset 才自取,所以
        不走预取的调用点行为不变。表里的 None = 预取时取数失败,与自取失败同样降级。
        """
        ob = self._book_cache.get(asset_id, _MISSING)
        if ob is _MISSING:
            try:
                ob = self.api.get_orderbook(asset_id)
            except Exception as e:
                logger.warning(
                    "orderbook for %s failed (exit tick=0.01): %s", asset_id, e
                )
                return 0.01, "0.01", None, None
        if ob is None:
            logger.warning("orderbook for %s 预取取不到 (exit tick=0.01)", asset_id)
            return 0.01, "0.01", None, None
        try:
            tick_str = ob.get("tick_size", "0.01")
            bids = sorted(
                ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            return float(tick_str), tick_str, best_bid, best_ask
        except Exception as e:
            logger.warning(
                "orderbook for %s 解析失败 (exit tick=0.01): %s", asset_id, e
            )
            return 0.01, "0.01", None, None

    @staticmethod
    def _book_levels(orderbook, side: str):
        rows = (orderbook or {}).get("bids" if side == "bids" else "asks", [])
        levels = []
        for row in rows:
            try:
                levels.append({"price": float(row["price"]), "size": float(row["size"])})
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(levels, key=lambda level: level["price"], reverse=side == "bids")

    def _opposite_token(self, condition_id: str, asset_id: str):
        """Find the ordinary-binary complement through the current CLOB market."""
        market = self.api.get_market(condition_id)
        tokens = market.get("tokens") or market.get("outcomes") or []
        parsed = []
        for token in tokens:
            if isinstance(token, dict):
                token_id = token.get("token_id") or token.get("tokenId") or token.get("id")
                outcome = str(token.get("outcome") or token.get("name") or "").upper()
                if token_id:
                    parsed.append((token_id, outcome))
        ours = next((outcome for token_id, outcome in parsed if str(token_id) == str(asset_id)), "")
        return next((token_id for token_id, outcome in parsed if outcome in {"YES", "NO"} and outcome != ours), None)

    def _urgent_merge_route(self, cid, asset_id, size, tick_str, *, condition_locked=False):
        """Compare executable direct sale to FOK complement+Merge for B0 only."""
        # A complement purchase is safe only when this worker can immediately
        # reconcile it through the Type3 relayer path. Other signature modes
        # fall back to the normal direct-exit decision.
        if int(getattr(self.api, "signature_type", -1)) != 3:
            return None
        try:
            self._merge_client()
        except RelayerUnavailable:
            return None
        try:
            direct_book = self._book_cache.get(asset_id) or self.api.get_orderbook(asset_id)
            complement = self._opposite_token(cid, asset_id)
            if not complement:
                return None
            complement_book = self.api.get_orderbook(complement)
            asks = self._book_levels(complement_book, "asks")
            limit_price = executable_limit_price(asks, size)
            if limit_price is None:
                return None
            # The V2 FOK signature fixes both the collateral amount and its
            # limit price. Compare its signed worst-case cost, not a best-case
            # depth average that the exchange is not obliged to price-improve.
            route = route_residual(
                qty=size,
                severe_loss=True,
                direct_levels=self._book_levels(direct_book, "bids"),
                complement_asks=[{"price": limit_price, "size": size}],
                safety_margin=float(self.db.get_template_for(self.wallet_address).get("merge_advantage_min_usd", 0.01) or 0),
            )
            self._record_action(cid, "urgent_route_selected", "-", -1, size, route.reason,
                                f"direct={route.direct_value}; merge={route.merge_value}; advantage={route.advantage}")
            if route.action != "buy_complement_and_merge":
                return None
            mutation_lock = None if condition_locked else self._try_condition_lock(cid)
            if mutation_lock is False:
                return None
            try:
                # Persist before FOK submission. If a process stops between
                # these calls, restart reconciliation waits for actual
                # inventory instead of forgetting an in-flight complement.
                op_id = self.db.create_merge_operation(
                    self.wallet_address, self._funder(), cid, asset_id, complement, size
                )
            except ActiveMergeOperationExists as exc:
                # Another worker/process already owns this exact condition.
                # It may also own an accepted/indeterminate FOK, so falling
                # through to a direct SELL would race the same inventory.
                self._release_condition_lock(mutation_lock)
                logger.info("urgent Merge already active for %s: %s", cid, exc)
                return "wait_for_merge"
            except Exception as exc:
                self._release_condition_lock(mutation_lock)
                logger.error("cannot persist pre-FOK Merge plan %s: %s", cid, exc)
                return None
            try:
                try:
                    self.api.place_complement_fok_buy(
                        complement, size, limit_price,
                        tick_size=complement_book.get("tick_size", tick_str), neg_risk=None
                    )
                except OrderRejected as exc:
                    self.db.update_merge_operation(op_id, "failed", error=f"FOK unfilled/rejected: {exc}")
                    self._pending_fok_until.pop(condition_key(cid), None)
                    self._record_action(cid, "urgent_fok_failed", "-", -1, size,
                                        f"FOK failed: {exc}", "no FOK inventory assumed; fall back to direct exit")
                    return None
                except Exception as exc:
                    # A transport failure may have happened after the exchange
                    # accepted the FOK. Treat it as indeterminate, not failed:
                    # a short durable reconciliation window prevents selling a
                    # possibly completed leg as if it did not exist.
                    self.db.update_merge_operation(
                        op_id, "planned", error=f"FOK submission indeterminate; awaiting inventory: {exc}"
                    )
                    self._pending_fok_until[condition_key(cid)] = (
                        time.time() + _FOK_RECONCILE_GRACE_SEC
                    )
                    self._record_action(cid, "urgent_fok_indeterminate", "-", -1, size,
                                        "FOK transport outcome unknown; awaiting inventory", str(exc))
                    return "wait_for_merge"
            finally:
                self._release_condition_lock(mutation_lock)
            # The FOK result is not merge confirmation and the Data API can lag
            # it. Keep its durable operation planned until this inventory is
            # visible and reconciliation safely submits the relayer batch.
            self.db.update_merge_operation(
                op_id, "planned", error="FOK accepted; awaiting confirmed Data API inventory"
            )
            self._pending_fok_until[condition_key(cid)] = (
                time.time() + _FOK_RECONCILE_GRACE_SEC
            )
            self._record_action(cid, "urgent_fok_submitted", "BUY", -1, size,
                                "FOK complement accepted; awaiting inventory then Merge", "all-or-nothing protected buy")
            return "wait_for_merge"
        except Exception as exc:
            logger.warning("urgent merge route unavailable asset=%s: %s", asset_id, exc)
            return None

    def check_low_balance(self, open_orders=None):
        """余额 < 阈值(0=关)时按优先级逐笔市价卖持仓腾现金,卖到「停手目标」停。
        覆盖「永不低于成本」(主动清仓腾现金,同结算清仓)。用估算余额防过卖(市价卖后链上
        余额有滞后,逐笔重查会过卖);无买盘跳过;整体失败不阻断其余步骤。在 check_exit 之前跑。"""
        tmpl = self.db.get_template_for(self.wallet_address)
        threshold = float(tmpl.get("low_balance_threshold_usd", 4) or 0)
        if threshold <= 0:
            return
        if time.time() < self._liquidate_cooldown_until:
            return  # 上次清仓后的冷却期:等到手余额链上结算,避免跨 tick 过卖
        try:
            balance = float(self.api.get_balance() or 0)
        except Exception as e:
            logger.warning("get_balance failed (skip low-balance): %s", e)
            return
        if balance >= threshold:
            return
        low_reward = float(tmpl.get("low_reward_threshold_usd", 30) or 30)
        small_shares = float(tmpl.get("small_position_shares", 20) or 20)
        if tmpl.get("liquidate_target_mode", "balance") == "next_order":
            moc = self.db.get_min_order_cost()
            target = moc if moc else threshold
        else:
            target = float(tmpl.get("liquidate_target_usd", 4) or 4)
        if balance >= target:
            return  # 已达目标(target<threshold 时可能):免空扫拉持仓/成本
        try:
            positions = self.api.get_user_positions(self._funder())
            if open_orders is None:
                open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.warning("fetch failed (skip low-balance): %s", e)
            return
        # Complete binary sets are collateral-equivalent.  Low-balance recovery
        # must never unilateral-sell either leg before the merge pass can act.
        merge_reserved = self.paired_reservations(positions)
        in_flight_merge_conditions = {
            condition_key(operation.get("condition_id", ""))
            for operation in self.db.get_unresolved_merges(self.wallet_address)
            if operation.get("condition_id")
        }
        in_flight_merge_conditions.update(
            condition_key(cid)
            for cid, until in self._pending_fok_until.items()
            if until > time.time()
        )
        # 逐仓的成交/盘口一次性并发取好,下面的循环只做纯判定(见 _exit_prefetch)。
        # 包 try:parallel_map 可能在任何一个任务开跑之前就抛(最现实的是线程池建不出线程,
        # N 个钱包 × 6 路 + 采集的 4 路同进程),让它逃出去会连带跳过后面的 check_exit ——
        # 全部持仓无保护、无 ⚠️ 状态行、什么都不发布,正是本仓出过两次事故的静默故障。
        # 降级是免费的:预取表空 = 每个 _cost_lots/_sell_book 各自回退自取(并发化前的行为)。
        try:
            self._exit_prefetch(
                [p for p in positions if float(p.get("size", 0) or 0) > 0]
            )
        except Exception as e:
            logger.warning("[低余额] 离场预取失败(退回逐仓自取): %s", e)
        meta, candidates = {}, []
        for pos in positions:
            asset_id = pos.get("asset", "")
            size = float(pos.get("size", 0) or 0)
            if size <= 0:
                continue
            cid = pos.get("conditionId", "")
            if condition_key(cid) in in_flight_merge_conditions:
                continue
            # Keep all inventory in a pair-bearing condition untouched until
            # the merge has settled. Selling only the apparent residual could
            # consume the paired quantity before relayer confirmation.
            if merge_reserved.get(asset_id, 0) > 0:
                continue
            cur = float(pos.get("curPrice", 0) or 0)
            cost, lots = self._cost_lots(asset_id, size, cid)
            if cost is None:
                # 成本没重建出(新成交未进 get_trades):推迟,不盲目清仓认亏(同 check_exit
                # 的裸奔跳过;低余额无结算 deadline,不必像结算清仓那样成本未知也照卖)。
                continue
            _, tick_str, best_bid, _ = self._sell_book(asset_id)
            reward = self.db.get_market_daily_reward(cid)
            loss = None if best_bid is None else (cost - best_bid) * size
            meta[asset_id] = {
                "cid": cid,
                "size": size,
                "cur": cur,
                "best_bid": best_bid,
                "cost": cost,
                "lots": lots,
                "tick_str": tick_str,
            }
            candidates.append(
                {
                    "asset_id": asset_id,
                    "size": size,
                    "daily_reward": reward,
                    "loss": loss,
                }
            )
        est = balance
        dumped_any = False
        for asset_id in plan_liquidation(candidates, low_reward, small_shares):
            if est >= target:
                break
            m = meta[asset_id]
            bb = m["best_bid"]
            if bb is None or bb <= 0:
                continue  # 无买盘,卖不出 -> 跳过(不计入腾现金)
            try:
                mutation_lock = self._try_condition_lock(m["cid"])
                if mutation_lock is False:
                    continue
                try:
                    fill = self._market_dump(
                        m["cid"],
                        asset_id,
                        m["size"],
                        m["cur"],
                        bb,
                        m["cost"],
                        m["lots"],
                        open_orders,
                        tag="低余额",
                        reason="低余额清仓腾现金（无视盈亏）",
                        tick_str=m["tick_str"],
                    )
                finally:
                    self._release_condition_lock(mutation_lock)
            except Exception as e:
                logger.warning("low-balance dump %s failed: %s", asset_id, e)
                continue
            if fill is not None:
                self._just_dumped.add(
                    asset_id
                )  # check_exit 同 tick 跳过(仓已卖、Data API 滞后)
                dumped_any = True
                est += bb * m["size"]  # 保守估算到手(best_bid×份额),防过卖
        if dumped_any:
            self._liquidate_cooldown_until = time.time() + LIQUIDATE_COOLDOWN_SEC

    def check_exit(self, open_orders=None):
        """两段式离场:成本≤买一挂卖一,成本>买一挂成本价(永不低于成本;亏损≥强平阈值兜底市价止损)。

        强平阈值可配置(模板 stop_loss_mode):按比例(占成本%,默认20%)或按固定金额(美分)。
        """
        tmpl = self.db.get_template_for(self.wallet_address)
        theta_loss = float(tmpl.get("theta_loss_cents", 2)) / 100.0
        stop_mode = tmpl.get("stop_loss_mode", "percent")
        stop_percent = tmpl.get("stop_loss_percent", 20)
        stop_cents = tmpl.get("theta_stop_cents", 5)
        case_a_mode = tmpl.get("case_a_mode", "ask")
        take_profit_mode = tmpl.get("take_profit_mode", "maker")
        # 先置「本轮未判定」:任何早退(取持仓/取挂单失败)都不能让 tick 耗时日志把上一轮的
        # 持仓数当成这一轮报出来 —— 那条日志是判断还要不要继续提速的唯一依据,不能骗人。
        self.last_position_count = None
        try:
            positions = self.api.get_user_positions(self._funder())
        except Exception as e:
            logger.warning("Data API positions failed (skip exit): %s", e)
            return
        if open_orders is None:
            try:
                open_orders = self.api.get_open_orders()
            except Exception as e:
                logger.error("get_open_orders failed (skip exit): %s", e)
                return
        # Merge is run earlier in the tick.  Until a confirmed result changes
        # inventory, paired quantities are deliberately held out of unilateral
        # stop loss / maker-exit mutation.
        merge_reserved = self.paired_reservations(positions)
        # 结算守卫(持仓侧):对持仓所在市场批量取 UMA 结算状态,结果已提交(非空)的市场,
        # 该持仓无视盈亏市价清仓。fail-open:Gamma 返回 {} -> resolving 空 -> 全部走原离场。
        cids = [
            pos.get("conditionId", "")
            for pos in positions
            if float(pos.get("size", 0) or 0) > 0
        ]
        status_map = self.api.gamma_resolution_status(cids)
        resolving = {c for c in cids if in_resolution(status_map.get(c))}
        # A funds-moving Merge always precedes resolution liquidation.  UMA
        # status cannot make it safe to sell inventory that a submitted Merge
        # (or an accepted/indeterminate FOK plan) may already be consuming.
        unresolved_conditions = {
            condition_key(op.get("condition_id", ""))
            for op in self.db.get_unresolved_merges(self.wallet_address)
            if op.get("condition_id")
        }
        unresolved_conditions.update(
            condition_key(cid)
            for cid, until in self._pending_fok_until.items()
            if until > time.time()
        )
        self.last_position_count = len(cids)
        # 本轮真要判定的仓(刚被低余额清掉的不算)一次性并发取好成交与盘口。
        # 包 try 的理由同 check_low_balance:预取抛出去会让整轮离场判定不执行(持仓全裸奔
        # 且无任何状态行),而降级只是退回并发化之前的逐仓自取。
        try:
            self._exit_prefetch(
                [
                    p
                    for p in positions
                    if float(p.get("size", 0) or 0) > 0
                    and p.get("asset", "") not in self._just_dumped
                ]
            )
        except Exception as e:
            logger.warning("[离场] 预取失败(退回逐仓自取): %s", e)
        for pos in positions:
            if pos.get("asset", "") in self._just_dumped:
                continue  # 本 tick 已被低余额清仓卖掉:Data API /positions 滞后仍显满仓,
                # 别对已无份额的仓挂卖单被拒、报假「裸奔」;下 tick 持仓刷新后自然消失。
            try:
                cid = pos.get("conditionId", "")
                if condition_key(cid) in unresolved_conditions:
                    self._status_add(
                        market=cid, side="卖出", price="-", size=str(pos.get("size", "")),
                        matched="-", stage="合并", action="等待合并确认",
                        detail="未决 Relayer Merge 阻止并行离场变更",
                    )
                    continue
                reserved_qty = merge_reserved.get(pos.get("asset", ""), 0.0)
                size = float(pos.get("size", 0) or 0)
                if reserved_qty >= size - 1e-9:
                    self._status_add(
                        market=pos.get("conditionId", ""), side="卖出", price="-",
                        size=str(size), matched="-", stage="合并",
                        action="等待合并", detail=f"保留 {reserved_qty:g} 份完整集合",
                    )
                    continue
                if reserved_qty > 0:
                    pos = dict(pos)
                    pos["size"] = size - reserved_qty
                mutation_lock = self._try_condition_lock(cid)
                if mutation_lock is False:
                    continue
                try:
                    self._exit_position(
                        pos,
                        open_orders,
                        theta_loss,
                        stop_mode,
                        stop_percent,
                        stop_cents,
                        case_a_mode,
                        take_profit_mode,
                        in_resolution_market=cid in resolving,
                    )
                finally:
                    self._release_condition_lock(mutation_lock)
            except Exception as e:
                logger.error("Exit error on %s: %s", pos.get("asset"), e)

    def _exit_position(
        self,
        pos,
        open_orders,
        theta_loss,
        stop_mode,
        stop_percent,
        stop_cents,
        case_a_mode,
        take_profit_mode="maker",
        in_resolution_market=False,
    ):
        asset_id = pos.get("asset", "")
        size = float(pos.get("size", 0) or 0)
        cur = float(pos.get("curPrice", 0) or 0)
        cid = pos.get("conditionId", "")
        if size <= 0:
            return
        cost, lots = self._cost_lots(asset_id, size, cid)
        if in_resolution_market:
            # 结算清仓:结果已提交,无视盈亏、无视成本是否算得出,市价清掉该持仓。
            self._resolution_dump(cid, asset_id, size, cur, cost, lots, open_orders)
            return
        if cost is None or cost <= 0:
            logger.warning(
                "Exit skipped (no buy fills) asset=%s size=%s — UNPROTECTED",
                asset_id,
                size,
            )
            self._status_add(
                market=cid,
                side="卖出",
                price="-",
                size=str(size),
                matched="-",
                stage="离场",
                action="⚠️跳过·裸奔",
                detail="get_trades 无买入成交、无法算成本，未离场，该持仓未受保护",
            )
            return
        tick, tick_str, best_bid, best_ask = self._sell_book(asset_id)
        # 强平阈值按成本现算(比例模式 = 成本×%;固定模式 = 美分)。
        theta_stop = effective_theta_stop(cost, stop_mode, stop_percent, stop_cents)
        plan = plan_exit(
            cost,
            best_bid,
            best_ask,
            tick,
            theta_loss,
            theta_stop,
            case_a_mode,
            size,
            take_profit_mode,
        )
        # 每次离场都把盘口+成本+强平阈值+决策打进日志,留作抓现行的证据(下次再现 0.13 卖,可
        # 一眼分清是成本算错[成本=0.13]还是盘口抽风[成本=0.14 却挂穿])。
        stop_txt = f"{theta_stop:.4f}" if theta_stop is not None else "关闭"
        logger.info(
            "离场决策 asset=%s 成本=%.4f 买一=%s 卖一=%s tick=%s size=%s 强平阈值=%s(%s) -> tier=%s action=%s 挂价=%s",
            asset_id,
            cost,
            best_bid,
            best_ask,
            tick_str,
            size,
            stop_txt,
            stop_mode,
            plan["tier"],
            plan["action"],
            plan["price"],
        )
        # open_orders 是 tick 开头的共用快照:本 tick 已被撤掉的卖单(低余额/结算清仓撤的)
        # 在快照里还在,必须滤掉——否则 plan_take_profit 会以为目标价上已有一张卖单而返回
        # keep,持仓实际无任何卖单保护,状态行还谎报「保持」。
        sells = [
            o
            for o in open_orders
            if o.get("asset_id") == asset_id
            and o.get("side") == "SELL"
            and o.get("id") not in self._cancelled_sell_ids
        ]
        action = plan["action"]
        basis = describe_cost_basis(cost, lots)

        if action == "market" and plan["tier"] == "B0":
            urgent = self._urgent_merge_route(cid, asset_id, size, tick_str, condition_locked=True)
            if urgent == "wait_for_merge":
                self._status_add(
                    market=cid, side="买入", price="-", size=str(size), matched="-",
                    stage="紧急路由", action="FOK补全→合并", detail="已接受 FOK，等待库存刷新与合并确认",
                )
                return

        if action == "noop":
            self._status_add(
                market=cid,
                side="卖出",
                price="-",
                size=str(size),
                matched="-",
                stage="离场",
                action="跳过(盘口空)",
                detail="无买盘无卖盘，本轮跳过",
            )
            return

        if action == "rest":
            want = plan["price"]
            # 硬底线:下任何挂卖单前,价格强制 ≥ 成本价(tick 对齐)。即便 plan_exit/成本一时
            # 异常吐出低于成本的价,也绝不挂穿成本。钳到下限时打 WARNING 以便抓现行。
            floor = ceil_to_tick(cost, tick)
            if want is not None and want < floor - 1e-9:
                logger.warning(
                    "离场挂价 %.4f 低于成本下限 %.4f，已钳至下限（asset=%s tier=%s 买一=%s 卖一=%s 成本=%.4f）",
                    want,
                    floor,
                    asset_id,
                    plan["tier"],
                    best_bid,
                    best_ask,
                    cost,
                )
                want = floor
            p = plan_take_profit(size, want, tick, sells)
            if p["action"] in ("noop", "keep"):
                self._status_add(
                    market=cid,
                    side="卖出",
                    price=f"{want:.4f}",
                    size=str(size),
                    matched="-",
                    stage="离场",
                    action=f"保持({plan['tier']})",
                    detail=f"成本{cost:.4f} 挂卖单{want:.4f}",
                )
                return
            if p["cancel_ids"]:
                try:
                    self.api.cancel_orders(p["cancel_ids"])
                    self._record_action(
                        market_id=cid,
                        action_type="exit_recancel",
                        side="-",
                        price=-1,
                        size=size,
                        reason="撤与持仓不符的旧卖单，改按持仓挂一笔",
                        price_basis=f"撤 {len(p['cancel_ids'])} 笔 SELL",
                    )
                except Exception as e:
                    logger.warning("Cancel stale sells %s failed: %s", asset_id, e)
                    return
            try:
                self.api.place_limit_sell(asset_id, want, size, tick_size=tick_str)
            except Exception as e:
                logger.error("Rest sell failed asset=%s: %s — UNPROTECTED", asset_id, e)
                self._status_add(
                    market=cid,
                    side="卖出",
                    price=f"{want:.4f}",
                    size=str(size),
                    matched="-",
                    stage="离场",
                    action="⚠️挂卖失败·裸奔",
                    detail=f"{plan['tier']} 挂卖单被拒，该持仓未受保护：{e}",
                )
                return
            self._record_action(
                market_id=cid,
                action_type="exit_rest",
                side="卖出",
                price=want,
                size=size,
                reason=f"{plan['tier']}：挂卖单离场",
                price_basis=f"{basis}；挂卖价{want:.4f}；来源：CLOB get_trades+get_orderbook",
            )
            self._status_add(
                market=cid,
                side="卖出",
                price=f"{want:.4f}",
                size=str(size),
                matched="-",
                stage="离场",
                action=f"挂卖单({plan['tier']})",
                detail=f"成本{cost:.4f}",
            )
            return

        sell_ids = [o["id"] for o in sells]
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
                self._record_action(
                    market_id=cid,
                    action_type="exit_cancel_sell",
                    side="-",
                    price=-1,
                    size=size,
                    reason=f"{plan['tier']}：先撤全部止盈卖单以便清仓",
                    price_basis=f"撤 {len(sell_ids)} 笔 SELL",
                )
            except Exception as e:
                # 撤单失败仍继续清仓:离场(尤其 B0 强平)优先保证出手,不因撤单一时失败
                # 把仓晾着不止损。代价是可能短暂与残留挂卖单并存(下个 tick 对账清理);
                # 超卖不会发生(没有的份额卖不出)。与旧 stop-loss 的取舍一致。
                logger.warning(
                    "Cancel sells %s failed (proceed to exit): %s", asset_id, e
                )

        if action == "market":
            try:
                resp = self.api.place_market_sell(asset_id, size, tick_size=tick_str)
            except Exception as e:
                logger.error(
                    "Market exit failed asset=%s: %s — UNPROTECTED", asset_id, e
                )
                self._status_add(
                    market=cid,
                    side="卖出",
                    price=f"{cur:.4f}",
                    size=str(size),
                    matched="-",
                    stage="离场",
                    action="⚠️止损失败·裸奔",
                    detail=f"{plan['tier']} 市价清仓被拒，该持仓未受保护：{e}",
                )
                return
            # 真实成交价(≈盘口买一,FAK 从买一往下吃),绝不用 Data API 现价——现价常与盘口
            # 背离,会把亏损显示成盈利(2026-06-24 用户实报:现价 0.13 记成盈利,实为 6.5¢ 止损)。
            fill = market_fill_price(resp, best_bid, cur)
            is_profit = plan["tier"] == "A_market"
            if plan["tier"] == "B0":
                self.db.record_trade(
                    wallet=self.wallet_address,
                    market_id=cid,
                    market_name="",
                    side="stop_loss",
                    price=fill,
                    size=size,
                    pnl=(fill - cost) * size,
                )
            self._record_action(
                market_id=cid,
                action_type="exit_market",
                side="卖出",
                price=fill,
                size=size,
                reason=(
                    f"{plan['tier']}：浮盈市价止盈离场"
                    if is_profit
                    else f"{plan['tier']}：市价清仓离场"
                ),
                price_basis=(
                    f"{basis}；市价清仓·成交≈买一{fill:.4f}（精确成交以链上为准）；"
                    f"来源：CLOB get_trades+get_orderbook"
                ),
            )
            self._status_add(
                market=cid,
                side="卖出",
                price=f"{fill:.4f}",
                size=str(size),
                matched="-",
                stage="离场",
                action=(
                    f"浮盈市价止盈({plan['tier']})"
                    if is_profit
                    else f"市价清仓({plan['tier']})"
                ),
                detail=f"成本{cost:.4f} 成交≈{fill:.4f}",
            )
            return

    def _resolution_dump(self, cid, asset_id, size, cur, cost, lots, open_orders):
        """结算清仓:市场结果已提交(umaResolutionStatus 非空)时,无视盈亏市价把该持仓清掉。

        与正常离场的关键差异:成本取不到也照卖——结算在即,裸奔不卖=等着被结算成 0,是最坏
        情况。先撤该 asset 全部挂卖单(否则挂卖单占用份额,市价卖没份额可卖),再市价卖。
        成交价用 market_fill_price(≈买一),绝不用 Data API 现价。
        """
        _, tick_str, best_bid, _ = self._sell_book(asset_id)
        if best_bid is None or best_bid <= 0:
            # 无买盘 / 买一≤0(或盘口拉取失败):市价单吃不到任何流动性(卖不出),且
            # market_fill_price 仅当 best_bid>0 才取买一、否则退回被禁的 Data API 现价
            # -> 会记一笔幻影成交。故不卖不记,发 ⚠️ 状态行标记该持仓仍暴露,下 tick 再清(自愈)。
            self._status_add(
                market=cid,
                side="卖出",
                price="-",
                size=str(size),
                matched="-",
                stage="离场",
                action="⚠️结算·无买盘暂未清仓",
                detail="市场结果已提交但盘口无买盘，市价卖不出，该持仓仍暴露，待有买盘再清",
            )
            return
        self._market_dump(
            cid,
            asset_id,
            size,
            cur,
            best_bid,
            cost,
            lots,
            open_orders,
            tag="结算",
            reason="市场结果已提交/进入 UMA 结算 → 市价清仓（无视盈亏）",
            expose_on_fail=True,
            tick_str=tick_str,
        )

    def _market_dump(
        self,
        cid,
        asset_id,
        size,
        cur,
        best_bid,
        cost,
        lots,
        open_orders,
        tag,
        reason,
        expose_on_fail=False,
        tick_str="0.01",
    ):
        """市价清仓一个持仓(撤挂卖→FAK 市价卖→记账),覆盖「永不低于成本」。tag=短标签
        (结算/低余额),reason=record_action 理由。成功返成交价 fill、被拒返 None。
        expose_on_fail=True(结算):卖失败时标「裸奔」(仓暴露给结算);False(低余额):仓仍
        被 check_exit 保护、只标失败。调用方须先确保 best_bid 有效(无买盘不进来)。共用。"""
        sells = [
            o
            for o in open_orders
            if o.get("asset_id") == asset_id
            and o.get("side") == "SELL"
            and o.get("id") not in self._cancelled_sell_ids
        ]
        sell_ids = [o["id"] for o in sells]
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
                # 撤成功即登记:共用快照里这些单还在,后面的 check_exit 不滤掉就会把它们
                # 当成"仓已有卖单保护"(市价卖被拒时 -> 裸奔 + 状态行谎报「保持」)。
                self._cancelled_sell_ids.update(sell_ids)
                self._record_action(
                    market_id=cid,
                    action_type="exit_cancel_sell",
                    side="-",
                    price=-1,
                    size=size,
                    reason=f"{tag}清仓:先撤全部挂卖单以便市价清仓",
                    price_basis=f"撤 {len(sell_ids)} 笔 SELL",
                )
            except Exception as e:
                # 撤单失败仍继续清仓:优先保证出手(超卖不会发生,没有的份额卖不出)。
                logger.warning(
                    "Cancel sells %s failed (proceed to %s dump): %s", asset_id, tag, e
                )
        try:
            resp = self.api.place_market_sell(asset_id, size, tick_size=tick_str)
        except Exception as e:
            logger.error("%s dump failed asset=%s: %s", tag, asset_id, e)
            self._status_add(
                market=cid,
                side="卖出",
                price=f"{cur:.4f}",
                size=str(size),
                matched="-",
                stage="离场",
                action=f"⚠️{tag}清仓失败" + ("·裸奔" if expose_on_fail else ""),
                detail=f"{tag}市价清仓被拒:{e}"
                + ("，该持仓未受保护" if expose_on_fail else ""),
            )
            return None
        fill = market_fill_price(resp, best_bid, cur)
        if cost is not None and cost > 0:
            self.db.record_trade(
                wallet=self.wallet_address,
                market_id=cid,
                market_name="",
                side="stop_loss",
                price=fill,
                size=size,
                pnl=(fill - cost) * size,
            )
            basis = describe_cost_basis(cost, lots)
            price_basis = (
                f"{basis}；{tag}市价清仓·成交≈买一{fill:.4f}（精确成交以链上为准）；"
                f"来源：CLOB get_trades+get_orderbook"
            )
            detail = f"成本{cost:.4f} 成交≈{fill:.4f}"
        else:
            price_basis = (
                f"成本未知（get_trades 无买入成交）；{tag}市价清仓·"
                f"成交≈买一{fill:.4f}（精确成交以链上为准）"
            )
            detail = f"成本未知 成交≈{fill:.4f}"
        self._record_action(
            market_id=cid,
            action_type="exit_market",
            side="卖出",
            price=fill,
            size=size,
            reason=reason,
            price_basis=price_basis,
        )
        self._status_add(
            market=cid,
            side="卖出",
            price=f"{fill:.4f}",
            size=str(size),
            matched="-",
            stage="离场",
            action=f"⚠️{tag}·市价清仓",
            detail=detail,
        )
        return fill

    # --- Step 3: strategy compliance on resting buy orders ---
    def check_sell_orders(self):
        """Reused tick name kept for the manager loop; runs strategy compliance."""
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            return
        self._observe_order_scoring(open_orders)
        # DB 读全部收在主线程、每轮一次:黑名单和模板原来是每单各读一遍(60 个挂单 =
        # 60 次查询),ttl 还要传进预取的 worker —— worker 绝不碰 db。
        # 整段包 try:这几步原来是每单一次、失败被逐单 try 吞掉(Step3 降级但循环跑完、
        # publish_status 照常执行);提到轮级之后,一次失败就会从 check_sell_orders 抛出去、
        # 跳过 publish_status 把监控状态表冻住(manager._run 的 F4 注释正是防这个)。
        try:
            blacklist = self.db.get_blacklist_ids()
            settings = self.db.get_template_for(self.wallet_address)
            ttl = self.db.get_settings()["rewards_cache_ttl_sec"]
            books, rewards = self._prefetch(open_orders, blacklist, ttl)
        except Exception as e:
            logger.error("[Step3] 本轮取数准备失败 %s: %s", self.wallet_address, e)
            return
        for o in open_orders:
            price_s = f"{float(o.get('price', 0) or 0):.4f}"
            size_s = str(o.get("original_size", ""))
            matched_s = str(o.get("size_matched", "0"))
            if o.get("side") != "BUY":
                self._status_add(
                    market=o.get("market", ""),
                    side="卖出",
                    price=price_s,
                    size=size_s,
                    matched=matched_s,
                    stage="止盈卖单",
                    action="挂单中",
                    detail="",
                )
                continue
            if float(o.get("size_matched", 0) or 0) > 0:
                self._status_add(
                    market=o.get("market", ""),
                    side="买入",
                    price=price_s,
                    size=size_s,
                    matched=matched_s,
                    stage="Step1",
                    action="部分成交",
                    detail=f"已成交{matched_s}",
                )
                continue
            try:
                self._check_compliance(o, blacklist, settings, books, rewards)
            except Exception as e:
                logger.error("Compliance error on %s: %s", o.get("id"), e)

    def _observe_order_scoring(self, open_orders):
        """Shadow-only batched scoring observation; it never mutates an order."""
        now = time.time()
        if now - self._scoring_checked_at < _SCORING_TTL_SEC:
            return
        buys = [o for o in open_orders if o.get("side") == "BUY" and o.get("id")]
        if not buys:
            return
        self._scoring_checked_at = now
        try:
            result = self.api.are_orders_scoring([o["id"] for o in buys]) or {}
        except Exception as exc:
            logger.warning("order scoring observation failed: %s", exc)
            result = {}
        for order in buys:
            value = result.get(order["id"]) if isinstance(result, dict) else None
            status = "true" if value is True else "false" if value is False else "unknown"
            try:
                self.db.record_scoring_observation(
                    self.wallet_address,
                    order.get("market", ""),
                    order.get("asset_id", ""),
                    order["id"],
                    True,
                    status,
                )
            except Exception as exc:
                logger.warning("record scoring observation failed: %s", exc)

    def _market_rewards(
        self, condition_id: str, ttl: float
    ) -> tuple[float | None, float | None]:
        """(rewards_max_spread 美分, 每日奖励美元),同一次响应解析,TTL 缓存。

        ttl 由调用方(主线程)读好传入,**本方法绝不碰 db**:Step3 的并发预取会在 worker
        线程里调它,而每线程一条 sqlite 连接、建了从不回收(models/database.py),worker
        里访问 db 就是每 5 秒一轮地泄漏连接。

        任一项为 None = 该项取不到(接口失败/字段缺失),调用方各自安全跳过。
        每日奖励的 0.0 与 None 含义不同:0.0=奖励真归零(要撤单),None=取不到(跳过)。
        max_spread 保持 float(不 int 化):实盘存在 3.5/4.5 美分,截断会缩窄奖励区间。
        """
        if not condition_id:
            return None, None
        now = time.time()
        hit = self._rewards_cache.get(condition_id)
        if hit and (now - hit[1]) < ttl:
            return hit[0]
        try:
            items = self.api.get_rewards_for_market(condition_id)
            pair = (extract_max_spread(items), extract_daily_rate(items))
        except Exception as e:
            # 取数和解析都在这个 try 里:坏数据(响应字段形状不对)与接口失败都走这里,
            # 所以别把话说成只是接口失败。
            logger.warning("[Step3] 预取奖励失败(取数或解析)%s: %s", condition_id, e)
            return None, None
        if pair[0] is None:
            return pair  # max_spread 取不到就不写缓存,下轮重试(与旧 _market_max_spread 一致)
        self._rewards_cache[condition_id] = (pair, now)
        return pair

    def _prefetch(self, open_orders, blacklist, ttl):
        """Step3 本轮的盘口 / 奖励并发预取,返回 (books, rewards) 两张表。

        books   = {token_id: 订单簿 dict 或 None}
        rewards = {condition_id: (max_spread, daily_rate)}

        **奖励先取、盘口后取,别调回来。**两拨串行,先取的那拨等到判定时,已经陈旧了后
        取那拨的全部耗时。盘口驱动 recheck_resting_buy——决定要不要撤一个正在吃奖励的
        单——必须最新鲜;而每日奖励是小时级才变的量,差几秒无所谓。奖励端点又正是本项目
        实测「娇气」的那个(烂代理约 30% 连接超时,_retry_on_connect_error 最多重试 3
        次),几个卡住的奖励任务能把那一拨拖到几十秒,这份陈旧不该落在盘口上。

        盘口走**一次批量** get_orderbooks(POST /books),不再每 token 一个请求:
        2026-07-29 实盘 51+ 挂单时这一拨中位 10.7s、p90 17.3s、最大 46.1s,而判定本身
        只要几毫秒(相邻判定日志 90% 间隔 <10ms)—— 撤单延迟(= tick 耗时 + 5s,实测
        p99 22s)几乎全是在等这些单发往返。奖励没有批量端点,仍走并发。

        两拨保持分开、不合成一拨异构任务:分开才有天然的按 token / 按市场去重。

        **books 里的 None 表示取数失败**,与「取到了但买卖盘为空」不是一回事:前者该单
        本轮跳过(等价于旧的取数抛异常路径、不留状态行),后者要走「盘口为空」分支并留
        状态行。两者塌成一个会让监控状态表说谎。

        只为「真要判定的买单」取数:卖单和部分成交单不进判定,黑名单市场的单在取数之前
        就 return,都不必为它们发请求。worker 线程只发网络请求、绝不碰 db(每线程一条
        sqlite 连接且建了不回收),每个任务自带异常处理、绝不外抛。
        """
        t0 = time.time()
        pending = [
            o
            for o in open_orders
            if o.get("side") == "BUY"
            and float(o.get("size_matched", 0) or 0) <= 0
            and o.get("market", "") not in blacklist
        ]
        token_ids = [t for t in {o.get("asset_id", "") for o in pending} if t]
        cids = [c for c in {o.get("market", "") for o in pending} if c]

        rewards = dict(
            zip(
                cids,
                parallel_map(
                    lambda c: self._market_rewards(c, ttl), cids, _STEP3_MAX_WORKERS
                ),
            )
        )
        try:
            books = self.api.get_orderbooks(token_ids)
        except Exception as e:
            # get_orderbooks 内部已按批 try(失败批记 None),走到这里说明整个调用崩了。
            # 与旧的「每个取数任务自带 try、绝不外抛」保持一致:全记 None,该轮各单
            # 走「订单簿取不到,本轮跳过」,绝不让 Step3 抛出去冻住 publish_status。
            logger.warning("[Step3] 批量预取订单簿失败,本轮全部跳过: %s", e)
            books = {t: None for t in token_ids}
        # 干过活的轮才记一条汇总:这次并发化的全部理由是「36 秒降到 6 秒」、最大风险是
        # 6 路把娇气的奖励端点打崩,两件事都只有这条日志看得见(逐项 WARNING 看不出面),
        # 而奖励系统性取不到会静默退化成 fail-open 跳过。
        # 门槛按「本轮有没有要取的东西」,不是「两张表是否都空」:问了却一个都没取回来的轮
        # 恰恰最该看见。本轮无可判定的买单(全是卖单/部分成交/黑名单)则一声不响 —— 否则
        # 5 个钱包空闲过夜就是每分钟 60 条 0/0,这条日志本身就废了。
        if token_ids or cids:
            ok_books = sum(1 for b in books.values() if b is not None)
            ok_rewards = sum(1 for pair in rewards.values() if pair[0] is not None)
            logger.info(
                "[Step3] 预取完成 盘口 %d/%d 奖励 %d/%d 耗时 %.1fs",
                ok_books,
                len(token_ids),
                ok_rewards,
                len(cids),
                time.time() - t0,
            )
        return books, rewards

    def _check_compliance(self, o: dict, blacklist, settings, books, rewards):
        """Decide what to do with a resting buy this tick: first re-check the
        bid-ask spread (cancel if it widened past the threshold), else price
        compliance.

        盘口与奖励由 check_sell_orders 并发预取好传入,本方法只判定、不发取数请求;
        撤单、记账、状态行仍在主线程串行。判定逻辑与顺序与预取化之前完全一致。
        """
        token_id = o.get("asset_id", "")
        cid = o.get("market", "")
        # 黑名单:该市场不应再有在挂买单——撤掉,绝不重挂(覆盖 Step3 重挂这条路径)。
        if cid in blacklist:
            old_price = float(o.get("price", 0) or 0)
            osize = int(float(o.get("original_size", 0) or 0))
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Blacklist cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="blacklist_cancel",
                side="-",
                price=-1,
                size=osize,
                reason="市场在黑名单,撤掉在挂买单且不重挂",
                price_basis="黑名单复查；来源：本地 blacklist 表",
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{old_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="黑名单撤单",
                detail="市场已加入黑名单",
            )
            logger.info("[Step3] blacklist cancel %s market %s", o.get("id"), cid)
            return
        ob = books.get(token_id)
        if ob is None:
            # 预取取不到盘口。等价于旧版 get_orderbook 抛异常被外层吞掉的路径:本轮
            # 跳过该单、不留状态行。「取到了但买卖盘为空」是另一回事,走下面的
            # 「跳过(盘口为空)」分支并留状态行,两者不可混为一谈。
            logger.warning(
                "[Step3] 单 %s 市场 %s | 订单簿取不到,本轮跳过", o.get("id"), cid
            )
            return
        bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        spread_cents = (
            (best_ask - best_bid) * 100
            if (best_bid is not None and best_ask is not None)
            else None
        )

        # Eligibility re-check: only the bid-ask spread now (cancel a resting buy
        # whose market's spread widened past max_spread_cents). None spread = book
        # side missing -> unknown -> keep.
        cancel, reason = recheck_resting_buy(spread_cents, settings)
        if cancel:
            old_price = float(o.get("price", 0) or 0)
            osize = int(float(o.get("original_size", 0) or 0))
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Eligibility cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="eligibility_cancel",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis="买一卖一价差超阈值复查；来源：CLOB get_orderbook",
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{old_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="复查撤单(价差过大)",
                detail=reason,
            )
            logger.info(
                "[Step3] eligibility cancel %s market %s: %s", o.get("id"), cid, reason
            )
            return

        cur_price = float(o.get("price", 0) or 0)
        osize = int(float(o.get("original_size", 0) or 0))
        max_spread, daily_rate = rewards.get(cid, (None, None))

        # 奖励金额实时复查:市场每日奖励跌破门槛 -> 撤买单不重挂,并把实时值写回候选池
        # (不写回的话,30 秒后的下单轮会拿最长 4 小时前的快照把单挂回来,来回打架)。
        # daily_rate 的 0.0 与 None 含义不同:0.0=奖励真归零(撤),None=取不到(跳过)。
        # 排在「盘口为空跳过」和「max_spread 取不到跳过」两道之前:奖励判定根本用不到盘口
        # (薄奖励市场常缺卖单一侧,但没有卖单的在挂买单照样会被别人的市价卖单吃掉,奖励归零
        # 就该撤),而 max_spread 与奖励来自同一次响应,响应里 rewards_max_spread 缺失时
        # 奖励判定也不该被一起跳过。跳过奖励闸门还会连带不写回候选池,下单轮就继续拿旧
        # 快照把单挂回来——这个功能要堵的 4 小时窗口正好又开了。
        min_reward = float(settings.get("min_reward_usd", 0) or 0)
        if daily_rate is not None and daily_rate < min_reward:
            reason = (
                f"实时每日奖励 ${daily_rate:.2f} < 门槛 ${min_reward:.2f}，撤买单不重挂"
            )
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Reward-drop cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="reward_drop_cancel",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis=(
                    f"实时每日奖励=${daily_rate:.2f}；门槛=${min_reward:.2f}；"
                    f"来源：CLOB /rewards/markets/{cid} 实时取数"
                ),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{cur_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="撤单(奖励下降)",
                detail=reason,
            )
            self._notify_reward_update(cid, daily_rate)
            logger.info(
                "[Step3] reward-drop cancel %s market %s: %s", o.get("id"), cid, reason
            )
            return

        # --- 奖励区间合规检查（不重挂，下单引擎在下次下单周期重挂）---
        if not bids or not asks:
            logger.info(
                "[Step3] 单 %s 市场 %s | 盘口为空，本轮跳过",
                o.get("id"),
                cid,
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{float(o.get('price', 0) or 0):.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="跳过(盘口为空)",
                detail="盘口为空",
            )
            return
        midpoint = (best_bid + best_ask) / 2
        if max_spread is None:
            logger.info(
                "[Step3] 单 %s 市场 %s 现价 %.4f | 取不到 rewards_max_spread，"
                "本轮跳过（不撤不重挂）",
                o.get("id"),
                cid,
                float(o.get("price", 0) or 0),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{float(o.get('price', 0) or 0):.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="跳过(取不到max_spread)",
                detail="取不到 rewards_max_spread",
            )
            return
        # Reward band is max_spread CENTS around the midpoint, NOT max_spread
        # ticks (the tick form is ~10x too narrow on 0.1-cent markets).
        rmin, rmax = reward_price_range(midpoint, max_spread)

        # 价格区间护栏：挂单价若超出配置「单价区间」[min,max]美分，撤该买单不重挂。
        # 缺失阈值时安全默认 0/100，使该闸门成为 no-op。
        min_pc = float(settings.get("min_price_cents", 0.0))
        max_pc = float(settings.get("max_price_cents", 100.0))
        if cur_price * 100 < min_pc or cur_price * 100 > max_pc:
            reason = (
                f"挂单价 {cur_price * 100:.1f}c 不在单价区间 "
                f"[{min_pc:.0f}c, {max_pc:.0f}c]，撤买单不重挂"
            )
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Price-band cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="price_band_cancel",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis=(
                    f"挂单价={cur_price:.4f}；单价区间[{min_pc:.0f}c,{max_pc:.0f}c]；"
                    f"来源：CLOB get_orderbook + get_rewards_for_market + 设置"
                ),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{cur_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="撤单(价格超区间)",
                detail=reason,
            )
            logger.info(
                "[Step3] price-band cancel %s market %s: %s", o.get("id"), cid, reason
            )
            return

        # 挂单价不在奖励区间 → 撤单不重挂（下单引擎下次 place_orders 重挂）。
        if not (rmin <= cur_price <= rmax):
            reason = (
                f"挂单价 {cur_price:.4f} 不在奖励区间 "
                f"[{rmin:.4f},{rmax:.4f}]，撤买单不重挂"
            )
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Cancel out-of-band %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="step3_cancel_outofband",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis=(
                    f"挂单价={cur_price:.4f}；奖励区间[{rmin:.4f},{rmax:.4f}] "
                    f"mid{midpoint:.4f} ms{max_spread}；"
                    f"来源：CLOB get_orderbook + get_rewards_for_market"
                ),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{cur_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="撤单(价格出奖励区间)",
                detail=reason,
            )
            logger.info(
                "[Step3] out-of-band cancel %s market %s | price %.4f not in [%.4f,%.4f]",
                o.get("id"),
                cid,
                cur_price,
                rmin,
                rmax,
            )
            return

        # 悬崖复查：奖励区间下沿往下 N¢ 内无买档支撑 → 撤买单不重挂（cancel-only）。
        # 与下单时的判定共用 has_cliff_below，两处口径不会漂。之所以在挂单也要复查：下单轮
        # 在预算被持仓吃空时会整市场 continue（manager.py 的「预算不足：活跃侧保持不动」），
        # 那条路径下在挂买单永远走不到下单轮的悬崖判定，只有这里够得着。
        cliff_cents = float(settings.get("cliff_probe_cents", 2) or 0)
        if has_cliff_below(bids, rmin, cliff_cents):
            reason = f"奖励区间下方 {cliff_cents:g}¢ 内无买档支撑（悬崖），撤买单不重挂"
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Cliff cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="cliff_cancel",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis=(
                    f"奖励区间下沿={rmin:.4f}；探测带"
                    f"[{rmin - cliff_cents * 0.01:.4f},{rmin:.4f}) 内无买档；"
                    f"来源：CLOB get_orderbook"
                ),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{cur_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="撤单(悬崖)",
                detail=reason,
            )
            logger.info(
                "[Step3] cliff cancel %s market %s | 下沿 %.4f 往下 %g¢ 无买档",
                o.get("id"),
                cid,
                rmin,
                cliff_cents,
            )
            return

        # 挂单价在奖励区间内 → 保持不动。
        logger.info(
            "[Step3] 单 %s 市场 %s 现价 %.4f | bid %.4f ask %.4f mid %.4f "
            "max_spread=%s 区间[%.4f,%.4f] | keep",
            o.get("id"),
            cid,
            cur_price,
            best_bid,
            best_ask,
            midpoint,
            max_spread,
            rmin,
            rmax,
        )
        self._status_add(
            market=cid,
            side="买入",
            price=f"{cur_price:.4f}",
            size=str(o.get("original_size", "")),
            matched=str(o.get("size_matched", "0")),
            stage="Step3",
            action="keep → 保持不动",
            detail=(
                f"bid{best_bid:.4f} ask{best_ask:.4f} mid{midpoint:.4f} "
                f"ms{max_spread} 区间[{rmin:.4f},{rmax:.4f}]"
            ),
        )
