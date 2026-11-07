#!/usr/bin/env bash
# One-shot (and safe to re-run) setup of Ava on a fresh Ubuntu 22.04/24.04 server.
#
# Usage (as root or with sudo):  sudo bash setup.sh <domain> <path-to-app.tar.gz> <path-to-ava.env>
# Works on plain Ubuntu servers and on Oracle Cloud (which has its own iptables firewall).
#   domain   e.g. ava.example.com, or <ip-with-dashes>.sslip.io if you have no domain
#
# Result: four systemd services that start on boot and restart on crash, Caddy serving
# the call page over HTTPS, and a firewall that only allows SSH, HTTP and HTTPS.
set -euo pipefail

DOMAIN="${1:?domain required}"
TARBALL="${2:?app tarball required}"
ENVFILE="${3:?env file required}"
APP_DIR=/opt/ava/app
SERVICES="ava-mock-api ava-mock-claude ava-agent ava-web"

echo "==> System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg build-essential \
  $(command -v netfilter-persistent >/dev/null || echo ufw) \
  debian-keyring debian-archive-keyring apt-transport-https >/dev/null

if ! command -v caddy >/dev/null; then
  echo "==> Caddy (HTTPS reverse proxy, automatic certificates)"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

if ! command -v uv >/dev/null; then
  echo "==> uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
fi

echo "==> Service user 'ava'"
id ava >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/ava --shell /usr/sbin/nologin ava

echo "==> App code"
systemctl stop $SERVICES 2>/dev/null || true
mkdir -p "$APP_DIR"
# keep the existing virtual environment so updates are fast
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
tar -xzf "$TARBALL" -C "$APP_DIR"
# files edited on Windows may have CRLF line endings, which break systemd units
sed -i 's/\r$//' "$APP_DIR"/deploy/*.service
chown -R ava:ava /opt/ava

echo "==> Python dependencies (first run takes a few minutes)"
sudo -u ava -H bash -c "cd $APP_DIR && UV_PYTHON_INSTALL_DIR=/opt/ava/.python uv sync --locked --no-dev --quiet"
sudo -u ava -H bash -c "cd $APP_DIR && .venv/bin/python -m livekit.agents download-files" >/dev/null

echo "==> Settings (/etc/ava/ava.env, readable only by root and ava)"
install -d -m 750 -o root -g ava /etc/ava
install -m 640 -o root -g ava "$ENVFILE" /etc/ava/ava.env

echo "==> systemd services"
install -m 644 "$APP_DIR"/deploy/ava-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now $SERVICES

echo "==> HTTPS for $DOMAIN"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:8090
}
EOF
systemctl enable caddy >/dev/null
systemctl reload caddy || systemctl restart caddy

echo "==> Firewall"
if [ -f /etc/iptables/rules.v4 ] && command -v netfilter-persistent >/dev/null; then
  # Oracle Cloud Ubuntu images ship their own iptables rules (and ufw conflicts with them):
  # open HTTP and HTTPS at the top of the INPUT chain and save the rules for reboots.
  for port in 80 443; do
    iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null \
      || iptables -I INPUT 1 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  done
  netfilter-persistent save >/dev/null
else
  ufw allow OpenSSH >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
fi

rm -f "$TARBALL" "$ENVFILE"
sleep 5
systemctl --no-pager --lines=0 status $SERVICES caddy | grep -E '●|Active:'
echo
echo "Done. Open https://$DOMAIN"
