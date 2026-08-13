# engine/positions.py
"""Pure helpers derived from Polymarket Data API /positions (no network/IO)."""


def held_side_info(positions: list[dict]):
    """从 Data API /positions 提取按侧(asset)暂停信息 + 按市场已持仓敞口。

    返回 (held_assets, value_by_market, shares_by_market):
      held_assets:      {asset_id}  size>0 的 token(该侧暂停新买单)。
      value_by_market:  {conditionId: Σ size×curPrice}  已持仓市值(扣减另一侧预算)。
      shares_by_market: {conditionId: Σ size}            已持仓份数(扣减份数预算)。

    size<=0 的项整项忽略;缺 asset/conditionId 只跳过对应聚合。市值用 curPrice
    (持仓当前市值),绝不用 avgPrice。size/curPrice 做 None/字符串安全转换。
    """
    held_assets: set[str] = set()
    value_by_market: dict[str, float] = {}
    shares_by_market: dict[str, float] = {}
    for p in positions:
        size = float(p.get("size", 0) or 0)
        if size <= 0:
            continue
        asset = p.get("asset", "")
        if asset:
            held_assets.add(asset)
        cid = p.get("conditionId", "")
        if cid:
            cur = float(p.get("curPrice", 0) or 0)
            value_by_market[cid] = value_by_market.get(cid, 0.0) + size * cur
            shares_by_market[cid] = shares_by_market.get(cid, 0.0) + size
    return held_assets, value_by_market, shares_by_market


def positions_by_condition(positions: list[dict]) -> dict[str, list[dict]]:
    """Group confirmed positive positions without rounding fractional shares."""
    out: dict[str, list[dict]] = {}
    for pos in positions or []:
        try:
            if float(pos.get("size", 0) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        cid = str(pos.get("conditionId", "") or "")
        if cid:
            out.setdefault(cid, []).append(pos)
    return out
