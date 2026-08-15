# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — macOS build (Apple 芯片 / arm64).
# Build:  python -m PyInstaller MarketMaker-mac.spec --noconfirm
# Output: dist/PolymarketMarketMaker.app  (CI 用 hdiutil 封装成 .dmg)
#
# 与 Windows 的 MarketMaker.spec 共用同一套 datas / hiddenimports；
# 区别：console=False + BUNDLE 出 .app，target_arch="arm64"。

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

import version

# Flask 运行时需要模板/静态资源；打进 web/ 下，使 web/routes.py 依 sys._MEIPASS
# 计算出的路径能解析到。
datas = [
    ("web/templates", "web/templates"),
    ("web/static", "web/static"),
]

# 这些包会动态 import 子模块，整包收进来，避免被静态分析漏掉。
# coincurve 尤其要收：eth_keys 在运行时靠 is_coincurve_available() 里的 `import coincurve`
# 选 ECC 后端，而 coincurve 的密码学实现在两个 cffi 生成的扩展里（_libsecp256k1、
# _cffi_backend），静态分析跟不到。只收一半会让它处于不一致状态——同一个
# is_coincurve_available() 前一次答 True（于是选了 CoinCurveECCBackend）、后一次在
# __init__ 里答 False，正是 2026-07-29 日志里 644 次建 API 失败的那条报错。
hiddenimports = []
for pkg in (
    "py_clob_client_v2",
    "py_builder_relayer_client",
    "py_builder_signing_sdk",
    "eth_account",
    "poly_eip712_structs",
    "coincurve",
):
    hiddenimports += collect_submodules(pkg)
    datas += collect_data_files(pkg)

# SOCKS5 代理支持是 httpx / requests 的惰性 import（只在代理串是 socks5 时才走到），
# 静态分析看不到 —— 漏掉的话配了 SOCKS5 代理的钱包一联网就 ImportError。
hiddenimports += ["socksio", "socks", "urllib3.contrib.socks"]


a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tkinter", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PolymarketMarketMaker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # .app 无控制台窗口；日志写入日志文件
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PolymarketMarketMaker",
)

app = BUNDLE(
    coll,
    name="PolymarketMarketMaker.app",
    icon=None,
    bundle_identifier="com.vminsidevm.polymarketmarketmaker",
    version=version.__version__,
    info_plist={
        # 磁盘上文件名为 PolymarketMarketMaker.app；Finder 显示中文名。
        "CFBundleName": "Polymarket 做市助手",
        "CFBundleDisplayName": "Polymarket 做市助手",
        "CFBundleShortVersionString": version.__version__,
        "CFBundleVersion": version.__version__,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
