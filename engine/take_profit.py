"""engine/take_profit.py — Pure position-driven exit planning (no IO).

``cost`` is the position's weighted-average cost reconstructed from our real CLOB
``get_trades`` fills (``position_cost_with_lots``: replay buys/sells, FIFO-net,
remaining lots = current position), NOT the Polymarket Data
API ``avgPrice`` — the Data API avgPrice was observed to glitch on freshly
opened positions (a real 0.28 buy read as ~0.21, dumping the position at
market). A single resting sell also replaces the older per-fill sells that split
one position into many orders priced off divergent per-fill data.
"""

import math
import time


def ceil_to_tick(price: float, tick: float) -> float:
    """Smallest tick-aligned price >= ``price`` (sells never go below cost).

    A price already sitting on a tick (within float noise) stays put rather
    than bumping a whole tick up — a get_trades weighted cost carries float
    dirt like 0.30000002 that must be treated as 0.30, not 0.31.
    """
    if tick <= 0:
        return price
    units = price / tick
    nearest = round(units)
    if abs(units - nearest) < 1e-4:  # on a tick (modulo float dirt)
        return round(nearest * tick, 10)
    return round(math.ceil(units) * tick, 10)


def _remaining(o: dict) -> float:
    return float(o.get("original_size", 0) or 0) - float(o.get("size_matched", 0) or 0)


def _price_matches(a: float, b: float, tick: float) -> bool:
    return abs(a - b) < tick / 2


def _size_matches(a: float, b: float) -> bool:
    # Tolerate sub-share drift (partial fills / float) so we don't churn.
    return abs(a - b) <= max(1.0, 0.01 * b)


def plan_take_profit(
    size: float, want_price: float, tick: float, existing_sells: list[dict]
) -> dict:
    """对一个持仓的 SELL 们做对账,使恰好一笔卖单挂在 want_price、覆盖整个 size。

    want_price 由调用方预先算好(已对齐 tick、已含穿价护栏),本函数不再加工价格。返回 {"action","price","size",
    "cancel_ids"}:noop / keep / replace。
    """
    if size <= 0 or want_price is None or want_price <= 0 or tick <= 0:
        return {"action": "noop", "price": None, "size": 0.0, "cancel_ids": []}
    want = want_price
    ids = [o.get("id") for o in existing_sells]
    remaining = sum(_remaining(o) for o in existing_sells)
    if (
        len(existing_sells) == 1
        and _price_matches(float(existing_sells[0].get("price", 0) or 0), want, tick)
        and _size_matches(remaining, size)
    ):
        return {"action": "keep", "price": want, "size": size, "cancel_ids": []}
    return {"action": "replace", "price": want, "size": size, "cancel_ids": ids}


def consume_fifo_lots(lots: list[dict], qty: float) -> tuple[list[dict], float]:
    """Consume ``qty`` FIFO shares and return (consumed lots, total cost).

    The input lots use the public ``take`` shape returned by
    :func:`position_cost_with_lots`.  Keeping this tiny helper pure allows a
    confirmed Merge to persist the exact basis it consumed instead of treating
    its returned collateral as all profit.
    """
    remaining = float(qty or 0)
    consumed, total = [], 0.0
    for lot in lots or []:
        if remaining <= 1e-9:
            break
        available = float(lot.get("take", lot.get("remaining", 0)) or 0)
        take = min(max(0.0, available), remaining)
        if take <= 0:
            continue
        item = dict(lot)
        item["take"] = take
        consumed.append(item)
        total += float(item.get("price", 0) or 0) * take
        remaining -= take
    if remaining > 1e-6:
        return [], 0.0
    return consumed, total


def remaining_fifo_lots(
    fills: list[dict], merge_consumptions: list[dict] | None = None
) -> list[dict]:
    """Replay buys, sells, and merges into an unvalidated FIFO queue.

    Confirmation accounting needs the pre-merge queue even while the Data API
    is stale, so it must not depend on the current reported position size.
    Public callers should still use :func:`position_cost_with_lots`, which
    validates that reconstructed inventory matches the Data API.
    """
    events = list(fills or []) + list(merge_consumptions or [])
    lots: list[dict] = []
    for f in sorted(events, key=lambda item: item.get("ts", 0) or 0):
        try:
            fsize = float(f.get("size", 0) or 0)
        except (TypeError, ValueError):
            continue
        if fsize <= 0:
            continue
        side = str(f.get("side", "")).upper()
        if side == "BUY":
            lots.append(
                {
                    "price": float(f.get("price", 0) or 0),
                    "remaining": fsize,
                    "ts": float(f.get("ts", 0) or 0),
                    "trade_id": f.get("trade_id", ""),
                }
            )
        elif side in ("SELL", "MERGE"):
            qty = fsize
            while qty > 1e-9 and lots:
                lot = lots[0]
                take = min(lot["remaining"], qty)
                lot["remaining"] -= take
                qty -= take
                if lot["remaining"] <= 1e-9:
                    lots.pop(0)
    return lots


