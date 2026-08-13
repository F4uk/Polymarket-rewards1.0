# dump_trades.py — 手动验证 get_trades 真实结构（非 pytest）。
# 用法：python dump_trades.py   （会要求输入访问密码）
#
# 目的：判定"所有持仓裸奔（成本算不出）"的根因。重点对比两种查询：
#   A) get_trades(asset_id=...)                 —— 不带 maker 过滤（_cost_lots 现在用的）
#   B) get_trades(maker_address=funder, asset)  —— 带 maker 过滤（check_buy_orders 用的）
# 若 B 能拿到我们的 maker 买入成交、而 A 拿不到，就证明 _cost_lots 漏了 maker_address。
import json
import hashlib
from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH
from py_clob_client_v2.clob_types import TradeParams
from engine.fills import extract_fills
from engine.take_profit import position_cost_with_lots


def _summarize(trades, funder, eoa, asset):
    """对一组 trades 统计：funder/eoa 在 maker_orders 里出现的次数，以及我们的 taker 笔数。"""
    f, e = funder.lower(), eoa.lower()
    funder_maker = eoa_maker = taker_ours = 0
    for tr in trades:
        makers = [
            str(mo.get("maker_address", "")).lower()
            for mo in tr.get("maker_orders", []) or []
        ]
        if f in makers:
            funder_maker += 1
        if e in makers:
            eoa_maker += 1
        if str(tr.get("trader_side", "")).upper() == "TAKER" and str(
            tr.get("asset_id", "")
        ) == str(asset):
            taker_ours += 1
    return funder_maker, eoa_maker, taker_ours


def main():
    db = Database(DB_PATH)
    db.init()
    pw_hash, salt = db.get_password()
    if pw_hash is None:
        print("未设置密码")
        return
    password = input("请输入访问密码: ")
    key = derive_key(password, salt)
    if hashlib.sha256(key).hexdigest() != pw_hash:
        print("密码错误")
        return
    w = db.list_wallets()[0]
    private_key = decrypt(w["encrypted_key"], key)

    from api.polymarket_api import PolymarketAPI

    api = PolymarketAPI(
        private_key,
        signature_type=w.get("signature_type", 2),
        funder=w.get("funder") or None,
    )
    funder = api.get_funder()
    eoa = api.get_address()
    print(f"EOA(签名地址) = {eoa}")
    print(f"funder(入金钱包) = {funder}")
    print(f"signature_type = {w.get('signature_type')}")
    print("=" * 78)

    # ---- L2 鉴权与身份探针(先确认 get_trades 空到底是鉴权坏了还是真没成交)----
    print("【鉴权探针】")
    try:
        creds = api.client.creds
        configured = bool(
            getattr(creds, "api_key", None)
            and getattr(creds, "api_secret", None)
            and getattr(creds, "api_passphrase", None)
        )
        print(f"  L2 creds 已配置 = {'是' if configured else '否'}（不输出凭据）")
    except Exception as e:
        print(f"  读取 creds 失败: {e}")
    try:
        keys = api.client.get_api_keys()  # L2 接口:能返回=鉴权是活的
        count = len(keys) if isinstance(keys, (dict, list, tuple, set)) else "未知"
        print(f"  get_api_keys OK（鉴权活）: {count} 条凭据记录（内容已隐藏）")
    except Exception as e:
        print(f"  get_api_keys 失败（L2 鉴权可能没拿到/坏了）: {e}")
    # 全局 get_trades 的几种查法:看这个账户在 CLOB 上到底有没有任何成交
    for label, params in (
        ("无参·全部成交", TradeParams()),
        ("maker=EOA(签名者)", TradeParams(maker_address=eoa)),
        ("maker=funder(Safe)", TradeParams(maker_address=funder)),
    ):
        try:
            ts = api.get_trades(params)
            print(f"  get_trades [{label}] = {len(ts)} 笔")
        except Exception as e:
            print(f"  get_trades [{label}] 报错: {e}")
    print("=" * 78)

    positions = [
        p for p in api.get_user_positions(funder) if float(p.get("size", 0) or 0) > 0
    ]
    if not positions:
        print("当前无持仓，无法验证；请在有持仓时再跑")
        return
    # ---- 深挖:服务端 asset 过滤=0,但全量有 222 笔。把全量拉下来,看这个 asset/market
    #      到底在不在里面,并用「客户端过滤」直接重建成本(extract_fills 本来就客户端过滤)----
    print("【深挖:全量成交 vs 服务端 asset 过滤】")
    all_maker = api.get_trades(TradeParams(maker_address=funder))  # 222 笔(全 asset)
    print(f"  全量(maker=funder)= {len(all_maker)} 笔")

    # 统计全量里出现过的 asset_id(我们 maker_orders 里的)与 market(condition_id)
    asset_inventory: dict = {}
    market_inventory: dict = {}
    f = funder.lower()
    for tr in all_maker:
        market_inventory[tr.get("market", "")] = (
            market_inventory.get(tr.get("market", ""), 0) + 1
        )
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() == f:
                aid = str(mo.get("asset_id", ""))
                asset_inventory[aid] = asset_inventory.get(aid, 0) + 1
    print(
        f"  全量里我们成交过的不同 asset 数 = {len(asset_inventory)}，不同 market 数 = {len(market_inventory)}"
    )

    for p in positions:
        asset = str(p["asset"])
        size = float(p.get("size", 0) or 0)
        cid = str(p.get("conditionId", ""))
        title = (p.get("title") or "")[:40]
        print(f"\n  持仓 asset={asset}")
        print(f"        market(conditionId)={cid} | size={size} | {title}")
        print(
            f"    · 该 asset 是否出现在全量成交里? {'是' if asset in asset_inventory else '否'}"
            f"（出现 {asset_inventory.get(asset, 0)} 次）"
        )
        print(
            f"    · 该 market 是否出现在全量成交里? {'是' if cid in market_inventory else '否'}"
            f"（出现 {market_inventory.get(cid, 0)} 次）"
        )

        # 客户端过滤重建成本(用全量 222 笔)
        fills = extract_fills(all_maker, funder, asset)
        cost, lots = position_cost_with_lots(fills, size)
        recon = sum(l.get("take", 0) for l in lots)
        print(
            f"    · 客户端过滤 extract_fills={len(fills)} 笔 -> cost={cost} 重建持仓={recon}（需≈{size}）"
        )

        # 再试用 market(condition_id) 做服务端过滤
        try:
            by_market = api.get_trades(TradeParams(market=cid))
            print(f"    · get_trades(market=conditionId) = {len(by_market)} 笔")
        except Exception as ex:
            print(f"    · get_trades(market=conditionId) 报错: {ex}")

    # 把全量样本里前 3 个 asset_id、以及持仓 asset 打出来对比格式
    sample = list(asset_inventory.keys())[:3]
    print("\n  全量成交里的 asset_id 样例(看格式/长度是否和持仓 asset 一致):")
    for s in sample:
        print(f"    {s}  (len={len(s)})")
    print(
        f"  持仓 asset: {positions[0]['asset']}  (len={len(str(positions[0]['asset']))})"
    )

    export = input("\n成交记录含私密交易历史。输入 EXPORT 才写入本地 trades_dump.json: ")
    if export.strip() == "EXPORT":
        with open("trades_dump.json", "w", encoding="utf-8") as fh:
            json.dump(all_maker, fh, ensure_ascii=False, indent=2)
        print("已写入本地 trades_dump.json；请勿提交或分享该文件")
    else:
        print("未写出成交记录")


if __name__ == "__main__":
    main()
