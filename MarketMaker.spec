# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Polymarket market maker.
# Build:  python -m PyInstaller MarketMaker.spec --noconfirm
# Output: dist/MarketMaker/  (onedir — wrapped by the Inno Setup installer)

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Flask needs its templates/static at runtime; bundle them under web/ so the
# paths computed from sys._MEIPASS in web/routes.py resolve correctly.
datas = [
    ("web/templates", "web/templates"),
    ("web/static", "web/static"),
]

# These packages can import sub-modules dynamically; pull them in wholesale so
# nothing is missed by PyInstaller's static analysis.
#
# coincurve is the one that bites: eth_keys picks its ECC backend at runtime via
# `import coincurve` inside is_coincurve_available(), and coincurve's actual crypto
# lives in two cffi-generated extensions (_libsecp256k1, _cffi_backend) that static
# analysis does not follow. Half-collected, the package lands in an inconsistent
# state where is_coincurve_available() answers True once (so CoinCurveECCBackend is
# selected) and False on the very next call inside its __init__ — which is exactly
# the "requires the coincurve library which is not available for import" error that
# broke API construction 644 times in the 2026-07-29 logs. collect_dynamic_libs is
# no help here: the binaries are .pyd, so they ride along as sub-modules instead.
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

# SOCKS5 proxy support is a lazy import in httpx / requests (only reached when the
# proxy string is socks5), so static analysis never sees it — leave it out and any
# wallet configured with a SOCKS5 proxy raises ImportError the moment it goes online.
# Keep in sync with MarketMaker-mac.spec, which has carried this since the mac build.
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
    name="MarketMaker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # keep the console window: shows logs and gives an obvious "close to quit"
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
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
    name="MarketMaker",
)
