# engine/merge.py
"""Merge(整对回收)的链上交易构造与盈亏纯计算(无 IO)。

官方合约地址与函数签名均已在 Polygon 主网逐一核实(2026-08-16 eth_call):
- CTF Collateral Adapter(标准市场) 0xAdA100Db00Ca00073811820692005400218FcE1f
  mergePositions(pUSD, bytes32(0), conditionId, [1,2], amount) —— 烧 YES+NO,
  原子地把回收的 USDC.e wrap 成 pUSD 打回调用者。
- NegRiskCtfCollateralAdapter(负风险市场) 0xadA2005600Dec949baf300f4C6120000bDB6eAab
  同样的接口,内部走 NegRiskAdapter.mergePositions(conditionId, amount)。
- 授权:两个 adapter 都经 CTF.safeBatchTransferFrom 从调用者拉走 YES/NO,因此
  调用者(我们的 Safe)须先在 CTF 上 setApprovalForAll(adapter, true)。

两个市场的 calldata 同形(第一个参数 collateralToken 被 adapter 忽略但保留以兼容
CTF 接口),Merge 前的授权也走同一个 CTF 合约。不要把旧的 NegRiskAdapter 直连路径
(0xd91E80…)写死进来:它返回的是 USDC.e 而非 pUSD,与 V2 交易抵押品不一致。
"""

from eth_abi import encode
from eth_utils import to_hex

# --- Polygon 主网官方合约(已链上核实) ---
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # 条件代币框架
CTF_COLLATERAL_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"  # 标准市场
NEG_RISK_COLLATERAL_ADAPTER = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"  # 负风险市场
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # Polymarket USD(CLOB 抵押品)
USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # 桥接 USDC(CTF 底层抵押品)

# 函数选择器(keccak 前 4 字节,已本地计算核实):
#   mergePositions(address,bytes32,bytes32,uint256[],uint256) = 0x9e7212ad
#   setApprovalForAll(address,bool) = 0xa22cb465
#   isApprovedForAll(address,address) = 0xe985e9c5
MERGE_SELECTOR = "0x9e7212ad"
SET_APPROVAL_SELECTOR = "0xa22cb465"
IS_APPROVED_SELECTOR = "0xe985e9c5"

COLLATERAL_DECIMALS = 6


def _bytes32(value: str) -> bytes:
    """'0x…'(64 位 hex)或裸 hex -> bytes32;不足左补零。"""
    s = str(value or "")
    if s.startswith("0x"):
        s = s[2:]
    raw = bytes.fromhex(s)
    if len(raw) > 32:
        raise ValueError(f"bytes32 超长: {value}")
    return raw.rjust(32, b"\x00")


def merge_adapter_address(neg_risk: bool) -> str:
    """按市场类型选 Merge 适配器(官方 V2 抵押品适配器)。"""
    return NEG_RISK_COLLATERAL_ADAPTER if neg_risk else CTF_COLLATERAL_ADAPTER


def build_merge_transaction(condition_id: str, amount: float, neg_risk: bool) -> dict:
    """构造 Merge 交易的 (to, data)。amount 为整对份数(≥0),金额按 6 位小数放大。

    烧 amount 个 YES + amount 个 NO,回收 amount 美元 pUSD(每份整对 = $1)。
    标准与负风险市场同形;负风险经 NegRiskCtfCollateralAdapter 路由到 NegRiskAdapter。
    """
    amount = float(amount or 0)
    if amount <= 0:
        raise ValueError("merge amount must be > 0")
    raw = int(round(amount * (10 ** COLLATERAL_DECIMALS)))
    payload = encode(
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [PUSD, bytes(32), _bytes32(condition_id), [1, 2], raw],
    )
    return {
        "to": merge_adapter_address(bool(neg_risk)),
        "data": MERGE_SELECTOR + to_hex(payload)[2:],
    }


def build_adapter_approval_transaction(neg_risk: bool) -> dict:
    """构造 CTF setApprovalForAll(adapter, true) 交易,供 Safe 在 Merge 前批量执行。"""
    adapter = merge_adapter_address(bool(neg_risk))
    payload = encode(["address", "bool"], [adapter, True])
    return {
        "to": CTF,
        "data": SET_APPROVAL_SELECTOR + to_hex(payload)[2:],
    }


def is_approved_for_all_calldata(owner: str, neg_risk: bool) -> str:
    """eth_call isApprovedForAll(owner, adapter) 的 calldata。"""
    adapter = merge_adapter_address(bool(neg_risk))
    payload = encode(["address", "address"], [owner, adapter])
    return IS_APPROVED_SELECTOR + to_hex(payload)[2:]


def parse_is_approved_result(result: str) -> bool:
    """eth_call 返回值(0x 前缀 64 位 hex) -> bool。空/异常视为 False。"""
    try:
        s = str(result or "")
        if s.startswith("0x"):
            s = s[2:]
        raw = bytes.fromhex(s)
        return len(raw) >= 32 and raw[-1] == 0x01
    except (TypeError, ValueError):
        return False


def merge_recovery_usd(amount: float) -> float:
    """每份整对回收 $1 pUSD。"""
    return float(amount or 0)


def merge_realized_pnl(
    amount: float,
    yes_consumed_cost: float,
    no_consumed_cost: float,
    fees_usd: float = 0.0,
) -> float:
    """CONFIRMED Merge 的已实现盈亏:
    回收 − 消耗的 YES FIFO 成本 − 消耗的 NO FIFO 成本 − 未含在成交成本里的费用。"""
    return (
        merge_recovery_usd(amount)
        - float(yes_consumed_cost or 0)
        - float(no_consumed_cost or 0)
        - float(fees_usd or 0)
    )
