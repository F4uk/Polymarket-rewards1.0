"""diagnose_deposit_wallet.py — 查询 deposit wallet；部署必须显式确认。

使用当前安装的官方 SDK `get_expected_deposit_wallet()` / `deploy_deposit_wallet()`。

私钥从加密数据库解密获取（不写死、不走 env）。
builder creds 从环境变量读取（deploy 才需要）：
  BUILDER_API_KEY / BUILDER_SECRET / BUILDER_PASS_PHRASE
relayer / chain 默认走生产环境，可用 RELAYER_URL / CHAIN_ID 覆盖。
"""

import argparse
import hashlib
import os

from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH

from py_builder_relayer_client.client import RelayClient
from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

RELAYER_URL = os.environ.get("RELAYER_URL", "https://relayer-v2.polymarket.com/")
CHAIN_ID = int(os.environ.get("CHAIN_ID", "137"))


def load_private_key() -> str:
    db = Database(DB_PATH)
    db.init()
    pw_hash, salt = db.get_password()
    if pw_hash is None:
        raise SystemExit("未设置密码")
    password = input("请输入访问密码: ")
    key = derive_key(password, salt)
    if hashlib.sha256(key).hexdigest() != pw_hash:
        raise SystemExit("密码错误")
    wallets = db.list_wallets()
    if not wallets:
        raise SystemExit("没有钱包")
    pk = decrypt(wallets[0]["encrypted_key"], key)
    db.close()
    return pk if pk.startswith("0x") else "0x" + pk


def build_builder_config():
    """有 builder env 就构造，没有就返回 None（deploy 才需要）。"""
    k = os.environ.get("BUILDER_API_KEY")
    s = os.environ.get("BUILDER_SECRET")
    p = os.environ.get("BUILDER_PASS_PHRASE")
    if k and s and p:
        return BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(key=k, secret=s, passphrase=p)
        )
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读诊断 Deposit Wallet；可显式选择部署")
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="在确认地址后部署尚未部署的 Deposit Wallet（会产生链上操作）",
    )
    args = parser.parse_args(argv)

    print("=" * 80)
    print("Deposit Wallet — 参考代码流程")
    print("=" * 80)

    private_key = load_private_key()

    builder_config = build_builder_config()
    print(f"\nRELAYER_URL = {RELAYER_URL}")
    print(f"CHAIN_ID    = {CHAIN_ID}")
    print(
        f"builder_config = {'已配置' if builder_config else 'None（未设置 builder env）'}"
    )

    relayer = RelayClient(
        RELAYER_URL,
        CHAIN_ID,
        private_key,
        builder_config,
    )

    deposit_wallet = relayer.get_expected_deposit_wallet()
    print(f"\n[get_expected_deposit_wallet] -> {deposit_wallet}")

    already = relayer.get_deployed(deposit_wallet)
    print(f"[get_deployed]                -> {already}")

    if already:
        print(
            "\n该 deposit wallet 已在链上部署，无需部署。"
        )
    elif not args.deploy:
        print("\n只读模式：未执行部署。若确需部署，请重新运行并显式传入 --deploy。")
    else:
        if builder_config is None:
            raise SystemExit("部署需要完整 builder credentials；未执行部署")
        confirmation = input(
            "部署会产生链上操作。请输入上面显示的完整 deposit wallet 地址以确认: "
        ).strip()
        if confirmation.lower() != deposit_wallet.lower():
            raise SystemExit("确认地址不匹配；未执行部署")
        # 唯一资金变更入口：显式 --deploy + 完整地址确认。
        try:
            response = relayer.deploy_deposit_wallet()
            print(f"\n[deploy_deposit_wallet] transaction_id = {response.transaction_id}")
            print(f"[deploy_deposit_wallet] transaction_hash = {response.transaction_hash}")
            print("等待链上确认 (response.wait()) ...")
            confirmed = response.wait()
            print(f"[response.wait()] -> {confirmed}")
        except Exception as e:
            print(f"\n[deploy_deposit_wallet] 调用结果: {type(e).__name__}: {e}")

    print("\n" + "=" * 80)
    print(f"你的 deposit wallet 地址: {deposit_wallet}")
    print(f"是否已部署: {already}")
    print("=" * 80)


if __name__ == "__main__":
    main()
