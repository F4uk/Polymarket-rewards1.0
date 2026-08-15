# engine/exit_router.py
"""快速退出路由器:Reward BUY 成交后的库存周转(Maker → 比价 → Merge/Protected FAK)。

流程(每 tick,在监控的离场步内逐仓调用):
    Reward BUY 成交 -> 实际持仓 + FIFO -> 已有 YES+NO 整对?先 Pair Merge -> 残差
    单侧:短 Maker 卖 -> 成交即平;超时/触发止损 -> 比价
        A. FOK 补对 + Merge   vs   B. Protected FAK 卖出
        执行更优路线。

关键不变量:
- Maker 等待截止时间 = 最老一笔在持 FIFO 买入成交的交易所时间戳 + exit_maker_wait_sec,
  部分成交/重启都不重置(deadline 由 get_trades 重建,FIFO 最早的一笔决定)。
- 现有止损阈值命中 -> 跳过 Maker 等待,立即比价离场。
- FOK 补对:先在 merge_events 表预留 READY 事件,再发外部 FOK;delayed/未决绝不发第二笔
  FOK;每 tick 先对账真实余额/订单状态再行动。
- 只有 CONFIRMED 的 Merge 计入盈亏;确认只做一次(幂等)。
- 实际账户状态永远优先于本地假设(手工卖出/手工补对/重启都按真实余额走)。

本模块不持有网络外的全局状态;每钱包一个实例,由 OrderMonitor 注入回调使用。
"""

import logging
import os
import time

from engine.merge_accounting import consume_fifo_cost, merge_realized_pnl, pair_amount
from engine.merge import merge_recovery_usd
from engine.protected_exit import maker_sell_price, protected_fak_plan, quote_direct_exit, quote_merge_route
from engine.take_profit import effective_theta_stop
from engine import exit_status
from api.polymarket_api import OrderRejected

logger = logging.getLogger(__name__)

# Relayer 轮询退避:确认窗口内每 30s 一次;异常后 120s;超时未决后降到 300s。
_POLL_INTERVAL = 30.0
_POLL_FAIL_BACKOFF = 120.0
_POLL_STALL_BACKOFF = 300.0

# Relayer 交易状态(官方 py-builder-relayer-client 的 RelayerTransactionState 字符串值)。
_TX_DONE = {"STATE_EXECUTED", "STATE_MINED", "STATE_CONFIRMED"}
_TX_FAILED = {"STATE_FAILED"}


def _num(v, default=0.0):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default


