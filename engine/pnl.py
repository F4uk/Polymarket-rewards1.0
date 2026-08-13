"""engine/pnl.py — 每日盈亏台账纯计算（无 IO，全单测）。

口径见 spec：net = 奖励 + 返佣 + 卖出盈利 − 亏损 − 手续费。
时区固定 UTC+8（北京），不靠机器时区。成本只认 get_trades 逐笔（禁 Data API avgPrice）。
"""

from datetime import datetime, timezone, timedelta

_BJ = timezone(timedelta(hours=8))


def beijing_day(ts) -> str:
    """epoch 秒 -> 北京(UTC+8)日期 'YYYY-MM-DD'（固定偏移，不靠机器时区）。"""
    return datetime.fromtimestamp(float(ts or 0), _BJ).strftime("%Y-%m-%d")


def beijing_hour(ts) -> int:
    """epoch 秒 -> 北京(UTC+8)小时(0-23)。"""
    return datetime.fromtimestamp(float(ts or 0), _BJ).hour


def weekly_window(today_str):
    """周报窗口。给定北京日期 'YYYY-MM-DD',返回 (week_key, start, end):

    - week_key = 本周周一日期(节流键:一周只推一次)。
    - [start, end] = 最近 7 整天(截止昨天),即 [昨天-6天, 昨天]——这样最近收到的奖励立刻在报里。
    """
    d = datetime.strptime(today_str, "%Y-%m-%d")
    monday = d - timedelta(days=d.weekday())  # 本周一(周一 weekday()==0)
    yesterday = d - timedelta(days=1)
    start = yesterday - timedelta(days=6)  # 含昨天共 7 天
    return (
        monday.strftime("%Y-%m-%d"),
        start.strftime("%Y-%m-%d"),
        yesterday.strftime("%Y-%m-%d"),
    )


def _prev_day(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )


def fee_from_fill(fill) -> float:
    """taker 手续费（美元）。实测 fee_rate_bps 全 0 -> 0;公式备将来收费（未经非零验证）。"""
    bps = float(fill.get("fee_rate_bps", 0) or 0)
    if bps <= 0:
        return 0.0
    return (
        (bps / 10000.0)
        * float(fill.get("price", 0) or 0)
        * float(fill.get("size", 0) or 0)
    )


def reward_rebate_by_day(activity) -> dict:
    """/activity -> {date: {"reward": x, "rebate": y}}。

    REWARD 归 beijing_day(ts)-1（发放前一天=earning 日）;MAKER_REBATE 归 beijing_day(ts)。
    其余 type（TRADE/DEPOSIT/WITHDRAWAL/REDEEM/…）此函数忽略。
    """
    out: dict = {}
    for a in activity or []:
        t = str(a.get("type", "")).upper()
        amt = float(a.get("usdcSize", 0) or 0)
        ts = a.get("timestamp", 0)
        if t == "REWARD":
            d = _prev_day(beijing_day(ts))
            out.setdefault(d, {}).setdefault("reward", 0.0)
            out[d]["reward"] += amt
        elif t == "MAKER_REBATE":
            d = beijing_day(ts)
            out.setdefault(d, {}).setdefault("rebate", 0.0)
            out[d]["rebate"] += amt
    return out


def our_traded_assets(trades, funder) -> set:
    """我方成交涉及的全部 asset_id（taker=top-level asset;maker=maker_orders 内我方条目）。"""
    f = (funder or "").lower()
    assets = set()
    for tr in trades:
        we_maker = False
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() == f:
                we_maker = True
                assets.add(str(mo.get("asset_id", "")))
        if not we_maker and str(tr.get("trader_side", "")).upper() == "TAKER":
            assets.add(str(tr.get("asset_id", "")))
    assets.discard("")
    return assets


def realized_pnl_by_day(fills, merge_events=None) -> dict:
    """单 asset 的 extract_fills(含 fee_rate_bps)-> 按北京日的已实现盈亏。

    FIFO 回放：买入入队;卖出从最早买入 lots 对冲,对冲部分已实现盈亏
    =(卖价-买价)*量,按 beijing_day(卖出 ts) 归:正入 sell_profit、负入 loss(绝对值)。
    手续费 fee_from_fill 按该 fill 的北京日累加(买卖都算,maker=0)。无买可冲的卖量忽略。
    返回 {date: {"sell_profit", "loss", "fee"}}(去掉全 0 的空日)。
    """
    out: dict = {}

    def _bucket(d):
        return out.setdefault(d, {"sell_profit": 0.0, "loss": 0.0, "fee": 0.0})

    ordered = sorted(list(fills or []) + list(merge_events or []), key=lambda f: f.get("ts", 0) or 0)
    lots: list = []  # FIFO:[{price, remaining}]
    for f in ordered:
        side = str(f.get("side", "")).upper()
        size = float(f.get("size", 0) or 0)
        price = float(f.get("price", 0) or 0)
        ts = f.get("ts", 0)
        fee = fee_from_fill(f)
        if fee:
            _bucket(beijing_day(ts))["fee"] += fee
        if size <= 0:
            continue
        if side == "BUY":
            lots.append({"price": price, "remaining": size})
        elif side == "SELL":
            qty = size
            realized = 0.0
            matched_any = False
            while qty > 1e-9 and lots:
                lot = lots[0]
                take = min(lot["remaining"], qty)
                realized += (price - lot["price"]) * take
                lot["remaining"] -= take
                qty -= take
                matched_any = True
                if lot["remaining"] <= 1e-9:
                    lots.pop(0)
            if not matched_any:
                continue  # 无买可对冲 -> 忽略这笔卖
            d = beijing_day(ts)
            if realized >= 0:
                _bucket(d)["sell_profit"] += realized
            else:
                _bucket(d)["loss"] += -realized
    return {d: v for d, v in out.items() if v["sell_profit"] or v["loss"] or v["fee"]}
