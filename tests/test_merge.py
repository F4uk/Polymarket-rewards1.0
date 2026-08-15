"""tests/test_merge.py — Merge 交易构造(标准/负风险)、回收与盈亏纯计算。"""

from engine.merge import (
    CTF,
    CTF_COLLATERAL_ADAPTER,
    NEG_RISK_COLLATERAL_ADAPTER,
    PUSD,
    build_adapter_approval_transaction,
    build_merge_transaction,
    is_approved_for_all_calldata,
    merge_adapter_address,
    merge_realized_pnl,
    merge_recovery_usd,
    parse_is_approved_result,
)

CID = "0x" + "ab" * 32


def test_standard_merge_uses_ctf_collateral_adapter():
    tx = build_merge_transaction(CID, 60, False)
    assert tx["to"] == CTF_COLLATERAL_ADAPTER
    assert tx["data"].startswith("0x9e7212ad")  # mergePositions(address,bytes32,bytes32,uint256[],uint256)
    # calldata 应包含 pUSD、conditionId、分区 [1,2] 与 60e6 金额
    assert PUSD[2:].lower() in tx["data"].lower()
    assert CID[2:].lower() in tx["data"].lower()
    assert "0000000000000000000000000000000000000000000000000000000000000002" in tx["data"]
    assert "0000000000000000000000000000000000000000000000000000000003938700" in tx["data"]  # 60 * 1e6


def test_neg_risk_merge_uses_neg_risk_collateral_adapter():
    tx = build_merge_transaction(CID, 60, True)
    assert tx["to"] == NEG_RISK_COLLATERAL_ADAPTER
    assert tx["data"].startswith("0x9e7212ad")


def test_merge_adapter_address_selection():
    assert merge_adapter_address(False) == CTF_COLLATERAL_ADAPTER
    assert merge_adapter_address(True) == NEG_RISK_COLLATERAL_ADAPTER


def test_approval_transaction_targets_ctf():
    tx = build_adapter_approval_transaction(False)
    assert tx["to"] == CTF
    assert tx["data"].startswith("0xa22cb465")  # setApprovalForAll(address,bool)
    assert NEG_RISK_COLLATERAL_ADAPTER not in tx["data"].lower()
    assert CTF_COLLATERAL_ADAPTER[2:].lower() in tx["data"].lower()
    # bool true 编码
    assert tx["data"].endswith("01")


def test_approval_transaction_neg_risk_adapter():
    tx = build_adapter_approval_transaction(True)
    assert NEG_RISK_COLLATERAL_ADAPTER[2:].lower() in tx["data"].lower()


def test_is_approved_for_all_calldata_selector_and_operand():
    cd = is_approved_for_all_calldata("0x" + "cd" * 20, False)
    assert cd.startswith("0xe985e9c5")
    assert CTF_COLLATERAL_ADAPTER[2:].lower() in cd.lower()


def test_parse_is_approved_result():
    assert parse_is_approved_result("0x" + "00" * 31 + "01") is True
    assert parse_is_approved_result("0x" + "00" * 32) is False
    assert parse_is_approved_result(None) is False
    assert parse_is_approved_result("") is False
    assert parse_is_approved_result("0x") is False


def test_recovery_and_pnl_math():
    assert merge_recovery_usd(60) == 60.0
    assert merge_recovery_usd(0) == 0.0
    # 回收 - 消耗的 YES 成本 - 消耗的 NO 成本 - 费用
    assert merge_realized_pnl(60, 33.0, 27.0, 0.0) == 0.0
    assert merge_realized_pnl(60, 30.0, 24.0, 0.5) == 5.5


def test_merge_transaction_rejects_bad_amount():
    import pytest

    with pytest.raises(ValueError):
        build_merge_transaction(CID, 0, False)