class ExitRouter:
    """单钱包快速退出路由器(依赖注入:所有网络/DB 副作用经 api/db/回调)。"""

    def __init__(
        self,
        api,
        db,
        wallet_address: str,
        *,
        cost_lots=None,
        sell_book=None,
        status_add=None,
        record_action=None,
        now=None,
    ):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        self._cost_lots_fn = cost_lots
        self._sell_book_fn = sell_book
        self._status_add = status_add or (lambda **kw: None)
        self._record_action = record_action or (lambda *a, **kw: None)
        self._now = now or time.time
        # asset_id -> {"order_id", "price", "size", "placed_at"} 每仓至多一张托管卖单。
        self._managed_sells: dict = {}
        # asset_id -> 上次见到的持仓量(识别托管卖单成交 -> 平仓动作记录)。
        self._last_sizes: dict = {}
        # event_id -> 下次允许轮询 Relayer 的时间(内存退避,重启后自然从头来)。
        self._poll_after: dict = {}
        # 本 tick 数据(positions/open_orders/模板),route_position 与 reconcile 共用。
        self._positions = []
        self._open_orders = []
        self._positions_by_cid: dict = {}
        self._tmpl = None
        self._tick_ts = 0.0
        self._relayer_status = None  # ("ok"|"missing"|"error", 消毒原因)
        self._funder_cached = None
        # 本 tick 状态快照(asset/cid 级),_publish_status 汇总后交给 exit_status。
        self._snap_assets: dict = {}
        self._snap_conditions: dict = {}

    # ---------------------------------------------------------------- 模板/辅助

    def _template(self) -> dict:
        if self._tmpl is None:
            try:
                self._tmpl = self.db.get_template_for(self.wallet_address)
            except Exception:
                self._tmpl = {}
        return self._tmpl

    def _funder(self) -> str:
        if self._funder_cached is None:
            self._funder_cached = self.api.get_funder()
        return self._funder_cached

    def _action(self, market_id, action_type, side, price, size, reason, basis=""):
        """记一条动作(绝不抛)。"""
        try:
            self._record_action(market_id, action_type, side, price, size, reason, basis)
        except Exception as e:
            logger.warning("exit action record failed: %s", e)

    def _stop_hit(self, cost: float, best_bid, tmpl: dict) -> bool:
        """现有止损阈值(模板配置,口径与上游一致)。命中 -> 跳过 Maker 等待。"""
        if best_bid is None:
            return False
        mode = tmpl.get("stop_loss_mode", "percent")
        percent = tmpl.get("stop_loss_percent", 20)
        cents = tmpl.get("theta_stop_cents", 5)
        theta = effective_theta_stop(cost, mode, percent, cents)
        return theta is not None and (cost - best_bid) >= theta

    def _oldest_lot_ts(self, lots) -> float:
        """最老一笔在持 FIFO 买入成交的交易所时间戳(决定 Maker 等待截止)。"""
        ts = None
        for l in lots or []:
            t = _num(l.get("ts"))
            if t > 0 and (ts is None or t < ts):
                ts = t
        return ts

    def _pair_for(self, condition_id: str, asset_id: str):
        """同市场另一侧持仓 (asset_id, size);找不到返回 None。"""
        entries = self._positions_by_cid.get(condition_id) or []
        for other in entries:
            if other.get("asset") != asset_id and _num(other.get("size")) > 0:
                return (other.get("asset", ""), _num(other.get("size")))
        return None

    def _relayer_credentials_ok(self) -> bool:
        """Relayer 凭证是否可用(环境变量)。只读环境,不触网;结果每 tick 缓存。"""
        if self._relayer_status is not None:
            return self._relayer_status[0] == "ok"
        key = os.environ.get("POLY_BUILDER_API_KEY")
        secret = os.environ.get("POLY_BUILDER_SECRET")
        passphrase = os.environ.get("POLY_BUILDER_PASSPHRASE")
        if not (key and secret and passphrase):
            self._relayer_status = (
                "missing",
                "未配置 POLY_BUILDER_API_KEY/SECRET/PASSPHRASE 环境变量, Merge 路由不可用",
            )
        else:
            self._relayer_status = ("ok", "")
        return self._relayer_status[0] == "ok"

    # ---------------------------------------------------------------- 对外入口

    def reconcile_pending(self, positions, open_orders):
        """每 tick 开头:对账实际持仓/挂单 + 恢复活动 Merge 操作 + 发布状态快照。

        任何一步失败都不抛(退出流程继续);真实余额永远优先于本地假设。
        """
        self._tick_ts = self._now()
        self._positions = positions or []
        self._open_orders = open_orders or []
        self._tmpl = None
        self._snap_assets = {}
        self._snap_conditions = {}
        self._positions_by_cid = {}
        for p in self._positions:
            if _num(p.get("size")) > 0:
                self._positions_by_cid.setdefault(p.get("conditionId", ""), []).append(p)
        try:
            self._reconcile_managed_sells()
        except Exception as e:
            logger.warning("exit: managed sells reconcile failed: %s", e)
        try:
            self._reconcile_events()
        except Exception as e:
            logger.warning("exit: merge events reconcile failed: %s", e)
        try:
            self._publish_status()
        except Exception as e:
            logger.warning("exit: status publish failed: %s", e)

    def route_position(self, pos, open_orders) -> bool:
        """处理一个持仓的快速退出;返回 True=本仓由快速退出接管(上游不再处理)。

        任何异常都返回 False 落到上游行为(裸奔告警/原离场),绝不让路由错误把仓晾着。
        """
        try:
            self._open_orders = open_orders or self._open_orders
            handled = self._route_position(pos)
            # 路由完成后立刻发布状态(UI 不滞后一个 tick);reconcile 开头的发布只覆盖
            # 本 tick 未路由的仓(成本未知/上游接管)。
            try:
                self._publish_status()
            except Exception:
                pass
            return handled
        except Exception as e:
            logger.warning("exit router error on %s: %s", pos.get("asset"), e, exc_info=True)
            return False

    # ---------------------------------------------------------------- 主路由

    def _route_position(self, pos) -> bool:
        asset_id = pos.get("asset", "")
        size = _num(pos.get("size"))
        cid = pos.get("conditionId", "")
        if size <= 0 or not cid:
            return False
        tmpl = self._template()
        exit_wait = _num(tmpl.get("exit_maker_wait_sec", 60), 60)
        merge_enabled = bool(tmpl.get("merge_enabled", False))
        if exit_wait <= 0 and not merge_enabled:
            return False  # 快速退出整体关闭 -> 上游行为
        # 活动 Merge 操作存在时:该市场不重复路由(残差等 Merge 完成后再处理)。
        try:
            active = self.db.get_active_merge_event(self.wallet_address, cid)
        except Exception:
            active = None
        if active:
            self._mark_asset(asset_id, active, cid)
            return True
        cost, lots = self._cost_lots_fn(asset_id, size, cid)
        if cost is None or cost <= 0:
            return False  # 成本取不到 -> 上游跳过+裸奔告警,下 tick 自愈
        tick, tick_str, best_bid, best_ask = self._sell_book_fn(asset_id)
        stop_hit = self._stop_hit(cost, best_bid, tmpl)
        if merge_enabled:
            pair = self._pair_for(cid, asset_id)
            if pair:
                handled = self._pair_merge_route(pos, pair, size, cost, lots, tick_str, tmpl)
                if handled:
                    return True
                # 不够最小份数 / Relayer 不可用:落到单侧路由(Maker/FAK 照常周转)。
        if stop_hit:
            return self._route_compare(pos, size, cost, lots, tick, tick_str, best_bid, tmpl)
        if exit_wait <= 0:
            return self._route_compare(pos, size, cost, lots, tick, tick_str, best_bid, tmpl)
        return self._maker_route(pos, size, cost, lots, tick, tick_str, best_bid, best_ask, tmpl, exit_wait)

    # ---------------------------------------------------------------- Pair Merge

    def _pair_merge_route(self, pos, pair, size, cost, lots, tick_str, tmpl) -> bool:
        asset_id = pos.get("asset", "")
        cid = pos.get("conditionId", "")
        other_asset, other_size = pair
        q = pair_amount(size, other_size)
        min_shares = _num(tmpl.get("merge_min_shares", 1), 1)
        if q < min_shares:
            self._mark_asset(asset_id, None, cid, note="pair<min")
            return False  # 不够最小 Merge 量:两侧走单侧路由
        if not self._relayer_credentials_ok():
            self._mark_asset(asset_id, None, cid, note="relayer")
            return False
        try:
            event_id = self.db.create_merge_event(
                wallet=self.wallet_address,
                condition_id=cid,
                route="PAIR",
                amount=q,
                yes_token_id=asset_id,
                no_token_id=other_asset,
                yes_cost=self._fifo_cost(lots, q),
                no_cost=self._fifo_cost(self._other_lots(other_asset, other_size, cid), q),
            )
        except Exception:
            event_id = None  # 已有活动事件或写入失败:交给 reconcile
        if event_id is None:
            self._mark_asset(asset_id, None, cid, note="merge-active")
            return True
        self._action(
            cid, "merge_pair_ready", "买入", -1, q,
            f"已有 YES+NO 整对 {q:g} 份,预留 Pair Merge(先撤冲突卖单、再提交 Relayer)",
        )
        self._mark_asset(asset_id, None, cid, note="pair-ready")
        self._submit_merge(event_id, cid, asset_id, other_asset, q, tick_str)
        return True

    def _fifo_cost(self, lots, qty) -> float:
        c, _ = consume_fifo_cost(lots, qty)
        return c if c is not None else 0.0

    def _other_lots(self, other_asset, other_size, cid):
        try:
            return self._cost_lots_fn(other_asset, other_size, cid)[1]
        except Exception:
            return []

    # ---------------------------------------------------------------- Maker 卖

    def _maker_route(
        self, pos, size, cost, lots, tick, tick_str, best_bid, best_ask, tmpl, exit_wait
    ) -> bool:
        asset_id = pos.get("asset", "")
        cid = pos.get("conditionId", "")
        oldest = self._oldest_lot_ts(lots)
        if oldest is None:
            oldest = self._tick_ts
        deadline = oldest + exit_wait
        remaining = deadline - self._now()
        managed = self._managed_sells.get(asset_id)
        if managed and self._order_open(managed["order_id"]):
            if remaining > 0:
                self._mark_asset(asset_id, None, cid, maker=exit_status.maker_wait_text(remaining))
            else:
                self._mark_asset(asset_id, None, cid, maker="MAKER · 到期")
                # 到期但单还在挂:撤掉走比价(FAK/FOK+Merge)。
                self._cancel_sells(asset_id, cid, size)
                self._managed_sells.pop(asset_id, None)
                return self._route_compare(pos, size, cost, lots, tick, tick_str, best_bid, tmpl)
            return True
        if remaining <= 0:
            return self._route_compare(pos, size, cost, lots, tick, tick_str, best_bid, tmpl)
        price = maker_sell_price(None, best_ask)
        if price is None:
            self._mark_asset(asset_id, None, cid, maker="MAKER · 无卖一")
            return True
        # 至少维持一张托管卖单:先撤旧/冲突卖单。
        self._cancel_sells(asset_id, cid, size)
        try:
            resp = self.api.place_limit_sell(
                asset_id, price, size, tick_size=tick_str, order_type="GTC", post_only=True
            )
        except OrderRejected as e:
            # post-only 被拒 = 簿已移动:下 tick 重算,绝不压价追单。
            logger.info("exit maker post-only rejected %s: %s", asset_id, e)
            self._mark_asset(asset_id, None, cid, maker="MAKER · 簿动重试")
            return True
        except Exception as e:
            logger.warning("exit maker sell failed %s: %s", asset_id, e)
            self._mark_asset(asset_id, None, cid, maker="MAKER · 失败")
            return True
        order_id = resp.get("orderID", "")
        if order_id:
            self._managed_sells[asset_id] = {
                "order_id": order_id, "price": price, "size": size, "placed_at": self._now(),
            }
        self._action(
            cid, "exit_maker", "卖出", price, size,
            f"快速退出:挂卖一 {price:.4f} 做 Maker(等待至 FIFO 最老买入 +{exit_wait:g}s)",
        )
        self._mark_asset(asset_id, None, cid, maker=exit_status.maker_wait_text(remaining))
        return True

    def _order_open(self, order_id: str) -> bool:
        return any(o.get("id") == order_id for o in self._open_orders)

    def _cancel_sells(self, asset_id, cid, size):
        """撤掉该仓全部在挂卖单(托管 + 冲突),保证 FAK/Merge 前份额不被占用。"""
        ids = [
            o.get("id")
            for o in self._open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL" and o.get("id")
        ]
        if not ids:
            return
        try:
            self.api.cancel_orders(ids)
            self._action(
                cid, "exit_cancel_sell", "-", -1, size,
                f"快速退出:撤 {len(ids)} 笔冲突挂卖单",
            )
        except Exception as e:
            logger.warning("exit cancel sells %s failed: %s", asset_id, e)
        self._open_orders = [
            o for o in self._open_orders
            if not (o.get("asset_id") == asset_id and o.get("side") == "SELL")
        ]
        self._managed_sells.pop(asset_id, None)

    # ---------------------------------------------------------------- 比价路由

    def _route_compare(self, pos, size, cost, lots, tick, tick_str, best_bid, tmpl) -> bool:
        """Maker 超时/止损命中:比价 FOK+Merge vs Protected FAK,执行更优路线。"""
        asset_id = pos.get("asset", "")
        cid = pos.get("conditionId", "")
        try:
            ob = self.api.get_orderbook(asset_id)
        except Exception as e:
            logger.warning("exit route orderbook failed %s: %s", asset_id, e)
            self._mark_asset(asset_id, None, cid, note="无盘口")
            return True
        bids = sorted(ob.get("bids", []), key=lambda b: _num(b.get("price")), reverse=True)
        direct = quote_direct_exit(bids, size)
        if direct is None:
            self._mark_asset(asset_id, None, cid, note="无买盘")
            return True
        merge_enabled = bool(tmpl.get("merge_enabled", False))
        relayer_ok = self._relayer_credentials_ok() if merge_enabled else False
        if relayer_ok:
            neg_risk = self._neg_risk(asset_id)
            comp_token = self._complement_token(cid, asset_id)
            if comp_token:
                try:
                    cob = self.api.get_orderbook(comp_token)
                except Exception as e:
                    logger.warning("exit complement book failed %s: %s", comp_token, e)
                    cob = None
                if cob:
                    c_asks = sorted(cob.get("asks", []), key=lambda a: _num(a.get("price")))
                    mq = quote_merge_route(
                        c_asks,
                        size,
                        direct["expected_recovery"],
                        _num(tmpl.get("merge_min_advantage_usd", 0.01), 0.01),
                        _num(tmpl.get("merge_min_shares", 1), 1),
                    )
                    if mq["feasible"]:
                        self._mark_asset(asset_id, None, cid, note="fok-merge")
                        self._start_fok_merge(
                            cid, asset_id, comp_token, neg_risk, size,
                            mq["fok_worst_price"], tick_str, tmpl,
                        )
                        return True
        # 直接路线:Protected FAK(带最坏价保护)。
        self._protected_fak(pos, size, cost, tick_str, direct)
        return True

    def _neg_risk(self, asset_id: str) -> bool:
        try:
            return bool(self.api.get_neg_risk(asset_id))
        except Exception:
            return False

    def _complement_token(self, cid: str, asset_id: str):
        """同市场另一侧 token:先看实际持仓(手工补对),再看扫描缓存(奖励市场两侧
        都在 eligible_markets 里);都拿不到返回 None(FOK 路由降级为直接 FAK)。"""
        pair = self._pair_for(cid, asset_id)
        if pair:
            return pair[0]
        try:
            for row in self.db.get_eligible_markets():
                if row.get("market_id") == cid and row.get("token_id") != asset_id:
                    return row.get("token_id", "")
        except Exception as e:
            logger.warning("exit: eligible lookup for complement failed: %s", e)
        return None

    # ---------------------------------------------------------------- FOK+Merge

    def _start_fok_merge(
        self, cid, asset_id, comp_token, neg_risk, size, worst_price, tick_str, tmpl
    ):
        """预留 FOK_MERGE 事件 -> 提交 exact-q FOK 补对买单(全成或全不成)。

        事件预留必须在外部 FOK 之前(防崩溃/重启后重复补对)。delayed/未决 -> 不再
        发第二笔 FOK,交给每 tick 的 reconcile 对账。
        """
        try:
            event_id = self.db.create_merge_event(
                wallet=self.wallet_address,
                condition_id=cid,
                route="FOK_MERGE",
                amount=size,
                yes_token_id=asset_id,
                no_token_id=comp_token,
                yes_cost=self._fifo_cost(self._cost_lots_fn(asset_id, size, cid)[1], size),
                no_cost=0.0,
            )
        except Exception:
            self._mark_asset(asset_id, None, cid, note="merge-active")
            return
        self._action(
            cid, "merge_fok_submit", "买入", worst_price, size,
            f"比价后选 FOK+Merge:补 {comp_token[:10]}… 最坏价 {worst_price:.4f}(动态上限)",
        )
        self._mark_asset(asset_id, None, cid, note="fok")
        try:
            resp = self.api.place_fok_buy(comp_token, worst_price, size, tick_size=tick_str)
        except OrderRejected as e:
            self._fail_event(event_id, f"FOK 被拒(全成或全不成): {e}")
            return
        status = str((resp or {}).get("status", "")).lower()
        order_id = (resp or {}).get("orderID", "")
        if order_id:
            self.db.update_merge_event(event_id, complement_order_id=order_id)
        if status == "matched":
            self.db.update_merge_event(event_id, complement_status="matched")
            self._action(cid, "merge_fok_matched", "买入", worst_price, size,
                         "FOK 补对已成交,待余额核实后提交 Merge")
            return
        if status == "delayed":
            self.db.update_merge_event(event_id, complement_status="delayed")
            self._action(cid, "merge_fok_submit", "买入", worst_price, size,
                         "FOK 补对延迟确认,对账后继续(绝不重复补对)")
            return
        self._fail_event(event_id, f"FOK 状态异常: {resp}")

    # ---------------------------------------------------------------- 事件对账

    def _reconcile_events(self):
        """恢复/推进本钱包全部活动 Merge 操作(READY/SUBMITTED)。"""
        active = []
        try:
            for status in ("READY", "SUBMITTED"):
                rows, _ = self.db.list_merge_events(
                    wallet=self.wallet_address, statuses=[status], limit=100
                )
                active.extend(rows)
        except Exception as e:
            logger.warning("exit: list merge events failed: %s", e)
            return
        for ev in active:
            try:
                self._reconcile_event(ev)
            except Exception as e:
                logger.warning("exit: reconcile event %s failed: %s", ev.get("id"), e)

    def _reconcile_event(self, ev):
        status = ev.get("status")
        route = ev.get("route")
        cid = ev.get("condition_id", "")
        eid = ev.get("id")
        if status == "READY" and route == "FOK_MERGE":
            if not ev.get("complement_order_id"):
                # 预留后 FOK 从未提交(崩溃窗口):真实余额说了算。
                if self._pair_balance_ok(ev):
                    self._submit_merge(eid, cid, ev.get("yes_token_id", ""),
                                       ev.get("no_token_id", ""), ev.get("amount", 0),
                                       self._tick_str_of(ev))
                else:
                    self._fail_event(eid, "FOK 未提交且无补对余额(重启恢复:取消预留)")
                return
            self._reconcile_fok_order(ev)
            return
        if status == "READY" and route == "PAIR":
            # 预留后 Merge 未提交(崩溃窗口):整对还在 -> 补提交;不在 -> 真实状态优先。
            if self._pair_balance_ok(ev):
                self._submit_merge(eid, cid, ev.get("yes_token_id", ""),
                                   ev.get("no_token_id", ""), ev.get("amount", 0),
                                   self._tick_str_of(ev))
            else:
                self._fail_event(eid, "Pair 余额已不在,按真实状态取消预留")
            return
        if status == "SUBMITTED":
            self._poll_merge(ev)
            return

    def _reconcile_fok_order(self, ev):
        eid = ev.get("id")
        order_id = ev.get("complement_order_id", "")
        cid = ev.get("condition_id", "")
        if not order_id:
            return
        try:
            o = self.api.get_order(order_id)
        except Exception as e:
            logger.warning("exit: FOK order query %s failed: %s", order_id, e)
            return
        ostatus = str((o or {}).get("status", "")).lower()
        matched = _num((o or {}).get("size_matched"))
        filled = matched >= _num(ev.get("amount")) - 1e-9
        if filled or "match" in ostatus or "fill" in ostatus:
            if filled or self._pair_balance_ok(ev):
                self.db.update_merge_event(eid, complement_status="matched")
                self._action(cid, "merge_fok_matched", "买入", -1, ev.get("amount", 0),
                             "FOK 补对确认成交(延迟后核实),提交 Merge")
                self._submit_merge(eid, cid, ev.get("yes_token_id", ""),
                                   ev.get("no_token_id", ""), ev.get("amount", 0),
                                   self._tick_str_of(ev))
                return
        if "cancel" in ostatus or "unmatch" in ostatus or "expire" in ostatus:
            # 订单已死:真实余额再确认一次(成交可能仍发生了)。
            if self._pair_balance_ok(ev):
                self._action(cid, "merge_fok_matched", "买入", -1, ev.get("amount", 0),
                             "FOK 订单已取消但余额证实补对已到,提交 Merge")
                self._submit_merge(eid, cid, ev.get("yes_token_id", ""),
                                   ev.get("no_token_id", ""), ev.get("amount", 0),
                                   self._tick_str_of(ev))
            else:
                self._fail_event(eid, "FOK 未成交(订单已取消),补对失败")
            return
        # 仍 pending/delayed:等待,绝不发第二笔 FOK。
        self.db.update_merge_event(eid, complement_status="delayed")

    def _pair_balance_ok(self, ev) -> bool:
        """实际余额是否已构成可 Merge 的整对(真实状态优先)。"""
        cid = ev.get("condition_id", "")
        yes_asset = ev.get("yes_token_id", "")
        no_asset = ev.get("no_token_id", "")
        if not (yes_asset and no_asset):
            return False
        yes_size = no_size = 0.0
        for p in self._positions:
            if p.get("conditionId") != cid:
                continue
            if p.get("asset") == yes_asset:
                yes_size = _num(p.get("size"))
            elif p.get("asset") == no_asset:
                no_size = _num(p.get("size"))
        return pair_amount(yes_size, no_size) >= _num(ev.get("amount")) - 1e-9

    def _tick_str_of(self, ev):
        for token in (ev.get("yes_token_id", ""), ev.get("no_token_id", "")):
            if token:
                try:
                    return self._sell_book_fn(token)[1]
                except Exception:
                    pass
        return "0.01"

    # ---------------------------------------------------------------- Merge 提交/确认

    def _submit_merge(self, event_id, cid, yes_asset, no_asset, amount, tick_str):
        """预留后提交 Merge 交易(必要时先批量授权)。失败不改变事件状态 -> 下 tick 重试。"""
        # Merge 前撤掉两侧冲突挂卖单:整对份额不能被卖单占用,否则链上烧不动。
        try:
            self._cancel_sells(yes_asset, cid, amount)
            self._cancel_sells(no_asset, cid, amount)
        except Exception as e:
            logger.warning("exit: pre-merge sell cancel failed: %s", e)
        neg_risk = self._neg_risk(yes_asset or no_asset)
        try:
            approved = self.api.is_merge_adapter_approved(self._funder(), neg_risk)
        except Exception as e:
            logger.warning("exit: adapter approval check failed: %s", e)
            approved = False
        try:
            resp = self.api.merge_positions(cid, amount, neg_risk, approval_batch=not approved)
        except Exception as e:
            # 失败 -> 事件保持 READY,退避后由 reconcile 重试(绝不重复提交已成功的事务:
            # 只有拿到 transaction_id 才算提交过)。
            self.db.update_merge_event(
                event_id, error_message=str(e)[:300], complement_status="submit-failed"
            )
            self._poll_after[event_id] = self._now() + _POLL_FAIL_BACKOFF
            logger.warning("exit: merge submit failed ev=%s: %s", event_id, e)
            self._action(cid, "merge_failed", "-", -1, amount,
                         f"Merge 提交失败(稍后重试): {e}")
            return
        tx_id = (resp or {}).get("transaction_id", "")
        if not tx_id:
            self._fail_event(event_id, "Merge 提交无 transaction_id")
            return
        self.db.update_merge_event(
            event_id, status="SUBMITTED", relayer_tx_id=tx_id,
            complement_status="merged-submitted",
        )
        self._poll_after[event_id] = self._now() + _POLL_INTERVAL
        self._action(cid, "merge_submit", "-", -1, amount,
                     f"Merge 已提交 Relayer(tx={tx_id[:12]}…),等待链上确认")

    def _poll_merge(self, ev):
        eid = ev.get("id")
        now = self._now()
        if now < self._poll_after.get(eid, 0):
            return
        tx_id = ev.get("relayer_tx_id", "")
        if not tx_id:
            self._poll_after[eid] = now + _POLL_INTERVAL
            return
        try:
            rows = self.api.get_merge_transaction_state(tx_id)
        except Exception as e:
            logger.warning("exit: relayer tx query %s failed: %s", tx_id, e)
            self._poll_after[eid] = now + _POLL_FAIL_BACKOFF
            return
        state = str((rows[0] or {}).get("state", "")) if rows else ""
        if state in _TX_DONE:
            self._finalize_confirmed(ev)
            return
        if state in _TX_FAILED:
            # 链上失败:整对若还在 -> 标 FAILED,下 tick 由 Pair Merge 重试;不在 -> 真状态。
            if self._pair_balance_ok(ev):
                self._fail_event(eid, f"Relayer 交易失败({state})")
            else:
                self._finalize_confirmed(ev)
            return
        # 未决/未知:确认窗口内继续轮询;超时后放慢,绝不重复提交。
        timeout = _num(self._template().get("merge_confirm_timeout_sec", 120), 120)
        if now - _num(ev.get("updated_at")) > timeout:
            if not self._pair_balance_ok(ev):
                # 超时但整对已不在:真实状态 = 已 Merge,按预留金额结账。
                self._finalize_confirmed(ev)
                return
            self._poll_after[eid] = now + _POLL_STALL_BACKOFF
            logger.info("exit: merge tx %s 超时未决,放慢轮询(不重复提交)", tx_id)
            return
        self._poll_after[eid] = now + _POLL_INTERVAL

    def _finalize_confirmed(self, ev):
        """把 SUBMITTED/READY 事件结账为 CONFIRMED(幂等:只处理一次)。"""
        eid = ev.get("id")
        cur = self.db.get_merge_event(eid)
        if not cur or cur.get("status") == "CONFIRMED":
            return
        amount = _num(cur.get("amount"))
        recovered = merge_recovery_usd(amount)
        pnl = merge_realized_pnl(amount, _num(cur.get("yes_cost")), _num(cur.get("no_cost")))
        self.db.update_merge_event(
            eid, status="CONFIRMED", recovered_usd=recovered, realized_pnl=pnl,
            confirmed_at=self._now(), error_message="",
        )
        self._action(
            cur.get("condition_id", ""), "merge_confirmed", "-", -1, amount,
            f"Merge 确认:{amount:g} 份整对 -> 回收 ${recovered:.2f},盈亏 ${pnl:.2f}",
        )
        logger.info(
            "Merge CONFIRMED wallet=%s cid=%s amount=%g pnl=%.4f",
            self.wallet_address, cur.get("condition_id", ""), amount, pnl,
        )

    def _fail_event(self, event_id, reason):
        cur = self.db.get_merge_event(event_id)
        if not cur or cur.get("status") == "FAILED":
            return
        self.db.update_merge_event(event_id, status="FAILED", error_message=reason[:300])
        self._action(
            cur.get("condition_id", ""), "merge_failed", "-", -1, cur.get("amount", 0),
            f"Merge 终止:{reason}",
        )
        self._poll_after.pop(event_id, None)

    # ---------------------------------------------------------------- Protected FAK

    def _protected_fak(self, pos, size, cost, tick_str, direct):
        asset_id = pos.get("asset", "")
        cid = pos.get("conditionId", "")
        self._cancel_sells(asset_id, cid, size)
        try:
            resp = self.api.place_marketable_limit_sell(
                asset_id, direct["worst_price"], size, tick_size=tick_str
            )
        except OrderRejected as e:
            logger.warning("exit protected FAK rejected %s: %s", asset_id, e)
            self._mark_asset(asset_id, None, cid, note="FAK被拒")
            return
        except Exception as e:
            logger.warning("exit protected FAK failed %s: %s", asset_id, e)
            self._mark_asset(asset_id, None, cid, note="FAK失败")
            return
        order_id = (resp or {}).get("orderID", "")
        matched = None
        if order_id:
            try:
                o = self.api.get_order(order_id)
                matched = _num((o or {}).get("size_matched"))
            except Exception:
                matched = None
        full = direct["full"]
        if matched is not None:
            full = matched >= size - 1e-9
            partial = 0 < matched < size - 1e-9
        else:
            partial = not full
        action = "exit_protected_fak" if full else "exit_protected_fak_partial"
        label = "PROTECTED FAK" if full else "FAK 部分成交"
        basis = (
            f"保护价 {direct['worst_price']:.4f}(FAK 限价,绝不成交低于此价);"
            f"期望回收 ${direct['expected_recovery']:.2f}"
        )
        self._action(
            cid, action, "卖出", direct["worst_price"], size,
            f"快速退出:受保护 FAK 卖出(最坏价 {direct['worst_price']:.4f})"
            + ("· 全部成交" if full else "· 部分成交,残差下 tick 再处理"),
            basis,
        )
        self._mark_asset(asset_id, None, cid, note=label)

    # ---------------------------------------------------------------- 托管卖单对账

    def _reconcile_managed_sells(self):
        """对账托管卖单:消失 + 持仓平 -> exit_maker_filled;消失 + 持仓在 -> 重挂。"""
        for asset_id in list(self._managed_sells.keys()):
            managed = self._managed_sells[asset_id]
            if self._order_open(managed["order_id"]):
                continue
            size_now = 0.0
            for p in self._positions:
                if p.get("asset") == asset_id:
                    size_now = _num(p.get("size"))
            last = self._last_sizes.get(asset_id)
            if size_now <= 0:
                self._action(
                    self._cid_of(asset_id), "exit_maker_filled", "卖出",
                    managed.get("price", -1), last or 0,
                    "快速退出 Maker 卖单成交,持仓已平",
                )
            self._managed_sells.pop(asset_id, None)
        # 更新各资产上次持仓量(识别手工卖出/FAK 部分成交)。
        for p in self._positions:
            self._last_sizes[p.get("asset", "")] = _num(p.get("size"))

    def _cid_of(self, asset_id) -> str:
        for p in self._positions:
            if p.get("asset") == asset_id:
                return p.get("conditionId", "")
        return ""

    # ---------------------------------------------------------------- 状态快照

    def _mark_asset(self, asset_id, event, cid, note="", maker=""):
        """把本仓状态写进本 tick 快照(asset 级),供 UI 读取。"""
        try:
            text = ""
            if event:
                text = exit_status.merge_status_text(
                    event.get("route"), event.get("status"),
                    event.get("complement_status", ""),
                )
            elif maker:
                text = maker
            elif note:
                text = {
                    "pair-ready": "PAIR MERGE · READY",
                    "pair<min": "PAIR < 最小份数",
                    "relayer": "MERGE 不可用·Relayer 未配置",
                    "merge-active": "MERGE 处理中",
                    "fok-merge": "FOK 补对",
                    "fok": "FOK 补对·待确认",
                    "无盘口": "无盘口",
                    "无买盘": "PROTECTED FAK · 无买盘",
                    "FAK被拒": "PROTECTED FAK · 被拒",
                    "FAK失败": "PROTECTED FAK · 失败",
                    "MAKER · 无卖一": "MAKER · 无卖一",
                    "MAKER · 簿动重试": "MAKER · 簿动重试",
                    "MAKER · 失败": "MAKER · 失败",
                    "PROTECTED FAK": "PROTECTED FAK",
                    "FAK 部分成交": "PROTECTED FAK · 部分",
                }.get(note, note)
            self._snap_assets[asset_id] = text
            if cid:
                self._snap_conditions[cid] = text
        except Exception:
            pass

    def _publish_status(self):
        """把本 tick 已累积的路由标记发布到 exit_status 模块(UI 只读)。

        不重置累积表:reconcile_pending 在 tick 开头清空,route_position 逐仓追加,
        每仓路由完即发布一次,UI 状态不滞后。"""
        active = 0
        try:
            active += self.db.count_active_merge_events(self.wallet_address)
        except Exception:
            pass
        active += len(self._managed_sells)
        for p in self._positions:
            if _num(p.get("size")) <= 0:
                continue
            aid = p.get("asset", "")
            cid = p.get("conditionId", "")
            if aid not in self._snap_assets:
                self._snap_assets[aid] = "—"
            self._snap_conditions.setdefault(cid, "—")
        exit_status.set_snapshot(
            self.wallet_address, self._snap_assets, self._snap_conditions,
            active, self._tick_ts,
        )
