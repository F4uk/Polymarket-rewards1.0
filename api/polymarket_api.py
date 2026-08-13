"""api/polymarket_api.py — Polymarket CLOB + Rewards API wrapper."""

import functools
import logging
import os
import time
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    BookParams,
    OrderArgs,
    OrderType,
    OrderPayload,
    MarketOrderArgsV2,
    PartialCreateOrderOptions,
    TradeParams,
    OrdersScoringParams,
)
from eth_account import Account
from py_builder_relayer_client.builder.derive import derive as _derive_safe
from py_builder_relayer_client.config import get_contract_config
from py_builder_relayer_client.client import RelayClient
from api.proxy import parse_proxy, use_proxy, http_get, install_clob_proxy

logger = logging.getLogger(__name__)


def _proxied(method):
    """实例网络方法装饰器:调用期把代理上下文设为本钱包(self.proxy_url),使其内部
    CLOB(httpx 分发器)与 http_get(requests)调用都走该钱包代理——无论从采集线程、
    监控线程还是路由直接调用。类定义末尾对所有网络实例方法统一套用(见 _PROXIED_METHODS)。
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        # getattr 兜底:极少数测试用 object.__new__ 跳过构造、不设 proxy_url -> 直连。
        with use_proxy(getattr(self, "proxy_url", None)):
            return method(self, *args, **kwargs)

    return wrapper


POLYMARKET_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet
SIG_POLY_PROXY = 1  # Email/embedded-login Polymarket Proxy Wallet
SIG_GNOSIS_SAFE = 2  # Browser-wallet proxy signature type
SIG_POLY_1271 = 3  # Polymarket Deposit Wallet (EIP-1271)

# POST /books 单次可带的 token 数。实测 500 个仍 HTTP 200(1.78s),1000 与 2000 都是
# HTTP 400。取 100 留足余量:一批同生共死,批越大一次失败丢的盘口越多。
_BOOKS_BATCH_SIZE = 100

# Rewards API is part of the CLOB API
REWARDS_API = POLYMARKET_HOST
DATA_API_HOST = "https://data-api.polymarket.com"


class OrderRejected(Exception):
    """The CLOB accepted the HTTP request (200) but did not place/fill the order.

    Polymarket's POST /order returns HTTP 200 with an application-level
    ``success``/``status`` even on logical rejection (e.g. ``success: false``
    insufficient balance) or a FOK/FAK market order that found no liquidity
    (``status: "unmatched"``). The raw client returns that body unchanged, so
    callers that only catch HTTP errors would treat a non-placed/non-filled
    order as success — recording a phantom stop-loss sale or a fake resting
    take-profit (2026-06-02 audit F2). Wrappers raise this instead.
    """


# Statuses that mean the order was accepted: resting (live/delayed) or filled
# (matched). Anything else (notably "unmatched" = FOK/FAK killed) is a failure.
_ORDER_OK_STATUSES = {"live", "matched", "delayed"}


def _check_order_resp(res, what: str):
    """Raise OrderRejected if an order response signals failure; else return it.

    Permissive on non-dict / missing fields (unknown shape -> don't break),
    strict on the two known failure signals: ``success is False`` and a present
    ``status`` outside the accepted set.
    """
    if not isinstance(res, dict):
        return res
    if res.get("success") is False:
        raise OrderRejected(f"{what} 被拒：{res.get('errorMsg') or res}")
    status = res.get("status")
    if status is not None and str(status).lower() not in _ORDER_OK_STATUSES:
        raise OrderRejected(f"{what} 未成交/未挂上（status={status}）：{res}")
    return res


def _check_cancel_resp(res, what: str = "批量撤单"):
    """撤单响应观测:应用层失败(success=False 或 not_canceled 非空)时 WARNING 记录。

    不抛异常——调用方(monitor/manager)都已 try/except 网络错且各有兜底,这里只保证
    "假装撤成功"的静默失败可见(F5)。撤单失败通常幂等(单已不在/已成交),下一轮对账
    自愈;真正的危害是看不见。
    """
    if isinstance(res, dict) and (
        res.get("success") is False or res.get("not_canceled")
    ):
        logger.warning("%s 部分/全部未撤: %s", what, res)
    return res


def derive_deposit_address(eoa_address: str) -> str:
    """Polymarket deposit-wallet (funder) address for a given signer EOA.

    Polymarket deploys a deterministic 1-of-1 Gnosis Safe per EOA via CREATE2;
    that Safe — not the EOA — holds the user's USDC/positions and is what the
    site shows as the deposit address. We derive it with the official relayer
    client so the user only ever needs to enter their private key. Matches
    signature_type=2 (POLY_GNOSIS_SAFE).
    """
    cfg = get_contract_config(CHAIN_ID)
    return _derive_safe(eoa_address, cfg.safe_factory)


def eoa_from_key(private_key: str) -> str:
    """Signer EOA (checksummed) for a private key — no network call."""
    return Account.from_key(private_key).address


def expected_type3_deposit_wallet(private_key: str, rpc_url: str | None = None) -> str:
    """Ask the official relayer SDK for the actual POLY_1271 Deposit Wallet.

    This is intentionally not a Safe derivation.  It only needs the owner key
    to derive/validate the address and sends no order or transaction.
    """
    client = RelayClient(
        os.environ.get("PMM_RELAYER_URL", "https://relayer-v2.polymarket.com"),
        CHAIN_ID,
        private_key=private_key,
        rpc_url=rpc_url or os.environ.get("PMM_POLYGON_RPC_URL") or None,
    )
    return client.get_expected_deposit_wallet()


def resolve_signature_type(derived_safe: str, funder: str) -> int:
    """Pick the Polymarket signature type for an imported wallet.

    A blank funder, or one equal to the EOA's derived Gnosis Safe, means a
    browser-wallet/Safe account (type 2 — the default we can derive). A funder
    that differs is the user's Polymarket Proxy deposit wallet (type 1, used by
    email/embedded-login accounts); we can't derive that address, so the user
    supplies it and we must sign/query as a proxy, not a Safe.
    """
    if funder and funder.lower() != derived_safe.lower():
        return SIG_POLY_PROXY
    return SIG_GNOSIS_SAFE


def pick_funded_sig_type(by_sig: dict) -> int | None:
    """Given {sig_type: collateral_balance_str}, return the sig type holding
    the most collateral, or None if all are zero/unreadable.

    The CLOB derives the wallet server-side from EOA + signature_type, so the
    type whose derived wallet actually holds pUSD is the account's real type
    (EOA=0 / proxy=1 / safe=2 / EIP-1271 smart wallet=3). This auto-detects it.
    """
    best, best_bal = None, 0
    for st, val in by_sig.items():
        try:
            bal = int(val)
        except (TypeError, ValueError):
            continue
        if bal > best_bal:
            best, best_bal = int(st), bal
    return best


class PolymarketAPI:
    """Wrapper for one wallet's Polymarket connection.

    Orders are placed from the GNOSIS_SAFE browser-wallet proxy (signature_type=2).
    """

    def __init__(
        self,
        private_key: str,
        signature_type: int = SIG_GNOSIS_SAFE,
        funder: str = None,
        proxy: str = None,
    ):
        """Initialize with private key.

        Args:
            private_key: Hex private key string.
            signature_type: 2=GNOSIS_SAFE (default, browser-wallet proxy).
            funder: Deposit-wallet (Gnosis Safe) address. If None, it is
                derived from the signer EOA via derive_deposit_address so the
                user only needs to provide a private key.
            proxy: 该钱包专属 HTTP 代理串(host:port[:user:pass] 或 http://...),
                None/空=直连。此后该钱包的所有网络活动都从这个代理 IP 出口。
        """
        self.private_key = private_key
        self.signature_type = int(signature_type)
        self.trading_enabled = True
        self.trading_block_reason = ""
        self.proxy_url = parse_proxy(proxy)
        install_clob_proxy()  # 幂等:激活 CLOB(httpx)的按代理选路
        # 构造期的网络调用(create_or_derive_api_key)也必须走该钱包代理。
        with use_proxy(self.proxy_url):
            # Step 1: Create temp client to derive API creds + the signer EOA.
            temp_client = ClobClient(
                host=POLYMARKET_HOST,
                key=private_key,
                chain_id=CHAIN_ID,
            )
            # create_or_derive (not derive-only): a freshly imported wallet has
            # no CLOB API creds yet, so derive_api_key alone fails with "Could
            # not derive api key!". create_api_key first (new wallet), fall back
            # to derive (already-onboarded wallet) — safe for both.
            api_creds = temp_client.create_or_derive_api_key()
            # The deposit wallet (where funds live) is the Polymarket Gnosis Safe
            # deterministically derived from the EOA — NOT the EOA itself. Derive
            # it when no funder is supplied.
            eoa_address = temp_client.get_address()
            # Step 2: Create full client with L2 auth
            resolved_funder = funder or derive_deposit_address(eoa_address)
            self.client = ClobClient(
                host=POLYMARKET_HOST,
                key=private_key,
                chain_id=CHAIN_ID,
                creds=api_creds,
                signature_type=self.signature_type,
                funder=resolved_funder,
            )
        if self.signature_type == SIG_POLY_1271:
            if not funder:
                self.trading_enabled = False
                self.trading_block_reason = "Type3 requires an explicit Deposit Wallet funder"
            else:
                try:
                    expected = expected_type3_deposit_wallet(private_key)
                except Exception as exc:  # validation must fail closed
                    self.trading_enabled = False
                    self.trading_block_reason = f"Type3 Deposit Wallet validation unavailable: {exc}"
                else:
                    if str(funder).lower() != str(expected).lower():
                        self.trading_enabled = False
                        self.trading_block_reason = "Type3 funder does not match the expected Deposit Wallet"

    def _require_trading_enabled(self):
        # Some existing diagnostic/unit paths construct the thin wrapper via
        # ``object.__new__`` and provide only a mocked CLOB client.  Absence of
        # the Type3 validation state therefore means legacy-compatible allowed;
        # a normally constructed Type3 wrapper always has the explicit fields.
        if not getattr(self, "trading_enabled", True):
            raise OrderRejected(
                getattr(self, "trading_block_reason", "")
                or "Automated trading is disabled"
            )

    def get_address(self) -> str:
        """Return wallet address derived from private key."""
        return self.client.get_address()

    def get_funder(self) -> str:
        """Proxy/funder address (where funds live) used for orders/data API."""
        return self.client.builder.funder

    # --- Market Data ---

    def get_orderbook(self, token_id: str) -> dict:
        """Get orderbook for a token. Returns {bids: [...], asks: [...]}."""
        return self.client.get_order_book(token_id)

    def get_orderbooks(self, token_ids: list) -> dict:
        """批量取盘口,返回 {token_id: 盘口 dict 或 None}。None = 该 token 没取到。

        CLOB 的 POST /books 一次返回多个盘口,字段与单发 GET /book 完全一致
        (asset_id/bids/asks/tick_size/neg_risk/min_order_size…,盘口深度不截断)。

        **必须按 asset_id 对齐,不能按下标 zip**:实测 /books 对失效 token 静默丢弃
        (HTTP 200 但返回条数少于请求条数),按下标会把后一个 token 的盘口安到前一个
        头上 —— Step3 拿错盘口判撤单、离场拿错买一定卖价。

        分批发,每批独立 try:一批同生共死,失败只让那批记 None,不能让一次抖动把整轮
        盘口清空(那等于 Step3 全跳过、离场全部 ⚠️裸奔)。
        """
        out = {t: None for t in token_ids}
        for i in range(0, len(token_ids), _BOOKS_BATCH_SIZE):
            chunk = token_ids[i : i + _BOOKS_BATCH_SIZE]
            try:
                resp = self.client.get_order_books(
                    [BookParams(token_id=t) for t in chunk]
                )
            except Exception as e:
                logger.warning(
                    "get_order_books 批(%d 个)失败,该批记 None: %s", len(chunk), e
                )
                continue
            by_asset = {
                b.get("asset_id"): b for b in (resp or []) if isinstance(b, dict)
            }
            for t in chunk:
                out[t] = by_asset.get(t)
        return out

    def get_spread(self, token_id: str) -> float:
        """Get spread for a token via GET /spread.

        Returns spread as float (e.g., 0.02 = 2 cents).
        Returns -1 if no orderbook exists.
        """
        try:
            resp = http_get(
                f"{POLYMARKET_HOST}/spread",
                params={"token_id": token_id},
                timeout=10,
            )
            if resp.status_code == 404:
                return -1
            resp.raise_for_status()
            return float(resp.json().get("spread", "0"))
        except Exception as e:
            logger.warning("Failed to get spread for %s: %s", token_id, e)
            return -1

    def get_last_trade_price(self, token_id: str) -> float:
        """Get last trade price for a token."""
        resp = self.client.get_last_trade_price(token_id)
        return float(resp.get("price", 0))

    # --- Balance ---

    def get_balance(self) -> float:
        """Get pUSD (collateral) balance of the proxy/safe, in human-readable units."""
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=self.signature_type,
        )
        try:
            bal = self.client.get_balance_allowance(params)
        except Exception as e:
            logger.warning(
                "get_balance failed eoa=%s funder=%s sig=%s: %s",
                self.get_address(),
                self.get_funder(),
                self.signature_type,
                e,
            )
            raise
        raw = float(bal.get("balance", 0)) if isinstance(bal, dict) else 0.0
        return raw / 1e6  # pUSD has 6 decimals

    def balance_by_sig_types(self) -> dict:
        """Diagnostic: COLLATERAL(pUSD) balance under each signature type.

        get_balance_allowance derives the wallet server-side from EOA +
        builder.signature_type (the funder we set is NOT sent), so probing all
        types tells us which one the CLOB maps to the address holding funds.
        """
        out = {}
        orig = self.client.builder.signature_type
        try:
            for st in (0, 1, 2, 3):
                try:
                    self.client.builder.signature_type = st
                    bal = self.client.get_balance_allowance(
                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    )
                    out[st] = bal.get("balance") if isinstance(bal, dict) else str(bal)
                except Exception as e:
                    out[st] = f"ERR {e}"
        finally:
            self.client.builder.signature_type = orig
        return out

    # --- Order Placement ---

    def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size: int,
        tick_size: str = "0.01",
        neg_risk: bool | None = None,
    ) -> dict:
        """Place a limit buy order.

        ``neg_risk=None`` (the default) lets the CLOB client auto-resolve the
        market's real neg_risk from ``token_id`` (it only falls back to
        ``get_neg_risk`` when the option is None). Passing a concrete True/False
        forces it — NEVER hardcode False, or orders on negative-risk markets get
        signed for the wrong exchange contract and are rejected.

        Returns dict with "orderID" and "status" keys.
        """
        self._require_trading_enabled()
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=float(size),
            side="BUY",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        # Reward buys must rest.  The official client serializes this as
        # ``postOnly`` and rejects a stale snapshot which would cross.
        res = self.client.create_and_post_order(
            order_args, options, OrderType.GTC, post_only=True
        )
        return _check_order_resp(res, "限价买单")

    def place_limit_sell(
        self,
        token_id: str,
        price: float,
        size: int,
        tick_size: str = "0.01",
        neg_risk: bool | None = None,
    ) -> dict:
        """Place a limit sell order at specified price.

        ``neg_risk=None`` (the default) lets the CLOB client auto-resolve the
        market's real neg_risk from ``token_id``. The take-profit caller does not
        know the position's neg_risk, so it relies on this auto-resolution — a
        hardcoded False here silently broke every sell on negative-risk markets
        (position couldn't be sold at all, 2026-06-02 incident).

        Returns dict with "orderID" and "status" keys.
        """
        self._require_trading_enabled()
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=float(size),
            side="SELL",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        res = self.client.create_and_post_order(order_args, options, OrderType.GTC)
        return _check_order_resp(res, "限价卖单")

    def place_market_sell(
        self,
        token_id: str,
        size: int,
        tick_size: str = "0.01",
        neg_risk: bool | None = None,
    ) -> dict:
        """Place a market sell order (FAK — fill-and-kill).

        Uses MarketOrderArgsV2 with amount = shares to sell. FAK (not FOK): a
        stop-loss must exit whatever liquidity exists *now* and kill the rest —
        FOK is all-or-nothing, so a position larger than top-of-book bid depth
        would never stop out (2026-06-02 audit F3). ``neg_risk=None`` (the
        default) lets the CLOB client auto-resolve the market's real neg_risk
        from ``token_id``; the stop-loss caller does not know it. A hardcoded
        False here meant stop-loss could never market-sell a negative-risk
        position (2026-06-02 incident).
        """
        self._require_trading_enabled()
        market_args = MarketOrderArgsV2(
            token_id=token_id,
            amount=float(size),
            side="SELL",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        res = self.client.create_and_post_market_order(
            market_args, options, OrderType.FAK
        )
        return _check_order_resp(res, "市价卖单")

    def place_complement_fok_buy(
        self,
        token_id: str,
        size: float,
        limit_price: float,
        tick_size: str = "0.01",
        neg_risk: bool | None = None,
    ) -> dict:
        """Protected all-or-nothing complement purchase for an urgent merge.

        ``size`` and ``limit_price`` come from executable ask depth.  The SDK's
        V2 market BUY uses ``amount`` as collateral and ``price`` as the limit;
        therefore the signed maximum collateral is ``size * limit_price`` and
        the signed token amount remains exactly ``size``. It is intentionally
        separate from a reward quote and can never fall back to GTC or a
        resting order.
        """
        self._require_trading_enabled()
        if size <= 0 or limit_price <= 0 or limit_price >= 1:
            raise OrderRejected("补全 FOK 的数量或限价无效")
        args = MarketOrderArgsV2(
            token_id=token_id,
            amount=float(size) * float(limit_price),
            side="BUY",
            price=float(limit_price),
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        return _check_order_resp(
            self.client.create_and_post_market_order(args, options, OrderType.FOK),
            "补全 FOK 买单",
        )

    def place_marketable_limit_sell(
        self,
        token_id: str,
        price: float,
        size: int,
        tick_size: str = "0.01",
        neg_risk: bool | None = None,
    ) -> dict:
        """限价卖但 FAK(fill-and-kill):吃掉所有 >= price 的买单后 kill 剩余,永不挂簿。

        用于 v4 §7 B 段逐级扫单——以「最低卖出价」为限价的市价单,绝不成交在该价以下
        (盘口瞬时假跌也卖不穿,自带防错杀)。neg_risk=None 走自动解析(与其它卖单一致,
        防负风险仓卖不出)。
        """
        self._require_trading_enabled()
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=float(size),
            side="SELL",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        res = self.client.create_and_post_order(order_args, options, OrderType.FAK)
        return _check_order_resp(res, "限价扫单(FAK)")

    # --- Order Management ---

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order by ID."""
        return _check_cancel_resp(
            self.client.cancel_order(OrderPayload(orderID=order_id)), "撤单"
        )

    def cancel_orders(self, order_ids: list) -> dict:
        """Batch-cancel multiple orders by ID in a single request."""
        return _check_cancel_resp(self.client.cancel_orders(order_ids), "批量撤单")

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders for this wallet."""
        return _check_cancel_resp(self.client.cancel_all(), "全撤")

    def get_order(self, order_id: str) -> dict:
        """Get order details by ID.

        Returns dict with keys: id, status, side, original_size,
        size_matched, price, asset_id, market, etc.
        """
        return self.client.get_order(order_id)

    def get_open_orders(self) -> list:
        """Get all open orders for this wallet.

        Returns list of order dicts with keys: id, status, side,
        original_size, size_matched, price, asset_id, market, etc.
        """
        return self.client.get_open_orders()

    def get_market(self, condition_id: str) -> dict:
        """Authenticated CLOB market metadata, including complementary tokens."""
        return self.client.get_market(condition_id)

    def get_trades(self, params: TradeParams = None) -> list:
        """Get trade history for this wallet (auto-paginated)."""
        return self.client.get_trades(params)

    def are_orders_scoring(self, order_ids: list) -> dict:
        """Batch-query whether orders are scoring (in reward band).

        Returns dict keyed by order id -> bool. Empty dict for empty input.
        """
        if not order_ids:
            return {}
        return self.client.are_orders_scoring(OrdersScoringParams(orderIds=order_ids))

    def get_user_positions(self, user_address: str) -> list:
        """Polymarket Data API: current positions for a user.

        Returns a list of position dicts. Confirmed fields (documented
        sample): asset (=asset_id), size, avgPrice, curPrice, conditionId
        (=market), outcome, title. user_address is the proxy/funder.
        """
        resp = http_get(
            f"{DATA_API_HOST}/positions",
            # limit 抬高单页上限,避免持仓被服务端默认页大小(约 100)静默截断 ->
            # 漏离场/止损(F7)。本 app 并发市场上限 ~10 -> 持仓 ≤~20,500 足够,单请求。
            params={"user": user_address, "limit": 500, "sizeThreshold": 0},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def get_activity(self, types=None, start=None, end=None) -> list:
        """Data API /activity（公开，按 funder）。offset 翻页至末页。

        types: 可选 type 白名单（逗号拼接）;start/end: epoch 秒窗口。用于盈亏台账取
        奖励(REWARD)/返佣(MAKER_REBATE)/赎回(REDEEM)。走本钱包代理(self.proxy_url)。
        """
        user = self.get_funder()
        limit = 500
        offset = 0
        out: list = []
        while True:
            params = {"user": user, "limit": limit, "offset": offset}
            if types:
                params["type"] = ",".join(types)
            if start is not None:
                params["start"] = int(start)
            if end is not None:
                params["end"] = int(end)
            resp = http_get(f"{DATA_API_HOST}/activity", params=params, timeout=20)
            resp.raise_for_status()
            page = resp.json()
            if not isinstance(page, list) or not page:
                break
            out.extend(page)
            if len(page) < limit or offset + limit >= 10000:  # 末页 / offset 上限
                break
            offset += limit
        return out

    # --- Rewards (CLOB API - public endpoints) ---

    @staticmethod
    def get_rewards_markets(
        min_price: float = None,
        max_price: float = None,
        max_spread: float = None,
        order_by: str = "rate_per_day",
        position: str = "DESC",
        max_pages: int = 5,
        tag_slug: str = None,
    ) -> list[dict]:
        """Fetch markets with active rewards using GET /rewards/markets/multi.

        Supports server-side filtering and sorting:
          - min_price/max_price: filter by first token price
          - max_spread: filter by current spread
          - order_by: sort field (rate_per_day, volume_24hr, spread, end_date, etc.)
          - position: ASC or DESC

        Returns list of dicts with keys:
          - condition_id, market_id, question, market_slug, end_date
          - tokens: [{token_id, outcome, price}, ...]
          - rewards_max_spread, rewards_min_size
          - rewards_config: [{rate_per_day, total_rewards, end_date, ...}, ...]
          - spread, volume_24hr
        """
        all_markets = []
        next_cursor = ""
        page = 0
        while True:
            page += 1
            # One page of 100 (sorted by rate_per_day DESC) already covers every
            # reward-qualifying market; next_cursor pagination covers the rest.
            # Do NOT combine a large page_size with min_price/max_price + order_by
            # — that combination makes this endpoint hang / 500 (see scanner.py).
            params = {"page_size": 100}
            if tag_slug is not None:
                params["tag_slug"] = tag_slug
            if min_price is not None:
                params["min_price"] = min_price
            if max_price is not None:
                params["max_price"] = max_price
            if max_spread is not None:
                params["max_spread"] = max_spread
            if order_by:
                params["order_by"] = order_by
            if position:
                params["position"] = position
            if next_cursor:
                params["next_cursor"] = next_cursor
            logger.info("Fetching rewards markets page %d...", page)

            # Retry per page
            resp_data = None
            for attempt in range(3):
                try:
                    resp = http_get(
                        f"{REWARDS_API}/rewards/markets/multi",
                        params=params,
                        timeout=30,
                    )
                    resp.raise_for_status()
                    resp_data = resp.json()
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning(
                            "Page %d attempt %d failed: %s, retrying...",
                            page,
                            attempt + 1,
                            e,
                        )
                        time.sleep(2)
                    else:
                        logger.error("Page %d failed after 3 attempts: %s", page, e)

            if resp_data is None:
                break

            markets = resp_data.get("data", [])
            all_markets.extend(markets)
            logger.info(
                "Page %d: got %d markets (total: %d)",
                page,
                len(markets),
                len(all_markets),
            )
            next_cursor = resp_data.get("next_cursor", "LTE=")
            if next_cursor == "LTE=" or not markets or page >= max_pages:
                break

        return all_markets

    @staticmethod
    def get_rewards_current() -> list[dict]:
        """Fetch current active rewards configurations using /rewards/markets/current.

        Returns list of dicts with keys:
          - condition_id, rewards_max_spread, rewards_min_size
          - rewards_config: [{rate_per_day, total_rewards, ...}, ...]
        """
        all_rewards = []
        next_cursor = ""
        try:
            while True:
                params = {}
                if next_cursor:
                    params["next_cursor"] = next_cursor
                resp = http_get(
                    f"{REWARDS_API}/rewards/markets/current",
                    params=params,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                rewards = data.get("data", [])
                all_rewards.extend(rewards)
                next_cursor = data.get("next_cursor", "LTE=")
                if next_cursor == "LTE=" or not rewards:
                    break
        except Exception as e:
            logger.error("Failed to fetch current rewards: %s", e)
        return all_rewards

    # --- Gamma API (market details) ---

    @staticmethod
    def get_market_by_id(market_id: str) -> dict:
        """Get market details from Gamma API including end_date, tokens, etc.

        Endpoint: GET https://gamma-api.polymarket.com/markets/{id}
        """
        try:
            resp = http_get(
                f"https://gamma-api.polymarket.com/markets/{market_id}",
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Failed to get market %s from Gamma API: %s", market_id, e)
            return {}

    @staticmethod
    def list_markets(**filters) -> list[dict]:
        """List markets from Gamma API with filters.

        Endpoint: GET https://gamma-api.polymarket.com/markets
        Supports filters: active, closed, end_date_min, end_date_max, etc.
        """
        try:
            resp = http_get(
                "https://gamma-api.polymarket.com/markets",
                params=filters,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Failed to list markets from Gamma API: %s", e)
            return []

    @staticmethod
    def gamma_markets_by_condition(condition_ids: list) -> dict:
        """按 condition_id 批量解析市场名+slug(Gamma 公共接口)。

        GET /markets?condition_ids=a&condition_ids=b... 返回 Gamma 已知的市场。
        返回 {condition_id: {"name", "market_slug", "event_slug"}}。
        HTTP/网络失败时抛出(由调用方决定如何处理)。
        """
        ids = [c for c in dict.fromkeys(condition_ids) if c]
        if not ids:
            return {}
        resp = http_get(
            "https://gamma-api.polymarket.com/markets",
            params=[("condition_ids", c) for c in ids],
            timeout=10,
        )
        resp.raise_for_status()
        out = {}
        for m in resp.json():
            cid = m.get("conditionId", "")
            if not cid:
                continue
            evs = m.get("events") or []
            out[cid] = {
                "name": m.get("question", ""),
                "market_slug": m.get("slug", ""),
                "event_slug": (evs[0].get("slug", "") if evs else ""),
            }
        return out

    @staticmethod
    def gamma_resolution_status(condition_ids: list) -> dict:
        """批量取每个 condition_id 的 umaResolutionStatus(Gamma 公共接口,免 auth)。

        GET /markets?condition_ids=a&condition_ids=b... 返回 {condition_id: status
        或 None}。UMA proposal 提交后该字段变非空(proposed->disputed->resolved);
        正常交易为 None。网络/HTTP 失败 -> 返回 {}(fail-open:Gamma 抖动时调用方
        一律不撤/不跳过,绝不因一次接口失败误撤全仓买单)。
        """
        ids = [c for c in dict.fromkeys(condition_ids) if c]
        if not ids:
            return {}
        try:
            resp = http_get(
                "https://gamma-api.polymarket.com/markets",
                params=[("condition_ids", c) for c in ids],
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning("gamma_resolution_status failed (fail-open): %s", e)
            return {}
        out = {}
        for m in resp.json():
            cid = m.get("conditionId", "")
            if cid:
                out[cid] = m.get("umaResolutionStatus")
        return out

    # --- Per-market rewards ---

    @staticmethod
    def get_rewards_for_market(condition_id: str) -> list[dict]:
        """Get raw rewards for a specific market.

        Endpoint: GET /rewards/markets/{condition_id}
        Returns **one item per market** (not per token) carrying that market's
        `rewards_config` with `rate_per_day` — verified by a 2026-07-26 census of
        the 500 live reward markets (91 checked against this endpoint, 1 item
        each, always 1 config entry).

        分页中途出错一律返回 `[]`,**绝不返回已取到的半份数据**:调用方会把这些项
        求和当成市场的每日奖励(`extract_daily_rate`),半份会算出一个「看着合理但
        偏小」的奖励额,足以让正在赚奖励的在挂买单被误判跌破门槛而撤掉(fail-close)。
        返回空列表则调用方看到的是「取不到」-> 安全跳过。
        """
        all_data = []
        next_cursor = ""
        try:
            while True:
                params = {}
                if next_cursor:
                    params["next_cursor"] = next_cursor
                resp = http_get(
                    f"{REWARDS_API}/rewards/markets/{condition_id}",
                    params=params,
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("data", [])
                all_data.extend(items)
                next_cursor = data.get("next_cursor", "LTE=")
                if next_cursor == "LTE=" or not items:
                    break
        except Exception as e:
            logger.error("Failed to get rewards for market %s: %s", condition_id, e)
            return []
        return all_data


# 给所有「实例网络方法」统一套上代理上下文(见 _proxied):凡经某钱包 API 实例发出的
# CLOB / Data API 调用,都从该钱包的代理 IP 出口,无论从哪个线程/路由调用。静态方法
# (rewards/gamma 公共市场数据,非钱包身份)不在此列,靠操作边界(_tick/place_orders/
# 采集器)设的环境代理走采集所用钱包的代理。
_PROXIED_METHODS = (
    "get_orderbook",
    "get_orderbooks",
    "get_spread",
    "get_last_trade_price",
    "get_balance",
    "balance_by_sig_types",
    "place_limit_buy",
    "place_limit_sell",
    "place_market_sell",
    "place_marketable_limit_sell",
    "place_complement_fok_buy",
    "cancel_order",
    "cancel_orders",
    "cancel_all_orders",
    "get_order",
    "get_open_orders",
    "get_market",
    "get_trades",
    "are_orders_scoring",
    "get_user_positions",
    "get_activity",
)
for _name in _PROXIED_METHODS:
    setattr(PolymarketAPI, _name, _proxied(getattr(PolymarketAPI, _name)))
