#!/usr/bin/env bash
# One-time server setup (Ubuntu 22.04 or 24.04). Run from the project folder on the server:
#   bash deploy/setup.sh
# Installs Python + the signing library, runs the tests, creates secrets/kalshi.env and the
# systemd service. It does not start the service and never places an order.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

echo "== system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv

echo "== clock sync (the rules skip trading if this machine's clock is off from Kalshi's)"
sudo timedatectl set-ntp true || true
timedatectl | grep -iE "synchronized|NTP service" || true

echo "== python environment"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet "cryptography>=42"

echo "== tests (no network, no orders)"
.venv/bin/python -m unittest discover -s tests

echo "== secrets folder"
mkdir -p secrets
chmod 700 secrets
if [ ! -f secrets/kalshi.env ]; then
  sed -e "s|__APP_DIR__|$APP_DIR|g" deploy/kalshi.env.example > secrets/kalshi.env
  chmod 600 secrets/kalshi.env
fi
chmod +x deploy/kb

echo "== systemd services (installed, not started)"
for unit in kalshi-live kalshi-dash; do
  sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$(id -un)|g" "deploy/$unit.service" \
    | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
sudo systemctl daemon-reload

cat <<EOF

Setup done. Next (deploy/SERVER.md, step 5 on):
  1. put your private key in $APP_DIR/secrets/kalshi.key  and run: chmod 600 secrets/kalshi.key
  2. edit secrets/kalshi.env (key ID; starts on the demo exchange with --dry-run)
  3. deploy/kb smoke
EOF
