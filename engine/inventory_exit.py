"""engine/inventory_exit.py — Merge-First Fast-Exit V2 (post-fill inventory engine).

Reward BUY fills are inventory-risk events, not directional investments.  This
module owns everything that happens after a fill, for one wallet:

- durable exit-cycle lifecycle (ACTIVE / CLOSING / BLOCKED / CLOSED) keyed by
  (wallet, condition_id), persisted in ``inventory_exit_cycles``;
- Rule #1: same held token Reward BUY is blocked while the cycle owns residual;
- opposite Reward BUY is capped to the unpaired residual;
- Rule #2: existing complete sets always Merge first (executed by
  ``engine/monitor.py`` check_merges; this engine never sells a pair);
- one-sided residual routes: immediate FOK+Merge when its recovery beats
  protected direct exit by ``merge_advantage_min_usd``; otherwise a bounded
  maker escape window (``maker_exit_wait_sec``); on timeout / emergency /
  low-balance / resolution, the best *protected* immediate route, never an
  uncontrolled market sell;
- fail-closed BLOCKED handling for unknown positions/orders/FOK/Merge outcomes.

The module deliberately stays narrow: pure economics live in
``engine/exit_router.py``, pairing/planning in ``engine/merge.py`` and funds
execution in ``api/ctf.py`` + ``api/polymarket_api.py``.
"""

from __future__ import annotations

import json
import logging
import time

from engine.exit_router import executable_limit_price, executable_value
from engine.fills import extract_fills
from engine.merge import ordinary_binary_plan
from engine.positions import condition_key, positions_by_condition
from engine.take_profit import (
    ceil_to_tick,
    effective_theta_stop,
    market_fill_price,
    plan_take_profit,
)
from models.database import ActiveExitCycleExists

logger = logging.getLogger(__name__)

# Cycle statuses (only four, per design; activity lives in legs).
CYCLE_ACTIVE = "ACTIVE"
CYCLE_CLOSING = "CLOSING"
CYCLE_BLOCKED = "BLOCKED"
CYCLE_CLOSED = "CLOSED"

# Exit method labels (UI + ledger).
EXIT_METHODS = frozenset({"MERGE", "FOK+MERGE", "MAKER", "MARKET"})

# Leg kinds.
LEG_REWARD_BUY = "reward_buy"
LEG_COMPLEMENT_BUY = "complement_buy"
LEG_MERGE = "merge"
LEG_MAKER_SELL = "maker_sell"
LEG_MARKET_SELL = "market_sell"

# A Merge runtime probe is read-only (expected wallet + nonce) but costs a
# Relayer round trip; cache it per wallet for a short TTL.  Never once per
# token/order.
MERGE_READINESS_TTL_SEC = 60.0

# Protected direct exit: FAK marketable limit SELL.  Its limit is the last
# consumed bid level (worst acceptable price derived from depth).
EPSILON = 1e-9
# How long a CLOSING cycle waits for Data API inventory convergence before a
# stale CLOSING (e.g. failed FOK with no barrier) is re-routed.
CLOSING_GRACE_SEC = 120.0
# After a cycle closes with unconfirmed exit legs, keep retrying authoritative
# CLOB fill reconciliation for this long; then the unfilled remainder is
# cancelled (superseded orders that never filled).
LEDGER_RECONCILE_GRACE_SEC = 300.0

# Leg statuses.  Only "confirmed" / "done" legs carry realized collateral.
LEG_RESTED = "rested"        # maker SELL intent, zero realized collateral
LEG_PENDING = "pending"      # dispatched FAK/FOK, zero realized collateral
LEG_CONFIRMED = "confirmed"  # fill reconciled from authoritative CLOB trades
LEG_DONE = "done"            # reward BUY fill / confirmed Merge
LEG_FAILED = "failed"
LEG_CANCELLED = "cancelled"
CONFIRMED_STATUSES = frozenset({LEG_CONFIRMED, LEG_DONE})
# Exit leg kinds (exclude reward_buy, which is the cost side).
EXIT_LEG_KINDS = frozenset({LEG_COMPLEMENT_BUY, LEG_MERGE, LEG_MAKER_SELL, LEG_MARKET_SELL})


# ---------------------------------------------------------------------------
# Pure economics (deterministic, fully unit-tested)
# ---------------------------------------------------------------------------


def direct_recovery(bids: list[dict], qty: float):
    """(depth-weighted proceeds, worst acceptable price) or (None, None).

    ``best_bid * qty`` is deliberately NOT used unless the best bid alone
    contains the entire qty — executable_value walks the full bid depth.
    """
    if float(qty or 0) <= 0:
        return 0.0, None
    value = executable_value(bids or [], qty)
    if value is None:
        return None, None
    return value, executable_limit_price(bids or [], qty)


def complement_merge_recovery(
    qty: float,
    complement_asks: list[dict],
    collateral_per_set: float = 1.0,
    fee_buffer: float = 0.0,
):
    """(recovery, worst_case_limit) or (None, None) for a qty complement buy + Merge.

    Recovery = qty * collateral_per_set - qty * limit - fee_buffer, where
    ``limit`` is the actual worst-case price bound of the signed FOK request
    (the last ask level consumed by the full qty) — never an optimistic best
    ask.  Insufficient complement depth -> (None, None): do not FOK.
    """
    qty = float(qty or 0)
    if qty <= 0:
        return 0.0, 0.0
    limit = executable_limit_price(complement_asks or [], qty)
    if limit is None:
        return None, None
    recovery = qty * float(collateral_per_set) - qty * limit - float(fee_buffer or 0)
    return recovery, limit


def merge_advantage(merge_value, direct_value):
    """MergeAdvantage = ComplementMergeRecovery - DirectRecovery, or None."""
    if merge_value is None or direct_value is None:
        return None
    return merge_value - direct_value


def choose_residual_route(
    *,
    qty: float,
    direct_value,
    direct_worst,
    merge_value,
    margin: float = 0.01,
    wait_sec: float = 30,
    elapsed: float = 0.0,
    urgent: bool = False,
    merge_capable: bool = True,
) -> dict:
    """Select the V2 route for one-sided residual ``qty``.

    Rules (documented in docs/FAST_EXIT_V2_DESIGN.md):
    1. FOK+Merge may execute immediately whenever
       ComplementMergeRecovery >= DirectRecovery + margin (or DirectRecovery
       is unavailable but the protected Merge route is executable).  It does
       NOT wait for the maker window.
    2. Otherwise, when not urgent and the window has not elapsed, rest in the
       maker escape window (the caller places a maker SELL / capped opposite
       BUY).
    3. Urgent (emergency / low-balance / resolution) or timeout: choose the
       protected direct exit; when it is not executable -> BLOCKED (the caller
       must not invent execution).
    """
    qty = float(qty or 0)
    if qty <= 0:
        return {"route": "NOOP", "reason": "no residual inventory", "window_remaining": None}
    merge_wins = bool(
        merge_capable
        and merge_value is not None
        and (direct_value is None or merge_value >= float(direct_value) + float(margin))
    )
    if merge_wins:
        return {
            "route": "FOK_MERGE",
            "reason": "FOK+Merge recovers at least the advantage margin more than direct exit",
            "window_remaining": None,
        }
    window_expired = float(wait_sec) <= 0 or float(elapsed) >= float(wait_sec)
    if urgent or window_expired:
        if direct_worst is not None and direct_value is not None:
            return {
                "route": "DIRECT",
                "reason": "protected direct exit selected after immediate comparison",
                "window_remaining": None,
            }
        return {
            "route": "BLOCKED",
            "reason": "neither protected route is safely executable",
            "window_remaining": None,
        }
    return {
        "route": "MAKER_WINDOW",
        "reason": "maker escape window until timeout",
        "window_remaining": max(0.0, float(wait_sec) - float(elapsed)),
    }


def maker_escape_price(best_bid, best_ask, tick):
    """Deterministic maker-only SELL price — cost is NOT a floor.

    A resting SELL must sit strictly above the best bid so it can never become
    an accidental taker order.  The natural maker price is ``best_ask``; when
    the ask side is missing or degenerate (best_ask <= best_bid), use the
    smallest tick above the best bid.  V2 fast inventory exit may rest below
    the original cost; cost stays an accounting/emergency metric only.
    """
    if best_bid is None or best_bid <= 0 or tick is None or float(tick) <= 0:
        return None
    floor = ceil_to_tick(float(best_bid) + float(tick), float(tick))
    if best_ask is not None and float(best_ask) > float(best_bid):
        return float(best_ask)
    return floor


def opposite_total_target(unpaired_residual, proposed_qty):
    """Desired TOTAL resting opposite Reward BUY quantity.

    Hard invariant: total OPEN opposite Reward BUY remaining quantity must be
    <= CURRENT unpaired residual.  This returns the total target (not an
    "additional allowed" delta); the caller reconciles the resting book to it.
    """
    return max(
        0.0,
        min(
            float(proposed_qty or 0),
            max(0.0, float(unpaired_residual or 0)),
        ),
    )


def exit_method_label(legs: list[dict]) -> str:
    """MERGE / FOK+MERGE / MAKER / MARKET / MIXED from CONFIRMED exit legs.

    Order intent (rested/pending legs) never counts as an exit method; only
    realized legs (confirmed fills / done merges) contribute.
    """
    methods = {
        str(leg.get("exit_method") or "")
        for leg in legs or []
        if leg.get("exit_method") in EXIT_METHODS
        and leg.get("kind") in EXIT_LEG_KINDS
        and leg.get("status") in CONFIRMED_STATUSES
    }
    if not methods:
        return ""
    return next(iter(methods)) if len(methods) == 1 else "MIXED"


