"""version.py — 应用版本号的唯一来源。

发版只改这里;build_installer.ps1 读取它注入 Inno Setup,release.ps1 用它打 git tag。
"""

__version__ = "9.1.0"
