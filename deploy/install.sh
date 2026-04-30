#!/usr/bin/env bash
# Einmal-Setup auf einer frischen Debian-13-VPS.
# Aufruf als root:   bash deploy/install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_FILE="/etc/systemd/system/fbe-tender.service"
NGINX_FILE="/etc/nginx/sites-available/fbe"
HTPASSWD_FILE="/etc/nginx/.fbe-htpasswd"

if [[ $EUID -ne 0 ]]; then
  echo "Bitte als root ausfuehren." >&2
  exit 1
fi

echo "==> Pakete installieren"
apt update
apt install -y python3 python3-venv python3-pip python3-full \
               git nginx apache2-utils curl ufw \
               libxml2-dev libxslt1-dev build-essential

echo "==> venv anlegen"
cd "$REPO_DIR"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt

[[ -f .env ]] || cp .env.example .env

echo "==> Optional: Playwright fuer JS-Portale (DTVP etc.)"
read -r -p "Playwright + Chromium installieren (~250 MB)? [y/N] " yn || yn=N
if [[ "$yn" =~ ^[YyJj]$ ]]; then
  pip install -r requirements-playwright.txt
  python -m playwright install chromium
  python -m playwright install-deps chromium || true
fi

echo "==> systemd-Service installieren"
install -m 0644 "$REPO_DIR/deploy/fbe-tender.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now fbe-tender
sleep 1
systemctl --no-pager status fbe-tender || true

echo "==> nginx-Reverse-Proxy installieren"
install -m 0644 "$REPO_DIR/deploy/nginx-fbe.conf" "$NGINX_FILE"
ln -sf "$NGINX_FILE" /etc/nginx/sites-enabled/fbe
rm -f /etc/nginx/sites-enabled/default

if [[ ! -f "$HTPASSWD_FILE" ]]; then
  echo
  echo "==> Basic-Auth-Passwort fuer Login 'fbe' anlegen:"
  htpasswd -c "$HTPASSWD_FILE" fbe
fi

nginx -t
systemctl reload nginx

echo "==> Firewall"
ufw allow OpenSSH
ufw allow "Nginx Full"
yes | ufw enable || true

echo
echo "Fertig. Health-Check:"
curl -s http://127.0.0.1:8000/api/health || true
echo
echo "Im Browser: http://<deine-vps-ip>/   (Login: fbe + dein Passwort)"