def open_buy_qty(open_orders: list[dict], token_id: str) -> float:
    """Unfilled resting BUY quantity on one token."""
    qty = 0.0
    for order in open_orders or []:
        if str(order.get("side", "")).upper() != "BUY":
            continue
        if str(order.get("asset_id", "")) != str(token_id):
            continue
        try:
            qty += float(order.get("original_size", order.get("size", 0)) or 0) - float(
                order.get("size_matched", 0) or 0
            )
        except (TypeError, ValueError):
            continue
    return qty


# ---------------------------------------------------------------------------
# Inventory Exit Engine (one instance per wallet)
# ---------------------------------------------------------------------------


class InventoryExitEngine:
    """Owns post-fill inventory for one wallet; final placement authority."""

    def __init__(
        self,
        api,
        db,
        wallet_address: str,
        encryption_key: bytes = None,
        condition_lock=None,
        cost_provider=None,
        book_provider=None,
        trades_provider=None,
        status_add=None,
        record_action=None,
        readiness_checker=None,
    ):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        self.encryption_key = encryption_key
        self._condition_lock = condition_lock
        # (asset_id, size, condition_id) -> (cost|None, lots); injected by the
        # monitor to reuse its per-tick cached CLOB cost reconstruction.
        self._cost_provider = cost_provider
        # (asset_id) -> raw orderbook dict|None; injected by the monitor to
        # reuse the per-tick prefetch cache.
        self._book_provider = book_provider
        # (condition_id) -> raw get_trades list|None; injected by the monitor
        # to reuse the per-tick prefetch cache for fill reconciliation.
        self._trades_provider = trades_provider
        self._status_add = status_add or (lambda **fields: None)
        self._record_action = record_action or (
            lambda *a, **kw: None
        )
        self._readiness_checker = readiness_checker
        self._readiness_cache = (0.0, True, "")
        # condition_key -> until; accepted/indeterminate FOK barrier (V2-owned).
        self._pending_fok_until: dict[str, float] = {}
        # cycle_id -> {asset_id: until} — per-asset mutation pending markers so
        # a two-sided cycle never blocks one side on the other's Data API lag.
        self._exit_pending: dict[int, dict[str, float]] = {}
        self._liquidate_cooldown_until: float = 0.0
        self._token_cache: dict[str, list] = {}
        self._last_position_count: int | None = None

    # -- helpers ------------------------------------------------------------

    def _try_condition_lock(self, condition_id):
        if not self._condition_lock:
            return None
        lock = self._condition_lock(condition_id)
        return lock if lock.acquire(blocking=False) else False

    @staticmethod
    def _release_condition_lock(lock):
        if lock:
            lock.release()

    def _template(self) -> dict:
        try:
            return self.db.get_template_for(self.wallet_address)
        except Exception as exc:
            logger.warning("template unavailable for %s: %s", self.wallet_address, exc)
            return {}

    def _token_outcome(self, asset_id: str, condition_id: str) -> str:
        """YES/NO label for an asset in a condition ('' when unknown)."""
        tokens = self._market_tokens(condition_id)
        for token_id, outcome in tokens:
            if str(token_id) == str(asset_id):
                return outcome
        return ""

    def _opposite_token(self, condition_id: str, asset_id: str):
        """Complement YES/NO token, or None for non-ordinary/ambiguous markets."""
        ours = self._token_outcome(asset_id, condition_id)
        if ours not in ("YES", "NO"):
            return None
        for token_id, outcome in self._market_tokens(condition_id):
            if outcome in ("YES", "NO") and outcome != ours:
                return token_id
        return None

    def _market_tokens(self, condition_id: str) -> list:
        if condition_id in self._token_cache:
            return self._token_cache[condition_id]
        parsed: list = []
        try:
            market = self.api.get_market(condition_id)
            for token in market.get("tokens") or market.get("outcomes") or []:
                if not isinstance(token, dict):
                    continue
                token_id = token.get("token_id") or token.get("tokenId") or token.get("id")
                outcome = str(token.get("outcome") or token.get("name") or "").upper()
                if token_id:
                    parsed.append((str(token_id), outcome))
        except Exception as exc:
            logger.warning("get_market(%s) failed for exit engine: %s", condition_id, exc)
        self._token_cache[condition_id] = parsed
        return parsed

    def _book(self, asset_id: str):
        if self._book_provider is not None:
            try:
                return self._book_provider(asset_id)
            except Exception:
                return None
        try:
            return self.api.get_orderbook(asset_id)
        except Exception as exc:
            logger.warning("orderbook %s failed (exit engine): %s", asset_id, exc)
            return None

    def _fresh_book(self, asset_id: str):
        """Orderbook bypassing the per-tick prefetch cache.

        Used after cancellations / position refetches, where the audit requires
        a genuinely fresh depth snapshot (never a stale prefetch).
        """
        try:
            return self.api.get_orderbook(asset_id)
        except Exception as exc:
            logger.warning("orderbook %s fresh refetch failed: %s", asset_id, exc)
            return None

    def _trades(self, condition_id: str):
        """Raw get_trades for a condition (prefetch cache first), or None."""
        if self._trades_provider is not None:
            try:
                return self._trades_provider(condition_id)
            except Exception:
                return None
        try:
            from py_clob_client_v2.clob_types import TradeParams

            return self.api.get_trades(TradeParams(market=condition_id))
        except Exception as exc:
            logger.warning("get_trades(%s) failed (exit engine): %s", condition_id, exc)
            return None

    def _cost(self, asset_id: str, size: float, condition_id: str):
        if self._cost_provider is not None:
            try:
                return self._cost_provider(asset_id, size, condition_id)
            except Exception as exc:
                logger.warning("cost provider failed %s: %s", asset_id, exc)
                return None, []
        return None, []

    def _log_route(self, cid: str, side: str, qty: float, age: float, direct, merge,
                   advantage, remaining, route: str, reason: str) -> None:
        logger.info(
            "[exit-v2] wallet=%s cond=%s side=%s qty=%g age=%.1fs direct=%s merge=%s "
            "advantage=%s maker_left=%s route=%s reason=%s",
            str(self.wallet_address)[:8],
            str(cid)[:10],
            side,
            qty,
            age,
            "n/a" if direct is None else f"{direct:.4f}",
            "n/a" if merge is None else f"{merge:.4f}",
            "n/a" if advantage is None else f"{advantage:.4f}",
            "n/a" if remaining is None else f"{remaining:.0f}s",
            route,
            reason,
        )

    # -- cycle lifecycle ------------------------------------------------------

    def on_reward_fill(self, ev: dict) -> None:
        """Create or join the (wallet, condition) exit cycle for a Reward BUY fill."""
        cid = ev.get("market", "")
        asset_id = ev.get("asset_id", "")
        size = float(ev.get("size", 0) or 0)
        price = float(ev.get("price", 0) or 0)
        if not cid or size <= 0:
            return
        try:
            outcome = self._token_outcome(asset_id, cid)
            existing = self.db.get_active_exit_cycle(self.wallet_address, cid)
            if existing:
                cycle_id = existing["id"]
                held_assets = set(
                    json.loads(existing.get("held_assets_json") or "[]")
                )
                held_assets.add(asset_id)
                self.db.update_exit_cycle(
                    cycle_id,
                    status=CYCLE_ACTIVE,
                    managed_qty=float(existing.get("managed_qty", 0) or 0) + size,
                    initial_qty=float(existing.get("initial_qty", 0) or 0) + size,
                    held_side=outcome or existing.get("held_side", ""),
                    held_asset_id=asset_id or existing.get("held_asset_id", ""),
                    held_assets_json=json.dumps(sorted(held_assets)),
                    error="",
                )
            else:
                cycle_id = self.db.create_exit_cycle(
                    self.wallet_address,
                    cid,
                    trigger="reward_fill",
                    held_side=outcome,
                    held_asset_id=asset_id,
                    held_assets_json=json.dumps([asset_id]),
                    qty=size,
                )
            self.db.add_exit_leg(
                cycle_id,
                self.wallet_address,
                cid,
                LEG_REWARD_BUY,
                asset_id=asset_id,
                side=outcome,
                qty=size,
                price=price,
                collateral=-(size * price),
                order_id=str(ev.get("order_id") or ""),
                note=f"reward fill trade={ev.get('trade_id', '')}",
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{price:.4f}",
                size=str(size),
                matched=str(size),
                stage="库存退出",
                action="周期 ACTIVE",
                detail=f"Reward BUY 成交 {size:g} 份 -> 库存退出周期（join={bool(existing)}）",
            )
        except ActiveExitCycleExists:
            logger.info("exit cycle already active for %s/%s", self.wallet_address[:8], cid)
        except Exception as exc:
            logger.error("on_reward_fill cycle ledger failed %s: %s", cid, exc)

    def _reconcile_cycle_inventory(self, cycle: dict, group: list[dict]) -> dict:
        """Update cycle managed inventory from confirmed positions.

        Returns a per-side summary: {yes_qty, no_qty, paired, residual_side,
        residual_asset, residual_qty, total, yes_asset, no_asset}.
        """
        yes_qty = no_qty = 0.0
        yes_asset = no_asset = ""
        for pos in group or []:
            try:
                size = float(pos.get("size", 0) or 0)
            except (TypeError, ValueError):
                continue
            if size <= 0:
                continue
            outcome = str(pos.get("outcome", "")).strip().upper()
            if outcome == "YES":
                yes_qty += size
                yes_asset = str(pos.get("asset", "") or "")
            elif outcome == "NO":
                no_qty += size
                no_asset = str(pos.get("asset", "") or "")
        minimum = float(self._template().get("merge_min_shares", 1) or 0)
        plan = ordinary_binary_plan(cycle["condition_id"], group or [], minimum)
        paired = float(plan.qty) if plan else 0.0
        residual_side, residual_asset, residual_qty = "", "", 0.0
        if yes_qty > no_qty + EPSILON:
            residual_side = "YES"
            residual_qty = yes_qty - paired
            residual_asset = yes_asset
        elif no_qty > yes_qty + EPSILON:
            residual_side = "NO"
            residual_qty = no_qty - paired
            residual_asset = no_asset
        return {
            "yes_qty": yes_qty,
            "no_qty": no_qty,
            "paired": paired,
            "residual_side": residual_side,
            "residual_asset": residual_asset,
            "residual_qty": residual_qty,
            "yes_asset": yes_asset,
            "no_asset": no_asset,
            "total": yes_qty + no_qty,
        }

    def run_tick(
        self,
        open_orders=None,
        low_balance: bool = False,
        positions: list | None = None,
    ) -> None:
        """One monitor-tick pass for all managed cycles of this wallet."""
        tmpl = self._template()
        if not tmpl.get("fast_exit_enabled", True):
            return  # legacy mode owns post-fill inventory
        self._token_cache = {}
        if positions is None:
            try:
                positions = self.api.get_user_positions(self.api.get_funder())
            except Exception as exc:
                logger.warning("[exit-v2] positions unavailable, cycles BLOCKED: %s", exc)
                for cycle in self.db.get_non_closed_exit_cycles(self.wallet_address):
                    if cycle["status"] != CYCLE_CLOSING:
                        self.db.update_exit_cycle(
                            cycle["id"], status=CYCLE_BLOCKED,
                            error="positions unavailable",
                        )
                return
        if open_orders is None:
            try:
                open_orders = self.api.get_open_orders()
            except Exception as exc:
                logger.warning("[exit-v2] open orders unavailable: %s", exc)
                for cycle in self.db.get_non_closed_exit_cycles(self.wallet_address):
                    if cycle["status"] != CYCLE_CLOSING:
                        self.db.update_exit_cycle(
                            cycle["id"], status=CYCLE_BLOCKED,
                            error="open orders unavailable; no funds mutation",
                        )
                return
        by_condition = positions_by_condition(positions)
        if not low_balance:
            low_balance = self._low_balance_triggered(tmpl)
        if low_balance:
            # Mirror V1: after a low-balance recovery, cool down so on-chain
            # collateral settlement can catch up before re-scanning.
            self._liquidate_cooldown_until = time.time() + 60
        resolving = self._resolving_conditions(by_condition)
        self._adopt_legacy_inventory(by_condition, open_orders)
        self._retry_close_ledger()
        cycles = self.db.get_non_closed_exit_cycles(self.wallet_address)
        self._last_position_count = sum(
            len(group) for group in by_condition.values()
        )
        for cycle in cycles:
            try:
                self._route_cycle(
                    cycle,
                    by_condition.get(cycle["condition_id"], []),
                    open_orders or [],
                    resolving,
                    low_balance,
                    tmpl,
                )
            except Exception as exc:
                logger.exception("[exit-v2] cycle %s failed: %s", cycle["id"], exc)

    def _resolving_conditions(self, by_condition) -> set:
        cids = [cid for cid in by_condition if cid]
        if not cids:
            return set()
        try:
            status_map = self.api.gamma_resolution_status(cids)
        except Exception as exc:
            logger.warning("[exit-v2] gamma status unavailable (fail-open): %s", exc)
            return set()
        from engine.resolution import in_resolution

        return {c for c in cids if in_resolution(status_map.get(c))}

    def _low_balance_triggered(self, tmpl: dict) -> bool:
        threshold = float(tmpl.get("low_balance_threshold_usd", 0) or 0)
        if threshold <= 0:
            return False
        if time.time() < self._liquidate_cooldown_until:
            return False
        try:
            balance = float(self.api.get_balance() or 0)
        except Exception as exc:
            logger.warning("[exit-v2] get_balance failed: %s", exc)
            return False
        return balance < threshold

    def _adopt_legacy_inventory(self, by_condition, open_orders) -> None:
        """Recover bot-owned inventory only, from authoritative CLOB evidence.

        Ownership rule (audit fix E/F): a V2 cycle may auto-own only inventory
        whose bot ownership is reconstructible from CLOB get_trades (our maker
        Reward BUY fills, bot SELLs, confirmed Merge consumption).  Manual /
        external positions without evidence are displayed UNMANAGED and never
        mutated.  A proven offline Reward fill after a previous CLOSED cycle
        opens a brand-new cycle exactly once (fill evidence is newer than the
        previous cycle's close); stale Data API rows after a close never reopen.
        """
        if not by_condition:
            return
        try:
            active = {
                condition_key(c["condition_id"])
                for c in self.db.get_non_closed_exit_cycles(self.wallet_address)
            }
            closed_by_condition: dict[str, list[dict]] = {}
            for row in self.db.get_exit_cycles(
                wallet=self.wallet_address, statuses=[CYCLE_CLOSED]
            ):
                closed_by_condition.setdefault(
                    condition_key(row["condition_id"]), []
                ).append(row)
        except Exception:
            return
        for cid, group in by_condition.items():
            key = condition_key(cid)
            if key in active:
                continue
            summary = self._reconcile_cycle_inventory(
                {"condition_id": cid}, group
            )
            if summary["total"] <= 0:
                continue
            # Evidence side: residual side when one-sided, else YES.
            if summary["residual_side"]:
                side, asset, side_qty = (
                    summary["residual_side"],
                    summary["residual_asset"],
                    summary["yes_qty"]
                    if summary["residual_side"] == "YES"
                    else summary["no_qty"],
                )
            else:
                side, asset, side_qty = "YES", summary["yes_asset"], summary["yes_qty"]
            if not asset or side_qty <= 0:
                continue
            cost, lots = self._cost(asset, side_qty, cid)
            if cost is None or cost <= 0:
                # No bot ownership evidence: never auto-trade this inventory.
                self._status_add(
                    market=cid, side=side, price="-", size=str(side_qty),
                    matched="-", stage="库存退出",
                    action="⚠️UNMANAGED·无机器人成交证据",
                    detail="CLOB 成交无法重建机器人所有权；不自动卖出/补边/Merge，等待人工处理",
                )
                self._record_action(
                    cid, "exit_unmanaged_inventory", "-", -1, side_qty,
                    "未托管库存：无机器人 Reward BUY 成交证据，V2 不自动交易",
                    "证据来源：CLOB get_trades + FIFO 重建（禁用 Data API avgPrice）",
                )
                continue
            prior = closed_by_condition.get(key, [])
            max_fill_ts = max(
                (float(l.get("ts", 0) or 0) for l in lots),
                default=0.0,
            )
            latest_close = max(
                (float(c.get("closed_at", 0) or 0) for c in prior),
                default=0.0,
            )
            if prior and max_fill_ts <= latest_close:
                # Inventory predates the previous cycle's close: stale Data API.
                self._status_add(
                    market=cid, side=side, price="-", size=str(side_qty),
                    matched="-", stage="库存退出",
                    action="陈旧库存（上次周期已关闭）",
                    detail="持仓证据早于上次周期关闭时间，等待 Data API 收敛；不重开周期",
                )
                continue
            try:
                trigger = "recovered_offline_fill" if prior else "adopt_legacy"
                self.db.create_exit_cycle(
                    self.wallet_address,
                    cid,
                    trigger=trigger,
                    held_side=summary["residual_side"] or side,
                    held_asset_id=summary["residual_asset"] or asset,
                    held_assets_json=json.dumps(
                        sorted(
                            a for a in (summary["yes_asset"], summary["no_asset"]) if a
                        )
                    ),
                    qty=summary["residual_qty"] or summary["total"],
                    paired_qty=summary["paired"],
                )
                self._record_action(
                    cid, "exit_cycle_opened", "-", -1, summary["total"],
                    f"V2 {trigger}：机器人成交证据确认所有权，开启库存退出周期",
                    f"evidence_side={side} residual={summary['residual_qty']:g} "
                    f"paired={summary['paired']:g}",
                )
            except ActiveExitCycleExists:
                continue

    def _route_cycle(
        self, cycle, group, open_orders, resolving: set, low_balance: bool, tmpl: dict
    ) -> None:
        cid = cycle["condition_id"]
        now = time.time()
        key = condition_key(cid)
        self._record_confirmed_merge_legs(cycle)
        self._confirm_exit_legs(cycle)
        # Funds-moving Merge machinery owns this condition: never mutate
        # residual while a planned/submitted Merge, an accepted/indeterminate
        # FOK, or a confirmed-but-unreconciled Merge is in flight.
        try:
            unresolved = {
                condition_key(op["condition_id"])
                for op in self.db.get_unresolved_merges(self.wallet_address)
                if op.get("condition_id")
            }
            inventory_barriers = {
                condition_key(op["condition_id"])
                for op in self.db.get_merge_inventory_barriers(self.wallet_address)
                if op.get("condition_id")
            }
        except Exception as exc:
            # Audit fix A: unknown barrier state MUST NOT mean "no barrier".
            # Fail closed: no Maker SELL, no FAK, no complement FOK.
            logger.error(
                "[exit-v2] merge barrier query failed; cycle BLOCKED: %s", exc
            )
            self.db.update_exit_cycle(
                cycle["id"], status=CYCLE_BLOCKED,
                error=f"merge barrier state unavailable: {exc}",
            )
            self._status_add(
                market=cid, side="-", price="-", size="-", matched="-",
                stage="库存退出", action="⚠️BLOCKED·合并屏障未知",
                detail="无法确认 Merge/FOK 屏障状态，禁止一切资金变更（fail-closed）",
            )
            return
        pending_fok = {
            k for k, until in self._pending_fok_until.items() if until > now
        }
        if key in unresolved or key in inventory_barriers or key in pending_fok:
            self.db.update_exit_cycle(cycle["id"], status=CYCLE_CLOSING, error="")
            self._status_add(
                market=cid, side="卖出", price="-", size="-", matched="-",
                stage="库存退出", action="CLOSING·等待合并确认",
                detail="未决 Merge/FOK 阻止并行库存变更",
            )
            return

        summary = self._reconcile_cycle_inventory(cycle, group)
        self.db.update_exit_cycle(
            cycle["id"],
            managed_qty=summary["total"],
            paired_qty=summary["paired"],
            held_side=summary["residual_side"] or cycle.get("held_side", ""),
            held_asset_id=summary["residual_asset"] or cycle.get("held_asset_id", ""),
        )

        if summary["total"] <= EPSILON:
            self._close_cycle(cycle, reason="inventory returned to zero")
            return

        merge_eligible = self._merge_capable(tmpl)
        merge_ready = False
        if merge_eligible:
            merge_ready, _ = self.merge_runtime_ready()
        merge_capable = merge_eligible and merge_ready

        if summary["paired"] > 0 and merge_capable:
            # Rule #2: existing complete sets always Merge first.  check_merges
            # runs earlier in the tick; never sell either leg of a pair while
            # automatic Merge is actually safely executable.
            self.db.update_exit_cycle(
                cycle["id"], status=CYCLE_ACTIVE, selected_route="MERGE_FIRST", error=""
            )
            self._status_add(
                market=cid, side="-", price="-", size=str(summary["paired"]),
                matched="-", stage="库存退出", action="等待合并(Merge-first)",
                detail=f"完整集合 {summary['paired']:g} 份先 Merge，残差 {summary['residual_qty']:g}",
            )
            return

        # Build the sides that must be routed.  With automatic Merge actually
        # executable the pair is reserved above; otherwise (audit fix G) BOTH
        # owned sides exit through non-Merge handling instead of freezing.
        sides = []
        if summary["yes_qty"] > EPSILON and summary["yes_asset"]:
            sides.append(
                {"side": "YES", "asset": summary["yes_asset"], "qty": summary["yes_qty"]}
            )
        if summary["no_qty"] > EPSILON and summary["no_asset"]:
            sides.append(
                {"side": "NO", "asset": summary["no_asset"], "qty": summary["no_qty"]}
            )
        if not sides:
            self.db.update_exit_cycle(cycle["id"], status=CYCLE_ACTIVE, selected_route="NOOP")
            return
        held_assets = sorted(s["asset"] for s in sides)
        # Per-asset CLOSING guard: a pending mutation on one asset must not
        # freeze the other side of the same condition.
        blocked = [s for s in sides if self._closing_blocks_side(cycle, s["asset"], now)]
        if blocked:
            self._status_add(
                market=cid, side=blocked[0]["side"], price="-",
                size=str(blocked[0]["qty"]), matched="-",
                stage="库存退出", action="CLOSING·等待库存收敛",
                detail="上一笔退出变更待 Data API 确认，禁止重复退出",
            )
            return
        if cycle.get("status") == CYCLE_CLOSING:
            # stale CLOSING (no fresh pending mutation, no barrier): re-route.
            self.db.update_exit_cycle(cycle["id"], status=CYCLE_ACTIVE, error="")

        routes = []
        for s in sides:
            route = self._route_one_side(
                cycle, cid, s["side"], s["asset"], s["qty"], tmpl, open_orders,
                resolving, low_balance, now, merge_capable,
            )
            routes.append(route)
        if any(r in ("FOK_MERGE", "MARKET") for r in routes):
            aggregate_status, aggregate_route = CYCLE_CLOSING, next(
                (r for r in routes if r in ("FOK_MERGE", "MARKET")), ""
            )
        elif any(r == "MAKER_WINDOW" for r in routes):
            aggregate_status, aggregate_route = CYCLE_ACTIVE, "MAKER_WINDOW"
        elif any(r == "BLOCKED" for r in routes):
            aggregate_status, aggregate_route = CYCLE_BLOCKED, "BLOCKED"
        elif all(r in ("WAIT", "NOOP") for r in routes):
            # Nothing executed and nothing was blocked: keep the cycle's own
            # status (a concurrent mutation may own it right now).
            aggregate_status, aggregate_route = (
                cycle.get("status", CYCLE_ACTIVE),
                cycle.get("selected_route", ""),
            )
        else:
            aggregate_status, aggregate_route = CYCLE_ACTIVE, "NOOP"
        current = self.db.get_exit_cycle(cycle["id"]) or cycle
        current_error = str(current.get("error", "") or "")
        if aggregate_status == CYCLE_BLOCKED and not current_error:
            current_error = "no protected route safely executable"
        self.db.update_exit_cycle(
            cycle["id"],
            status=aggregate_status,
            selected_route=aggregate_route,
            held_side=sides[0]["side"],
            held_asset_id=sides[0]["asset"],
            held_assets_json=json.dumps(held_assets),
            error=current_error,
        )

    def _closing_blocks_side(self, cycle: dict, asset: str, now: float) -> bool:
        """True when a fresh pending mutation on this asset forbids re-routing."""
        if cycle.get("status") != CYCLE_CLOSING:
            return False
        pending_map = self._exit_pending.get(cycle["id"], {})
        if pending_map.get(asset, 0.0) > now:
            return True
        if not pending_map:
            # Restart case: a recent CLOSING with no in-memory markers waits
            # out the grace window before being treated as stale.
            return now - float(cycle.get("updated_at", now) or now) < CLOSING_GRACE_SEC
        return False

    def _route_one_side(
        self,
        cycle,
        cid,
        side,
        asset,
        qty,
        tmpl,
        open_orders,
        resolving,
        low_balance,
        now,
        merge_capable,
    ):
        """Route one owned side (one-sided residual or half of an unmergeable pair)."""
        key = condition_key(cid)
        cost, lots = self._cost(asset, qty, cid)
        if cost is None or cost <= 0:
            self._status_add(
                market=cid, side=side, price="-", size=str(qty), matched="-",
                stage="库存退出", action="⚠️BLOCKED·成本未知",
                detail="get_trades 无法重建成本，不做库存变更，下 tick 重试",
            )
            return "BLOCKED"

        book = self._book(asset) or {}
        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        tick = float(book.get("tick_size", "0.01") or 0.01)
        tick_str = str(book.get("tick_size", "0.01") or "0.01")

        direct, direct_worst = direct_recovery(bids, qty)
        complement = self._opposite_token(cid, asset)
        merge_value = merge_limit = None
        if merge_capable and complement:
            comp_book = self._book(complement)
            if comp_book:
                comp_asks = sorted(
                    comp_book.get("asks", []), key=lambda x: float(x["price"])
                )
                merge_value, merge_limit = complement_merge_recovery(qty, comp_asks)
        advantage = merge_advantage(merge_value, direct)

        stop_mode = tmpl.get("stop_loss_mode", "percent")
        stop_percent = tmpl.get("stop_loss_percent", 20)
        stop_cents = tmpl.get("theta_stop_cents", 5)
        theta_stop = effective_theta_stop(cost, stop_mode, stop_percent, stop_cents)
        emergency = (
            theta_stop is not None
            and best_bid is not None
            and (cost - best_bid) >= theta_stop
        )
        urgent = bool(low_balance or key in resolving or emergency)
        if (
            tmpl.get("take_profit_mode", "maker") == "market"
            and cost is not None
            and best_bid is not None
            and cost < best_bid
        ):
            urgent = True
        wait_sec = float(tmpl.get("maker_exit_wait_sec", 30) or 0)
        opened_at = float(cycle.get("opened_at", now) or now)
        elapsed = now - opened_at
        decision = choose_residual_route(
            qty=qty,
            direct_value=direct,
            direct_worst=direct_worst,
            merge_value=merge_value,
            margin=float(tmpl.get("merge_advantage_min_usd", 0.01) or 0),
            wait_sec=wait_sec,
            elapsed=elapsed,
            urgent=urgent,
            merge_capable=merge_capable,
        )
        self._log_route(
            cid, side, qty, now - opened_at, direct, merge_value,
            advantage, decision.get("window_remaining"), decision["route"],
            decision["reason"],
        )
        self.db.update_exit_cycle(
            cycle["id"],
            direct_recovery=direct,
            merge_recovery=merge_value,
            advantage=advantage,
            cost_basis=cost,
        )

        if decision["route"] == "FOK_MERGE":
            return self._execute_fok_merge(
                cid, asset, complement, qty, merge_limit, tick_str,
                condition_locked=False,
            )
        if decision["route"] == "DIRECT":
            return self._execute_protected_direct(
                cid, asset, qty, direct_worst, tick, tick_str,
                best_bid, cost, lots, cycle,
            )
        if decision["route"] == "BLOCKED":
            self._status_add(
                market=cid, side=side, price="-", size=str(qty), matched="-",
                stage="库存退出", action="⚠️BLOCKED·无可执行保护路线",
                detail=(
                    f"直接回收={'不可用' if direct is None else '%.4f' % direct} "
                    f"Merge回收={'不可用' if merge_value is None else '%.4f' % merge_value}"
                ),
            )
            return "BLOCKED"

        # MAKER_WINDOW: bounded maker escape.  Audit fix J: the countdown is
        # anchored to opened_at + wait_sec and never reset each tick.
        window_until = float(cycle.get("maker_window_until") or 0) or (
            opened_at + wait_sec
        )
        self.db.update_exit_cycle(
            cycle["id"],
            selected_route="MAKER_WINDOW",
            maker_window_until=window_until,
        )
        self._rest_maker_sell(
            cid, asset, qty, best_bid, best_ask, tick, tick_str, cost,
            open_orders, window_until, side, cycle,
        )
        return "MAKER_WINDOW"

    def _record_confirmed_merge_legs(self, cycle: dict) -> None:
        """Record durable MERGE legs from confirmed merge operations.

        A confirmed Merge for the cycle's condition is inventory consumption:
        it returns ``qty`` collateral per set.  Attributed as FOK+MERGE when
        this cycle previously bought a complement leg, else MERGE.
        """
        try:
            ops = self.db.get_confirmed_merges(self.wallet_address)
            legs = self.db.get_exit_legs(cycle["id"])
        except Exception as exc:
            logger.warning("[exit-v2] confirmed merge legs unavailable: %s", exc)
            return
        recorded = {
            str(leg.get("order_id"))
            for leg in legs
            if leg.get("kind") == LEG_MERGE
        }
        has_complement = any(
            leg.get("kind") == LEG_COMPLEMENT_BUY for leg in legs
        )
        opened = float(cycle.get("opened_at", 0) or 0)
        for op in ops:
            cid = op.get("condition_id", "")
            if condition_key(cid) != condition_key(cycle["condition_id"]):
                continue
            if float(op.get("confirmed_at", 0) or 0) < opened:
                continue  # merged before this cycle existed
            op_id = str(op.get("id"))
            if op_id in recorded:
                continue
            qty = float(op.get("requested_qty", 0) or 0)
            if qty <= 0:
                continue
            method = "FOK+MERGE" if has_complement else "MERGE"
            try:
                self.db.add_exit_leg(
                    cycle["id"], self.wallet_address, cid, LEG_MERGE,
                    asset_id="", side="", qty=qty, price=1.0,
                    collateral=qty, exit_method=method, order_id=op_id,
                    relayer_id=str(op.get("relayer_id", "") or ""),
                    status="done",
                    note=f"confirmed merge op {op_id} pnl={op.get('realized_pnl')}",
                    created_at=float(op.get("confirmed_at", 0) or 0) or None,
                )
                if method == "FOK+MERGE":
                    # Audit fix I: the confirmed Merge proves the complement was
                    # bought.  Realize the pending complement leg (its signed
                    # worst limit is the conservative cost bound; the CLOB fill
                    # reconciliation refines the actual price when visible).
                    for leg in legs:
                        if (
                            leg.get("kind") == LEG_COMPLEMENT_BUY
                            and leg.get("status") == LEG_PENDING
                            and condition_key(leg.get("condition_id", "")) == condition_key(cid)
                        ):
                            leg_qty = float(leg.get("qty", 0) or 0)
                            leg_price = float(leg.get("price", 0) or 0)
                            self.db.update_exit_leg(
                                leg["id"],
                                status=LEG_CONFIRMED,
                                collateral=-(leg_qty * leg_price),
                                note=f"{leg.get('note', '')} | confirmed by merge op {op_id}",
                            )
            except Exception as exc:
                logger.warning("[exit-v2] merge leg record failed %s: %s", op_id, exc)

    def _confirm_exit_legs(self, cycle: dict) -> tuple[bool, bool]:
        """Reconcile pending/rested exit legs against authoritative CLOB fills.

        Audit fix I: only confirmed fills become realized collateral.  Order
        intent (rested/pending) carries zero collateral and never counts toward
        PnL or the exit method.  Returns (filled_any, still_pending).
        """
        try:
            legs = self.db.get_exit_legs(cycle["id"])
        except Exception as exc:
            logger.warning("[exit-v2] leg read failed %s: %s", cycle["id"], exc)
            return False, True
        all_by_asset: dict[str, list[dict]] = {}
        for leg in legs:
            kind = leg.get("kind")
            if kind not in EXIT_LEG_KINDS:
                continue
            all_by_asset.setdefault(str(leg.get("asset_id", "")), []).append(leg)
        pending_by_asset = {
            asset: [l for l in leg_list if l.get("status") in (LEG_RESTED, LEG_PENDING)]
            for asset, leg_list in all_by_asset.items()
        }
        if not any(pending_by_asset.values()):
            return False, False
        trades = self._trades(cycle["condition_id"])
        if trades is None:
            return False, True
        try:
            funder = self.api.get_funder()
        except Exception:
            return False, True
        opened = float(cycle.get("opened_at", 0) or 0)
        filled_any, still_pending = False, False
        for asset, legs_ in pending_by_asset.items():
            if not asset:
                still_pending = True
                continue
            fills = sorted(
                (
                    f
                    for f in extract_fills(trades, funder, asset)
                    if float(f.get("ts", 0) or 0) >= opened - 2.0
                ),
                key=lambda f: float(f.get("ts", 0) or 0),
            )
            budget = [
                {
                    "side": str(f.get("side", "")).upper(),
                    "price": float(f.get("price", 0) or 0),
                    "size": float(f.get("size", 0) or 0),
                    "ts": float(f.get("ts", 0) or 0),
                }
                for f in fills
                if float(f.get("size", 0) or 0) > 0
            ]
            # Time-window attribution: a fill belongs to the leg that was the
            # active intent at that moment (leg created_at .. next same-direction
            # leg created_at).  A superseded maker rest never captures the later
            # market FAK fill.  Windows are built from ALL legs of the asset
            # (confirmed ones included), so a pending leg can never extend its
            # window past a later leg merely because that leg already confirmed.
            ordered = sorted(
                all_by_asset.get(asset, []),
                key=lambda leg: float(leg.get("created_at", 0) or 0),
            )
            for idx, leg in enumerate(ordered):
                wanted = float(leg.get("qty", 0) or 0)
                if wanted <= 0:
                    continue
                start = float(leg.get("created_at", 0) or 0) - 2.0
                end = (
                    float(ordered[idx + 1].get("created_at", 0) or 0)
                    if idx + 1 < len(ordered)
                    else float("inf")
                )
                want_side = (
                    "BUY"
                    if leg.get("kind") == LEG_COMPLEMENT_BUY
                    else "SELL"
                )
                taken, total_price = 0.0, 0.0
                for f in budget:
                    if f["side"] != want_side or f["size"] <= 0:
                        continue
                    if not (start <= f["ts"] < end):
                        continue
                    use = min(wanted - taken, f["size"])
                    if use <= 0:
                        continue
                    taken += use
                    total_price += f["price"] * use
                    f["size"] -= use
                    if taken >= wanted - 1e-6:
                        break
                if taken <= 0:
                    still_pending = True
                    continue
                price = total_price / taken
                collateral = taken * price
                if leg.get("kind") == LEG_COMPLEMENT_BUY:
                    collateral = -collateral
                partial = " (partial fill)" if taken < wanted - 1e-6 else ""
                try:
                    self.db.update_exit_leg(
                        leg["id"],
                        status=LEG_CONFIRMED,
                        qty=taken,
                        price=price,
                        collateral=collateral,
                        note=f"{leg.get('note', '')} | confirmed fill {taken:g}@{price:.4f}{partial}",
                    )
                except Exception as exc:
                    logger.warning("[exit-v2] leg confirm failed %s: %s", leg["id"], exc)
                    still_pending = True
                    continue
                filled_any = True
                if taken < wanted - 1e-6:
                    still_pending = True
        return filled_any, still_pending

    def _finalize_cycle(self, cycle: dict, reason: str = None) -> None:
        """Compute and persist the final economic summary from CONFIRMED legs."""
        legs = self.db.get_exit_legs(cycle["id"])
        collateral = sum(
            float(l.get("collateral", 0) or 0)
            for l in legs
            if l.get("status") in CONFIRMED_STATUSES
        )
        buy_collateral = sum(
            float(l.get("collateral", 0) or 0)
            for l in legs
            if l.get("kind") == LEG_REWARD_BUY
        )
        recovered = collateral - buy_collateral
        duration = time.time() - float(cycle.get("opened_at", time.time()) or time.time())
        method = exit_method_label(legs)
        self.db.close_exit_cycle(
            cycle["id"],
            closed_reason=reason or cycle.get("closed_reason", "") or "inventory returned to zero",
            realized_recovered_collateral=recovered,
            inventory_pnl=collateral,
            holding_duration_sec=max(0.0, duration),
        )
        self._exit_pending.pop(cycle["id"], None)
        self._record_action(
            cycle["condition_id"], "exit_cycle_closed", "-", -1,
            float(cycle.get("initial_qty", 0) or 0),
            f"库存退出周期关闭（{method or '—'}）：回收 ${recovered:.4f} 库存损益 ${collateral:.4f}",
            f"reason={reason or cycle.get('closed_reason', '')} duration={duration:.0f}s legs={len(legs)}",
        )
        logger.info(
            "[exit-v2] cycle %s finalized wallet=%s cond=%s method=%s recovery=%.4f pnl=%.4f",
            cycle["id"], str(self.wallet_address)[:8], str(cycle["condition_id"])[:10],
            method or "-", recovered, collateral,
        )

    def _retry_close_ledger(self) -> None:
        """Retry fill reconciliation for CLOSED cycles awaiting realized legs."""
        try:
            cycles = self.db.get_ledger_pending_exit_cycles(self.wallet_address)
        except Exception as exc:
            logger.warning("[exit-v2] ledger-pending cycle query failed: %s", exc)
            return
        for cycle in cycles:
            try:
                self._confirm_exit_legs(cycle)
                legs = self.db.get_exit_legs(cycle["id"])
                still = [
                    l for l in legs
                    if l.get("kind") in EXIT_LEG_KINDS
                    and l.get("status") in (LEG_RESTED, LEG_PENDING)
                ]
                if still:
                    age = time.time() - float(cycle.get("closed_at", time.time()) or time.time())
                    if age >= LEDGER_RECONCILE_GRACE_SEC:
                        # Unfilled remainder of superseded orders never filled:
                        # cancel it so the cycle can be finalized.
                        for l in still:
                            self.db.update_exit_leg(
                                l["id"], status=LEG_CANCELLED,
                                note="no confirmed fill within ledger grace; unfilled remainder",
                            )
                        self._finalize_cycle(cycle)
                    continue
                self._finalize_cycle(cycle)
            except Exception as exc:
                logger.warning("[exit-v2] ledger retry failed cycle %s: %s", cycle["id"], exc)

    # -- route execution -------------------------------------------------------

    def _merge_capable(self, tmpl: dict) -> bool:
        """True when the automatic-Merge funds path is eligible for this wallet."""
        if not tmpl.get("merge_enabled", True):
            return False
        if int(getattr(self.api, "signature_type", -1)) != 3:
            return False
        if not getattr(self.api, "trading_enabled", True):
            return False
        try:
            from api.relayer_config import load_relayer_runtime_config

            config = load_relayer_runtime_config(self.db, self.encryption_key)
            return config is not None and config.configured
        except Exception:
            return False

    def _execute_fok_merge(
        self, cid, held_asset, complement, qty, limit_price, tick_str,
        *, condition_locked=False,
    ):
        """Protected complement FOK + Merge with the full race-protection sequence.

        Order (spec §18): lock -> CLOSING -> cancel opposite Reward BUY ->
        cancel SELL reservations -> confirm cancellations by refetch -> refetch
        positions -> exact missing qty -> refetch complement book -> recompute
        economics -> persist plan -> submit exact protected FOK.  A stale
        quantity is never used; without confirmed cancellations no FOK happens.
        """
        mutation_lock = None if condition_locked else self._try_condition_lock(cid)
        if mutation_lock is False:
            return "WAIT"
        try:
            # Audit fix C: never buy the complement unless the Merge runtime is
            # actually READY right now.  The route preview may use the short
            # TTL cache; immediately before the funds-moving FOK we force a
            # fresh read-only validation (Type3 + trading + credentials +
            # expected Deposit Wallet == funder + authenticated nonce).
            ready, reason = self.merge_runtime_ready(force=True)
            if not ready:
                cycle_row = self.db.get_active_exit_cycle(self.wallet_address, cid)
                if cycle_row:
                    self.db.update_exit_cycle(
                        cycle_row["id"], status=CYCLE_ACTIVE, selected_route="",
                        error="",
                    )
                self._status_add(
                    market=cid, side="-", price="-", size=str(qty), matched="-",
                    stage="库存退出", action="FOK 取消（Merge 运行时未就绪）",
                    detail=f"readiness={reason}；回退 Maker/Market 保护路线",
                )
                return "NOOP"
            try:
                open_orders = self.api.get_open_orders()
            except Exception as exc:
                logger.warning("[exit-v2] FOK pre-check open orders failed: %s", exc)
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_BLOCKED, error="open orders unavailable before FOK",
                )
                return "BLOCKED"
            cancel_ids = [
                o["id"]
                for o in open_orders
                if o.get("id")
                and (
                    (
                        str(o.get("side", "")).upper() == "BUY"
                        and str(o.get("asset_id", "")) == str(complement)
                    )
                    or (
                        str(o.get("side", "")).upper() == "SELL"
                        and str(o.get("asset_id", "")) in (str(held_asset), str(complement))
                    )
                )
            ]
            if cancel_ids:
                try:
                    self.api.cancel_orders(cancel_ids)
                except Exception as exc:
                    logger.warning("[exit-v2] FOK pre-cancel failed: %s", exc)
                    self.db.update_exit_cycle(
                        self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                        status=CYCLE_BLOCKED, error="cancellation failed before FOK",
                    )
                    return "BLOCKED"
                self._record_action(
                    cid, "fok_pre_cancel", "-", -1, qty,
                    "FOK 前置：撤对侧 Reward BUY 与冲突 SELL 预约",
                    f"cancel {len(cancel_ids)} orders",
                )
            try:
                refreshed_orders = self.api.get_open_orders()
            except Exception as exc:
                logger.warning("[exit-v2] FOK cancel confirmation failed: %s", exc)
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_BLOCKED, error="cancellation confirmation unavailable",
                )
                return "BLOCKED"
            conflicts = [
                o["id"]
                for o in refreshed_orders
                if o.get("id")
                and (
                    (
                        str(o.get("side", "")).upper() == "BUY"
                        and str(o.get("asset_id", "")) == str(complement)
                    )
                    or (
                        str(o.get("side", "")).upper() == "SELL"
                        and str(o.get("asset_id", "")) in (str(held_asset), str(complement))
                    )
                )
            ]
            if conflicts:
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_BLOCKED,
                    error="cancellation not reconciled; FOK withheld",
                )
                logger.warning(
                    "[exit-v2] FOK withheld %s: cancellations not reconciled (%s)",
                    cid, conflicts,
                )
                return "BLOCKED"
            try:
                positions = self.api.get_user_positions(self.api.get_funder())
            except Exception as exc:
                logger.warning("[exit-v2] FOK positions refetch failed: %s", exc)
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_BLOCKED, error="positions unavailable before FOK",
                )
                return "BLOCKED"
            fresh = self._reconcile_cycle_inventory(
                {"condition_id": cid}, positions_by_condition(positions).get(cid, [])
            )
            residual = fresh["residual_qty"]
            if residual <= EPSILON:
                cycle_row = self.db.get_active_exit_cycle(self.wallet_address, cid)
                if cycle_row:
                    self.db.update_exit_cycle(
                        cycle_row["id"], status=CYCLE_ACTIVE, selected_route="",
                    )
                return "NOOP"  # inventory changed; next tick reconciles
            comp_book = self._fresh_book(complement) or {}
            comp_asks = sorted(
                comp_book.get("asks", []), key=lambda x: float(x["price"])
            )
            merge_value, fresh_limit = complement_merge_recovery(residual, comp_asks)
            if fresh_limit is None or merge_value is None:
                cycle_row = self.db.get_active_exit_cycle(self.wallet_address, cid)
                if cycle_row:
                    self.db.update_exit_cycle(
                        cycle_row["id"], status=CYCLE_ACTIVE, selected_route="",
                    )
                self._status_add(
                    market=cid, side="-", price="-", size=str(residual), matched="-",
                    stage="库存退出", action="FOK 取消（补边深度不足）",
                    detail="补边盘口深度不足，不提交 FOK",
                )
                return "NOOP"
            held_book = self._fresh_book(held_asset) or {}
            held_bids = sorted(
                held_book.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            fresh_direct, _ = direct_recovery(held_bids, residual)
            margin = float(self._template().get("merge_advantage_min_usd", 0.01) or 0)
            if fresh_direct is not None and merge_value < fresh_direct + margin:
                # Economics changed after cancellation: abort without mutation.
                cycle_row = self.db.get_active_exit_cycle(self.wallet_address, cid)
                if cycle_row:
                    self.db.update_exit_cycle(
                        cycle_row["id"], status=CYCLE_ACTIVE, selected_route="",
                    )
                self._status_add(
                    market=cid, side="-", price="-", size=str(residual), matched="-",
                    stage="库存退出", action="FOK 取消（经济学变化）",
                    detail="撤单后重算不再满足优势下限，回退路由",
                )
                return "NOOP"
            try:
                op_id = self.db.create_merge_operation(
                    self.wallet_address, self.api.get_funder(), cid,
                    held_asset, complement, residual,
                )
            except Exception as exc:
                from models.database import ActiveMergeOperationExists

                if isinstance(exc, ActiveMergeOperationExists):
                    self.db.update_exit_cycle(
                        self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                        status=CYCLE_CLOSING,
                        error="Merge operation already active for condition",
                    )
                    return "WAIT"
                logger.error(
                    "[exit-v2] pre-FOK merge plan persist failed %s: %s", cid, exc
                )
                return "NOOP"
            try:
                self.api.place_complement_fok_buy(
                    complement, residual, fresh_limit,
                    tick_size=comp_book.get("tick_size", tick_str), neg_risk=None,
                )
            except Exception as exc:
                from api.polymarket_api import OrderRejected

                if isinstance(exc, OrderRejected):
                    self.db.update_merge_operation(op_id, "failed", error=f"FOK unfilled/rejected: {exc}")
                    self._pending_fok_until.pop(condition_key(cid), None)
                    cycle_row = self.db.get_active_exit_cycle(self.wallet_address, cid)
                    if cycle_row:
                        self.db.update_exit_cycle(
                            cycle_row["id"], status=CYCLE_ACTIVE,
                            error="", selected_route="",
                        )
                    self._record_action(
                        cid, "exit_fok_failed", "BUY", -1, residual,
                        f"FOK 补边被拒：{exc}", "no FOK inventory assumed; route fallback",
                    )
                    return "NOOP"
                # Transport uncertainty: never assume failure and market-sell.
                self.db.update_merge_operation(
                    op_id, "planned",
                    error=f"FOK submission indeterminate; awaiting inventory: {exc}",
                )
                self._pending_fok_until[condition_key(cid)] = time.time() + 30
                self._record_action(
                    cid, "exit_fok_indeterminate", "BUY", -1, residual,
                    "FOK 传输结果未知；等待库存确认，禁止并行卖出",
                    str(exc),
                )
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_CLOSING, error="FOK outcome unknown",
                )
                return "WAIT"
            self.db.update_merge_operation(
                op_id, "planned", error="FOK accepted; awaiting confirmed Data API inventory"
            )
            self._pending_fok_until[condition_key(cid)] = time.time() + 30
            self.db.add_exit_leg(
                self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                self.wallet_address, cid, LEG_COMPLEMENT_BUY,
                asset_id=complement, side="", qty=residual, price=fresh_limit,
                # Audit fix I: the signed worst limit is routing protection,
                # NOT realized cost.  Zero collateral until the complement is
                # confirmed against authoritative fills / the confirmed Merge.
                collateral=0.0,
                exit_method="FOK+MERGE", status="pending",
                note=f"exact protected FOK limit={fresh_limit:.4f}",
            )
            self._record_action(
                cid, "exit_fok_submitted", "BUY", -1, residual,
                "FOK 补边已提交；等待库存确认后 Merge",
                f"limit={fresh_limit:.4f} all-or-nothing protected buy",
            )
            self._status_add(
                market=cid, side="买入", price=f"{fresh_limit:.4f}", size=str(residual),
                matched="-", stage="库存退出", action="FOK+MERGE 已提交",
                detail="等待对侧库存确认",
            )
            return "FOK_MERGE"
        finally:
            self._release_condition_lock(mutation_lock)

    def _execute_protected_direct(
        self, cid, asset, qty, worst_price, tick, tick_str, best_bid, cost, lots,
        cycle=None,
    ):
        """Protected direct market exit (audit fix B).

        Sequence: condition lock -> get open orders -> cancel conflicting SELL
        -> if the cancel CALL fails: BLOCKED, return -> refetch open orders ->
        if a conflicting SELL is still visible: BLOCKED, return -> refetch
        positions -> recompute residual qty -> refetch held-side orderbook ->
        recompute the worst executable FAK limit -> only then submit the
        protected FAK.  A stale quantity or stale depth is never used.
        """
        mutation_lock = self._try_condition_lock(cid)
        if mutation_lock is False:
            return "WAIT"
        cycle_row = cycle or self.db.get_active_exit_cycle(self.wallet_address, cid)

        def _block(error, detail, action="⚠️BLOCKED·直卖前置未确认"):
            if cycle_row:
                self.db.update_exit_cycle(
                    cycle_row["id"], status=CYCLE_BLOCKED, error=error,
                )
            self._status_add(
                market=cid, side="卖出", price="-", size=str(qty), matched="-",
                stage="库存退出", action=action, detail=detail,
            )

        try:
            try:
                open_orders = self.api.get_open_orders()
            except Exception as exc:
                _block(
                    f"open orders unavailable before protected exit: {exc}",
                    "无法确认在挂卖单，不做任何资金变更",
                )
                return "BLOCKED"
            sell_ids = [
                o["id"] for o in open_orders
                if o.get("side") == "SELL" and o.get("asset_id") == asset and o.get("id")
            ]
            if sell_ids:
                try:
                    self.api.cancel_orders(sell_ids)
                    self._record_action(
                        cid, "exit_cancel_sell", "-", -1, qty,
                        "受保护直卖前撤掉在挂卖单", f"cancel {len(sell_ids)} SELL",
                    )
                except Exception as exc:
                    # Audit fix B: a failed cancellation call must never be
                    # followed by a FAK against reserved inventory.
                    logger.error("[exit-v2] direct pre-cancel failed: %s", exc)
                    _block(
                        f"SELL cancellation failed before protected exit: {exc}",
                        "撤单调用失败，BLOCKED；不提交 FAK",
                    )
                    return "BLOCKED"
            try:
                refreshed_orders = self.api.get_open_orders()
            except Exception as exc:
                _block(
                    f"open orders refetch failed before protected exit: {exc}",
                    "无法确认撤单结果，BLOCKED；不提交 FAK",
                )
                return "BLOCKED"
            conflicts = [
                o["id"] for o in refreshed_orders
                if o.get("side") == "SELL" and o.get("asset_id") == asset and o.get("id")
            ]
            if conflicts:
                _block(
                    "SELL cancellation not reconciled; protected FAK withheld",
                    f"撤单后仍见 {len(conflicts)} 笔在挂卖单，BLOCKED；不提交 FAK",
                )
                return "BLOCKED"
            try:
                positions = self.api.get_user_positions(self.api.get_funder())
            except Exception as exc:
                _block(
                    f"positions unavailable before protected exit: {exc}",
                    "无法确认最新持仓，BLOCKED；不提交 FAK",
                )
                return "BLOCKED"
            fresh_qty = 0.0
            for pos in positions:
                if str(pos.get("asset", "")) == str(asset):
                    try:
                        fresh_qty = float(pos.get("size", 0) or 0)
                    except (TypeError, ValueError):
                        fresh_qty = 0.0
                    break
            if fresh_qty <= EPSILON:
                # Inventory already gone (another path exited it): no FAK.
                self._status_add(
                    market=cid, side="卖出", price="-", size=str(qty), matched="-",
                    stage="库存退出", action="直卖跳过（库存已变）",
                    detail="撤单后重查持仓为 0，不提交 FAK",
                )
                return "NOOP"
            fresh_book = self._fresh_book(asset) or {}
            fresh_bids = sorted(
                fresh_book.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            fresh_value, fresh_worst = direct_recovery(fresh_bids, fresh_qty)
            if fresh_worst is None or fresh_value is None:
                _block(
                    "depth changed; no fresh worst acceptable price",
                    "盘口深度变化后无法按保护价执行，BLOCKED；不提交 FAK",
                )
                return "BLOCKED"
            fresh_best_bid = float(fresh_bids[0]["price"]) if fresh_bids else best_bid
            resp = self.api.place_marketable_limit_sell(
                asset, fresh_worst, fresh_qty, tick_size=tick_str, neg_risk=None
            )
            fill = market_fill_price(resp, fresh_best_bid, fresh_worst)
            cycle = self.db.get_active_exit_cycle(self.wallet_address, cid)
            if cycle:
                self._exit_pending.setdefault(cycle["id"], {})[asset] = (
                    time.time() + CLOSING_GRACE_SEC
                )
                self.db.add_exit_leg(
                    cycle["id"], self.wallet_address, cid, LEG_MARKET_SELL,
                    asset_id=asset, side="SELL", qty=fresh_qty, price=fresh_worst,
                    # Audit fix I: realized collateral is zero until the actual
                    # fill is confirmed from authoritative CLOB get_trades.
                    collateral=0.0, exit_method="MARKET", status="pending",
                    order_id=str((resp or {}).get("orderID", "")) if isinstance(resp, dict) else "",
                    note=f"protected FAK worst={fresh_worst:.4f} requested={fresh_qty:g}",
                )
                self.db.update_exit_cycle(
                    cycle["id"], status=CYCLE_CLOSING,
                    selected_route="MARKET", error="",
                )
            self._record_action(
                cid, "exit_market_protected", "卖出", fill, fresh_qty,
                "受保护直卖：深度最差价上界 FAK",
                f"worst={fresh_worst:.4f} requested={fresh_qty:g} fill≈{fill:.4f}",
            )
            self._status_add(
                market=cid, side="卖出", price=f"{fill:.4f}", size=str(fresh_qty), matched="-",
                stage="库存退出", action="MARKET·受保护直卖",
                detail=f"worst={fresh_worst:.4f} fill≈{fill:.4f}（已实现以成交对账为准）",
            )
            return "MARKET"
        except Exception as exc:
            logger.error("[exit-v2] protected direct exit failed %s: %s", asset, exc)
            if cycle_row:
                self.db.update_exit_cycle(
                    cycle_row["id"], status=CYCLE_ACTIVE,
                    error=f"protected direct exit failed: {exc}",
                )
            self._status_add(
                market=cid, side="卖出", price="-", size=str(qty), matched="-",
                stage="库存退出", action="⚠️直卖失败·保留",
                detail=f"受保护直卖被拒：{exc}",
            )
            return "NOOP"
        finally:
            self._release_condition_lock(mutation_lock)

    def _rest_maker_sell(
        self, cid, asset, qty, best_bid, best_ask, tick, tick_str, cost,
        open_orders, window_until, side, cycle=None,
    ):
        """Rest one POST-ONLY maker SELL for the escape window.

        Audit fix H: the V2 maker escape order is GTC + POST-ONLY — if the
        snapshot moved and the proposed SELL would cross, the CLOB rejects it
        (acceptable); it must never intentionally fall back to a taker.
        Audit fix I: a resting order is intent, not cash.  The leg is recorded
        with status=rested and zero realized collateral; only a confirmed fill
        from CLOB get_trades later turns it into realized MAKER collateral.
        """
        price = maker_escape_price(best_bid, best_ask, tick)
        if price is None:
            self._status_add(
                market=cid, side=side, price="-", size=str(qty), matched="-",
                stage="库存退出", action="MAKER 窗口（无盘口不挂）",
                detail="无买盘/无有效 tick，本 tick 不挂卖单",
            )
            return
        sells = [
            o for o in open_orders
            if o.get("side") == "SELL" and o.get("asset_id") == asset
        ]
        plan = plan_take_profit(qty, price, tick, sells)
        if plan["action"] in ("noop", "keep"):
            self._status_add(
                market=cid, side=side, price=f"{price:.4f}", size=str(qty), matched="-",
                stage="库存退出", action="MAKER 窗口·挂单保持",
                detail=f"成本{cost:.4f} 挂卖{price:.4f}（可低于成本，不穿价）",
            )
            return
        mutation_lock = self._try_condition_lock(cid)
        if mutation_lock is False:
            return
        try:
            if plan["cancel_ids"]:
                try:
                    self.api.cancel_orders(plan["cancel_ids"])
                    self._record_action(
                        cid, "exit_recancel", "-", -1, qty,
                        "撤与持仓不符的旧卖单，改挂 MAKER 逃生价", f"cancel {len(plan['cancel_ids'])} SELL",
                    )
                except Exception as exc:
                    logger.warning("[exit-v2] maker recancel failed: %s", exc)
                    return
            resp = self.api.place_post_only_sell(
                asset, price, qty, tick_size=tick_str, neg_risk=None
            )
            cycle = cycle or self.db.get_active_exit_cycle(self.wallet_address, cid)
            if cycle:
                # Superseded rested intents stay in the ledger but never count:
                # the fill reconciliation matches confirmed fills FIFO, so an
                # unfilled superseded remainder simply never realizes.
                self.db.add_exit_leg(
                    cycle["id"], self.wallet_address, cid, LEG_MAKER_SELL,
                    asset_id=asset, side="SELL", qty=qty, price=price,
                    collateral=0.0, exit_method="MAKER", status="rested",
                    order_id=str((resp or {}).get("orderID", "")) if isinstance(resp, dict) else "",
                    note=f"maker escape below-cost allowed, cost={cost:.4f}",
                )
                self.db.update_exit_cycle(
                    cycle["id"], selected_route="MAKER_WINDOW",
                    maker_window_until=window_until,
                )
            self._record_action(
                cid, "exit_maker_rest", "卖出", price, qty,
                f"MAKER 逃生窗口挂卖单（成本 {cost:.4f}，可低于成本，绝不穿价）",
                f"maker_escape_price={price:.4f} bid={best_bid} ask={best_ask}",
            )
            self._status_add(
                market=cid, side=side, price=f"{price:.4f}", size=str(qty), matched="-",
                stage="库存退出", action="MAKER 窗口·挂卖单",
                detail=f"成本{cost:.4f} 挂卖{price:.4f}",
            )
        except Exception as exc:
            logger.error("[exit-v2] maker rest failed %s: %s", asset, exc)
            self._status_add(
                market=cid, side=side, price=f"{price:.4f}", size=str(qty), matched="-",
                stage="库存退出", action="⚠️MAKER 挂单失败",
                detail=f"{exc}",
            )
        finally:
            self._release_condition_lock(mutation_lock)

    def _close_cycle(self, cycle: dict, reason: str) -> None:
        """Close a cycle once inventory is zero.

        Audit fix I: the economic summary uses only CONFIRMED legs.  When exit
        legs still await authoritative CLOB fills, the cycle closes as CLOSED
        with a LEDGER_PENDING marker and later ticks finalize it.
        """
        try:
            self._confirm_exit_legs(cycle)
            legs = self.db.get_exit_legs(cycle["id"])
            pending = [
                l for l in legs
                if l.get("kind") in EXIT_LEG_KINDS
                and l.get("status") in (LEG_RESTED, LEG_PENDING)
            ]
            duration = time.time() - float(cycle.get("opened_at", time.time()) or time.time())
            if pending:
                self.db.close_exit_cycle(
                    cycle["id"],
                    closed_reason=reason,
                    holding_duration_sec=max(0.0, duration),
                    error="LEDGER_PENDING: 待成交对账",
                )
                logger.info(
                    "[exit-v2] cycle %s closed (ledger pending, %d exit legs)",
                    cycle["id"], len(pending),
                )
            else:
                self._finalize_cycle(cycle, reason=reason)
            # Cancel any lingering stale SELL so inventory and orders converge.
            try:
                open_orders = self.api.get_open_orders()
                stale = [
                    o["id"] for o in open_orders
                    if o.get("side") == "SELL" and o.get("asset_id") in (
                        cycle.get("held_asset_id", ""),
                    ) and o.get("id")
                ]
                if stale:
                    self.api.cancel_orders(stale)
                    self._record_action(
                        cycle["condition_id"], "exit_cancel_sell", "-", -1, 0,
                        "周期关闭：撤掉残留过期卖单",
                        f"cancel {len(stale)} stale SELL",
                    )
            except Exception as exc:
                logger.warning("[exit-v2] stale SELL cleanup failed: %s", exc)
        except Exception as exc:
            logger.error("[exit-v2] close cycle %s failed: %s", cycle["id"], exc)

    # -- placement authority ---------------------------------------------------

    def authorize_placement(
        self, condition_id: str, token_id: str, outcome: str, proposed_qty: float,
        open_orders: list[dict],
    ) -> dict:
        """Final authority over Reward BUY placement for one token.

        Returns {kind, allowed, total_target, reason} (audit fix D):
        - ``total_target`` is the desired TOTAL resting opposite Reward BUY
          quantity (never an "additional allowed" delta).  The caller
          reconciles the resting book to it with reconcile_buy_orders, so the
          open quantity shrinks when the residual shrinks and drops to zero
          when the residual is gone.
        - kind="same_token_block": held token — cancel resting buys, place none.
        - kind="opposite_cap": opposite token — reconcile to total_target
          (which may be 0 => cancel all opposite Reward BUYs).
        - kind="ok": normal placement.
        Scanner/laddering proposes; this engine clamps or rejects.
        """
        try:
            cycle = self.db.get_active_exit_cycle(self.wallet_address, condition_id)
        except Exception as exc:
            logger.warning("[exit-v2] placement authority cycle lookup failed: %s", exc)
            return {
                "kind": "same_token_block", "allowed": False, "total_target": 0.0,
                "reason": "退出周期查询失败，fail-closed",
            }
        if not isinstance(cycle, dict):
            return {
                "kind": "ok", "allowed": True,
                "total_target": float(proposed_qty or 0), "reason": "",
            }
        managed = float(cycle.get("managed_qty", 0) or 0)
        held_asset = str(cycle.get("held_asset_id", "") or "")
        held_side = str(cycle.get("held_side", "") or "").upper()
        held_assets = set(json.loads(cycle.get("held_assets_json") or "[]"))
        if held_asset:
            held_assets.add(held_asset)
        # Rule #1: same held token is blocked while the cycle owns residual.
        if (
            str(token_id) in held_assets
            or (held_side and held_side == str(outcome or "").upper())
        ):
            return {
                "kind": "same_token_block", "allowed": False,
                "total_target": 0.0,
                "reason": "库存退出中：同侧持仓未退出，禁止重挂 Reward BUY（防填-亏-再买 churn）",
            }
        # Opposite Reward BUY: TOTAL target = min(proposal, unpaired residual).
        residual = max(0.0, managed - float(cycle.get("paired_qty", 0) or 0))
        target = opposite_total_target(residual, proposed_qty)
        open_qty = open_buy_qty(open_orders, token_id)
        return {
            "kind": "opposite_cap", "allowed": True,
            "total_target": target,
            "residual": residual,
            "open_qty": open_qty,
            "reason": (
                f"对侧 Reward BUY 总量目标 {target:g}（未配对残差 {residual:g}，"
                f"在挂 {open_qty:g}，提议 {proposed_qty:g}）"
            ),
        }

    # -- Merge runtime readiness (new-BUY gate) ---------------------------------

    def merge_runtime_ready(self, force: bool = False) -> tuple[bool, str]:
        """(ready, reason) for automatic-Merge runtime capability.

        The gate is only meaningful for Type3 wallets whose template enables
        fast_exit + merge + require_merge_ready_for_new_buys; Type1/Type2 and
        gate-off templates return (True, '').  ``force=True`` bypasses the
        short TTL cache (audit fix C): immediately before a funds-moving
        complement FOK the readiness must be freshly validated.
        """
        tmpl = self._template()
        if not (
            tmpl.get("fast_exit_enabled", True)
            and tmpl.get("merge_enabled", True)
            and tmpl.get("require_merge_ready_for_new_buys", True)
        ):
            return True, ""
        if int(getattr(self.api, "signature_type", -1)) != 3:
            return True, ""
        now = time.time()
        cached_at, cached_ready, cached_reason = self._readiness_cache
        if not force and now - cached_at < MERGE_READINESS_TTL_SEC:
            return cached_ready, cached_reason
        if not getattr(self.api, "trading_enabled", True):
            result = (False, "TRADING_DISABLED")
        elif self._readiness_checker is not None:
            try:
                result = tuple(self._readiness_checker())
            except Exception as exc:
                logger.warning("[exit-v2] readiness checker failed: %s", exc)
                result = (False, "RELAYER_UNREACHABLE")
        else:
            result = self._probe_merge_runtime()
        if not force:
            self._readiness_cache = (now, bool(result[0]), str(result[1]))
        return bool(result[0]), str(result[1])

    def _probe_merge_runtime(self) -> tuple[bool, str]:
        """Read-only runtime probe (expected Deposit Wallet + authenticated nonce)."""
        try:
            from api.relayer_config import run_wallet_preflight

            wallet = {
                "address": self.wallet_address,
                "signature_type": getattr(self.api, "signature_type", 3),
                "funder": self.api.get_funder() or "",
                "trading_block_reason": getattr(self.api, "trading_block_reason", ""),
            }
            result = run_wallet_preflight(
                wallet,
                getattr(self.api, "private_key", None),
                db=self.db,
                encryption_key=self.encryption_key,
                trading_enabled=getattr(self.api, "trading_enabled", True),
                timeout=10.0,
            )
            return result.ready, result.reason
        except Exception as exc:
            logger.warning("[exit-v2] merge runtime probe failed: %s", exc)
            return False, "RELAYER_UNREACHABLE"