def position_cost_with_lots(
    fills: list[dict], size: float, merge_consumptions: list[dict] | None = None
):
    """当前持仓的加权成本 + 剩余逐笔持仓明细,严格由成交流(买入∪卖出)重建。

    按 ts 正序回放我们在该 token 的全部成交:买入入 FIFO 队列,卖出从最早一笔开始
    抵消;回放完后队列里剩下的就是当前真实持仓,其加权均价即成本。已卖出的旧买单会被
    抵消、移出队列,绝不污染成本(老 bug:不看卖出、从新到旧凑 size,会把早已平掉的
    旧买入当成本,导致按错价亏本市价卖)。重建出的剩余持仓数量必须与 Data API 持仓
    size 吻合(容差 max(1.0, 0.01*size)),否则成本不可信(成交流相对 Data API 滞后、
    或外部转入的仓)-> (None, []),由调用方走"跳过+⚠️告警"、下个 tick 自愈。
    返回 (cost_or_None, lots);lots 形如 {price, take(剩余持仓量), ts, trade_id}。
    """
    if size <= 0 or not fills:
        return None, []
    lots = remaining_fifo_lots(fills, merge_consumptions)
    recon = sum(l["remaining"] for l in lots)
    if recon <= 0:
        return None, []
    if abs(recon - size) > max(1.0, 0.01 * size):
        return None, []
    cost_sum = sum(l["price"] * l["remaining"] for l in lots)
    result_lots = [
        {
            "price": l["price"],
            "take": l["remaining"],
            "ts": l["ts"],
            "trade_id": l["trade_id"],
        }
        for l in lots
    ]
    return cost_sum / recon, result_lots


_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _fmt_share(x: float) -> str:
    """份额去掉无意义的 .0;非整数保留两位小数。"""
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else f"{x:.2f}"


def _short_tid(tid) -> str:
    tid = str(tid or "")
    return tid if len(tid) <= 12 else f"{tid[:6]}..{tid[-4:]}"


def describe_cost_basis(cost, lots: list[dict], max_lots: int = 6) -> str:
    """成本构成的中文片段(纯函数),供止盈/止损卖单理由引用。

    lots 来自 position_cost_with_lots,按 ts 正序(最早->最新)逐笔列:
    "①时间 价格×份额股 [trade 缩写id]"。超过 max_lots 笔时列前 max_lots 笔
    + "…等共N笔"。时间用本地时区 MM-DD HH:MM。cost 为 None 时给降级文案。
    """
    n = len(lots)
    if cost is None or n == 0:
        return "成本=无（无买入成交）"
    ordered = sorted(lots, key=lambda l: l.get("ts", 0) or 0)
    total_take = sum(float(l.get("take", 0) or 0) for l in ordered)
    parts = []
    for i, l in enumerate(ordered[:max_lots]):
        mark = _CIRCLED[i] if i < len(_CIRCLED) else f"{i + 1}."
        t = time.strftime("%m-%d %H:%M", time.localtime(float(l.get("ts", 0) or 0)))
        parts.append(
            f"{mark}{t} {float(l.get('price', 0) or 0):.4f}"
            f"×{_fmt_share(float(l.get('take', 0) or 0))}股 "
            f"[trade {_short_tid(l.get('trade_id', ''))}]"
        )
    more = f" …等共{n}笔" if n > max_lots else ""
    return (
        f"成本={cost:.4f}（加权自{n}笔买入成交："
        f"{' '.join(parts)}{more} 共取{_fmt_share(total_take)}股）"
    )


def effective_theta_stop(cost, mode, percent, cents):
    """B0 强平的有效阈值(价位单位 = ¢/100)。三种模式:

    - "off":返回 None,调用方据此彻底关闭 B0 强平(套牢仓无兜底,用户明确选择)。
    - "percent"(默认):成本 × 百分比/100,即相对成本的最大回撤(默认 20%)。成本越高、
      允许的绝对亏损越大;成本越低、止损越紧。
    - "fixed":固定美分 / 100(原行为,默认 5¢),与成本无关的固定亏损额。

    任一参数异常时回退固定美分,绝不让止损阈值算成 0/负(那会一触即发或永不触发)。
    """
    if str(mode) == "off":
        return None
    try:
        if str(mode) == "fixed":
            return max(0.0, float(cents) / 100.0)
        return max(0.0, float(cost) * float(percent) / 100.0)
    except (TypeError, ValueError):
        try:
            return max(0.0, float(cents) / 100.0)
        except (TypeError, ValueError):
            return 0.05


