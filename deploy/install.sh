#!/bin/bash
# 在 Ubuntu 22.04 上安装衣姿的 HTTP 服务。
# 只写入 /etc/systemd/system/posewear.service 并启动本服务。
# 不安装软件包，不占用、不查看、不重启 443 上的现有程序。
set -euo pipefail

SRC=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
PORT="${POSEWEAR_PORT:-80}"
ORIGIN="${POSEWEAR_PUBLIC_ORIGIN:-https://176.122.145.34:8443}"
PREFIX="${POSEWEAR_PREFIX:-/opt/posewear}"

if [[ "$PORT" == "443" || "$PORT" == "41080" ]]; then
  echo "拒绝：不能把本服务绑到 ${PORT}。该端口已有其他程序，本脚本不会修改它。" >&2
  exit 1
fi
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "POSEWEAR_PORT 无效：$PORT" >&2
  exit 1
fi
case "$ORIGIN" in
  http://*|https://*) ;;
  *) echo "POSEWEAR_PUBLIC_ORIGIN 必须以 http:// 或 https:// 开头。" >&2; exit 1 ;;
esac
if [[ "$ORIGIN" == *":443"* || "$ORIGIN" == "https://176.122.145.34" ]]; then
  echo "拒绝：公开地址不能使用 443。账号用 https://176.122.145.34:8443 。" >&2
  exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请用 root 运行：sudo bash install.sh" >&2
  exit 1
fi
if ! /usr/bin/python3 -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)'; then
  echo "需要 Python 3.10 或更高，且路径为 /usr/bin/python3。" >&2
  exit 1
fi
if [[ ! -f "$SRC/serve.py" || ! -f "$SRC/dist/index.html" ]]; then
  echo "在 $SRC 下没有找到 serve.py 和 dist/index.html。请先解压发布包。" >&2
  exit 1
fi

if [[ "$(readlink -f "$SRC")" != "$(readlink -f "$PREFIX")" ]]; then
  mkdir -p "$PREFIX"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$SRC"/ "$PREFIX"/
  else
    cp -a "$SRC"/. "$PREFIX"/
  fi
else
  mkdir -p "$PREFIX"
fi
if ! id posewear >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin posewear
fi
chown -R posewear:posewear "$PREFIX"
chmod -R u=rwX,go=rX "$PREFIX"
mkdir -p /var/lib/posewear/tls /var/lib/posewear/acme-webroot
chown -R posewear:posewear /var/lib/posewear
chmod 700 /var/lib/posewear

sed -e "s|__PREFIX__|$PREFIX|g" -e "s|__PORT__|$PORT|g" -e "s|__ORIGIN__|$ORIGIN|g" \
  "$PREFIX/posewear.service.in" > /etc/systemd/system/posewear.service
systemctl daemon-reload
systemctl enable posewear
systemctl restart posewear

echo "已启动 posewear。图库在端口 ${PORT}，账号地址是 ${ORIGIN}。"
echo "没有证书时 8443 不会监听，登录接口会拒绝明文。443 和 41080 未改动。"
echo "查看日志：journalctl -u posewear -n 50 --no-pager"
