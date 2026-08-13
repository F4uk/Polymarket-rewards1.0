#!/usr/bin/env bash
# deploy/install.sh — 在一台干净的 Debian/Ubuntu VPS 上部署本程序。
# 用法(需要 root):
#   bash install.sh your-domain.com
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
	echo "用法: bash install.sh <域名>" >&2
	exit 1
fi

REPO="https://github.com/VMInsideVM/poly-marketmaker.git"
BASE=/opt/pmm
APP="$BASE/poly-marketmaker"

echo "==> 设置时区为 Asia/Shanghai"
# 每日盈亏台账、周报、监控 watermark 都按本地时间算,留在 UTC 会导致日期错位。
# 部分 LXC/OpenVZ 或无 dbus 的精简镜像上 timedatectl 会失败,回退到手动软链接。
timedatectl set-timezone Asia/Shanghai 2>/dev/null || ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

echo "==> 安装系统依赖"
apt-get update
apt-get install -y python3 python3-venv python3-pip python3-dev git curl ufw sudo gnupg \
	build-essential debian-keyring debian-archive-keyring apt-transport-https

echo "==> 创建服务用户 pmm"
id -u pmm >/dev/null 2>&1 || useradd --system --create-home --home-dir "$BASE" --shell /usr/sbin/nologin pmm
mkdir -p "$BASE"
chown -R pmm:pmm "$BASE"

echo "==> 克隆代码"
# 使用 git clone 保留可维护的部署工作树；后续更新由管理员显式进行。
if [ ! -d "$APP/.git" ]; then
	sudo -H -u pmm git clone "$REPO" "$APP"
fi

echo "==> 建虚拟环境、装 Python 依赖"
sudo -H -u pmm python3 -m venv "$BASE/venv"
sudo -H -u pmm "$BASE/venv/bin/pip" install --upgrade pip
sudo -H -u pmm "$BASE/venv/bin/pip" install -r "$APP/requirements.txt"

echo "==> 安装 Caddy"
if ! command -v caddy >/dev/null 2>&1; then
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' |
		gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' |
		tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
	apt-get update
	apt-get install -y caddy
fi

echo "==> 配置 Caddy(域名: $DOMAIN)"
sed "s/YOUR_DOMAIN/$DOMAIN/" "$APP/deploy/Caddyfile" >/etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo "==> 安装 systemd 服务"
cp "$APP/deploy/pmm.service" /etc/systemd/system/pmm.service
systemctl daemon-reload
systemctl enable --now pmm
systemctl restart pmm  # enable --now 在服务已运行时不会重启,重跑脚本时确保新代码/配置生效

echo "==> 配置防火墙"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo
echo "完成。打开 https://$DOMAIN 首次设置密码。"
echo "查看服务日志: journalctl -u pmm -f"
