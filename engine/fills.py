# engine/fills.py
"""Pure fill-detection logic (no network/IO).

Polymarket get_trades: top-level fields are the taker/aggregate view. OUR
fills are in trade["maker_orders"], filtered by maker_address == our funder.
A resting BUY of ours that got filled appears as a maker_orders entry with
side == "BUY". Dedup is per (trade_id, order_id).
"""


def select_new_buy_fills(trades: list[dict], funder: str, seen_keys: set) -> list[dict]:
    """Flatten get_trades into our unseen BUY fill events, oldest-first.

    Each event: {trade_id, order_id, asset_id, price, size, market, ts}
    where price/size/asset_id come from the matching maker_orders entry and
    market/ts come from the trade's top-level fields. seen_keys holds
    (trade_id, order_id) tuples already processed.
    """
    f = (funder or "").lower()
    events = []
    for tr in trades:
        trade_id = tr.get("id")
        market = tr.get("market", "")
        ts = float(tr.get("match_time", 0) or 0)
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() != f:
                continue
            if str(mo.get("side", "")).upper() != "BUY":
                continue
            order_id = mo.get("order_id")
            if (trade_id, order_id) in seen_keys:
                continue
            events.append(
                {
                    "trade_id": trade_id,
                    "order_id": order_id,
                    "asset_id": mo.get("asset_id", ""),
                    "price": float(mo.get("price", 0) or 0),
                    "size": float(mo.get("matched_amount", 0) or 0),
                    "market": market,
                    "ts": ts,
                }
            )
    events.sort(key=lambda e: e["ts"])
    return events


def extract_fills(trades: list[dict], funder: str, asset_id: str) -> list[dict]:
    """挑出我们在某 token 上的全部成交(BUY 和 SELL),用于按时间回放重建持仓成本。

    返回 [{side, price, size, ts, trade_id}, ...](不去重、不依赖 seen_keys)。两种来源:
    - maker:我们挂的单被吃,成交在 trade.maker_orders 里(按 funder 过滤,asset 匹配);
      side=mo.side,price=mo.price,size=mo.matched_amount。
    - taker:我们的单下单瞬间以 taker 立即成交(含止损市价平仓),成交在 trade 顶层
      (maker_orders 里是对手方,不是我们)。当 trade.trader_side==TAKER 且 asset 匹配:
      side=trade.side,price=trade.price,size=trade.size。
    一笔 trade 对我们而言要么是 maker 要么是 taker,两分支天然互斥,不会重复计入。
    ts 取 trade 顶层 match_time。
    """
    f = (funder or "").lower()
    a = str(asset_id)
    out = []
    for tr in trades:
        trade_id = tr.get("id")
        ts = float(tr.get("match_time", 0) or 0)
        we_are_maker = False
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() != f:
                continue
            we_are_maker = True  # 我们在这笔 trade 里是 maker -> 不可能同时是 taker
            if str(mo.get("asset_id", "")) != a:
                continue
            out.append(
                {
                    "side": str(mo.get("side", "")).upper(),
                    "price": float(mo.get("price", 0) or 0),
                    "size": float(mo.get("matched_amount", 0) or 0),
                    "ts": ts,
                    "trade_id": trade_id,
                    "fee_rate_bps": float(mo.get("fee_rate_bps") or 0),
                }
            )
        if (
            not we_are_maker
            and str(tr.get("trader_side", "")).upper() == "TAKER"
            and str(tr.get("asset_id", "")) == a
        ):
            out.append(
                {
                    "side": str(tr.get("side", "")).upper(),
                    "price": float(tr.get("price", 0) or 0),
                    "size": float(tr.get("size", 0) or 0),
                    "ts": ts,
                    "trade_id": trade_id,
                    "fee_rate_bps": float(tr.get("fee_rate_bps") or 0),
                }
            )
    return out


def extract_buy_fills(trades: list[dict], funder: str, asset_id: str) -> list[dict]:
    """我们在某 token 上的全部 BUY 成交(extract_fills 的 BUY 视图,去掉 side/费率键)。

    成本重建不需手续费,故一并去掉 fee_rate_bps,保持既有 BUY 视图字段不变。"""
    return [
        {k: v for k, v in f.items() if k not in ("side", "fee_rate_bps")}
        for f in extract_fills(trades, funder, asset_id)
        if f["side"] == "BUY"
    ]


def bot_buy_fills(trades: list[dict], funder: str) -> list[dict]:
    """Our maker BUY fills flattened WITH order_id (ownership provenance).

    Used by the Inventory Exit Engine to prove that inventory belongs to this
    application: a fill is only bot-managed when its ``order_id`` matches a
    Reward BUY order the application itself persisted (bot_buy_orders).  This
    helper deliberately does not change ``extract_fills``' output shape.
    """
    f = (funder or "").lower()
    out = []
    for tr in trades or []:
        ts = float(tr.get("match_time", 0) or 0)
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() != f:
                continue
            if str(mo.get("side", "")).upper() != "BUY":
                continue
            out.append(
                {
                    "order_id": mo.get("order_id", ""),
                    "asset_id": mo.get("asset_id", ""),
                    "price": float(mo.get("price", 0) or 0),
                    "size": float(mo.get("matched_amount", 0) or 0),
                    "ts": ts,
                }
            )
    return out