def market_fill_price(resp, best_bid, cur):
    """市价清仓(B0)的真实成交价,**绝不用 Data API 现价**。

    现价(curPrice)常与盘口背离——薄盘口里现价 0.13、买一却只有 0.06,拿现价当成交价
    会把一笔亏损显示成盈利(用户实报 2026-06-24)。取价优先级:
    1. 市价单响应里的成交额:takingAmount(收到 USDC)/ makingAmount(卖出份额)= 均价,
       仅当比值是合理价位 (0,1] 才采信(防字段缺失/语义反/小数位不一致)。
    2. 退回盘口买一 best_bid —— FAK 市价卖从买一往下吃,买一是真实成交的起点价(上界)。
    3. 再退回现价 cur(仅当连买一都没有,理论上 B0 不会走到这)。
    """
    try:
        if isinstance(resp, dict):
            making = float(resp.get("makingAmount") or 0)  # 卖出份额
            taking = float(resp.get("takingAmount") or 0)  # 收到 USDC
            if making > 0 and taking > 0:
                p = taking / making
                if 0 < p <= 1:
                    return p
    except (TypeError, ValueError):
        pass
    if best_bid is not None and best_bid > 0:
        return best_bid
    return cur


def plan_exit(
    cost,
    best_bid,
    best_ask,
    tick,
    theta_loss,
    theta_stop,
    case_a_mode,
    size,
    take_profit_mode="maker",
):
    """两段式离场决策。theta_stop 为价位单位(=¢/100),None 表示关闭 B0 强平。

    take_profit_mode:
      - "maker"(默认,现状):盈利(成本 ≤ 买一)挂卖一做 maker 捕价差(对成本取下限);
        套牢挂成本价;亏损 ≥ theta_stop 市价止损(B0)。
      - "market":浮盈(成本 < 买一)立即市价清仓(A_market;买一>成本,成交必 ≥ 成本);
        保本/套牢(成本 ≥ 买一)挂成本价等回本;theta_stop 非 None 时仍有 B0 兜底。

    返回 {"tier","action","price","size"};action ∈ {"rest","market","noop"}。
    cost>0、size>0 由调用方在调用前保证(成本取不到 -> 裸奔跳过,不进此函数)。
    两个模式都绝不低于成本卖出 —— 唯一认亏出口是 B0 强平(theta_stop 为 None 时无此出口)。
    theta_loss / case_a_mode 已不再使用(签名保留以免改动调用方)。
    """
    cost_floor = ceil_to_tick(cost, tick)

    if take_profit_mode == "market":
        # 浮盈:成本 < 买一 -> 立即市价卖出(买一>成本,成交必 ≥ 成本)。
        if best_bid is not None and cost < best_bid:
            return {"tier": "A_market", "action": "market", "price": None, "size": size}
        # 兜底止损(仅在开启时):亏损 ≥ theta_stop 市价清仓。
        if (
            theta_stop is not None
            and best_bid is not None
            and (cost - best_bid) >= theta_stop
        ):
            return {"tier": "B0", "action": "market", "price": None, "size": size}
        # 完全无盘口:不挂进死 book。
        if best_bid is None and (best_ask is None or best_ask <= 0):
            return {"tier": "none", "action": "noop", "price": None, "size": 0.0}
        # 保本/套牢:挂成本价等回本(绝不低于成本)。
        return {"tier": "B_park", "action": "rest", "price": cost_floor, "size": size}

    # --- maker 模式(默认,现状) ---
    # 盈利:成本 ≤ 买一 -> 挂卖一做 maker 捕捉价差;但**绝不挂在成本下方**——盘口异常/买一
    # 是幻影滞后报价时,卖一可能 < 成本(正常簿里卖一≥买一≥成本,这是对异常簿的兜底),
    # 故对成本取下限。无卖一则回退成本价。
    if best_bid is not None and cost <= best_bid:
        if best_ask is not None and best_ask > 0:
            price = max(best_ask, cost_floor)
        else:
            price = cost_floor
        return {"tier": "A", "action": "rest", "price": price, "size": size}

    # 成本 > 买一:套牢/保本。亏损过大兜底市价止损(theta_stop None 时关闭,无买盘时跳过)。
    if (
        theta_stop is not None
        and best_bid is not None
        and (cost - best_bid) >= theta_stop
    ):
        return {"tier": "B0", "action": "market", "price": None, "size": size}

    # 完全无盘口(无买无卖):不挂进死 book。
    if best_bid is None and (best_ask is None or best_ask <= 0):
        return {"tier": "none", "action": "noop", "price": None, "size": 0.0}

    # 套牢/无买盘:挂成本价等回本(绝不低于成本)。
    return {"tier": "B_park", "action": "rest", "price": cost_floor, "size": size}
