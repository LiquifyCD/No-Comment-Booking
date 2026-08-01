#!/bin/bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if [[ -z ${PUBLIC_HOST:-} ]]; then
  echo "Set PUBLIC_HOST to Frostbyte's public IPv4 address or DNS name." >&2
  exit 1
fi

APP_USERNAME=${APP_USERNAME:-liquify}
SOURCE_REPOSITORY=${SOURCE_REPOSITORY:-https://github.com/LiquifyCD/No-Comment-Booking.git}
SOURCE_REF=${SOURCE_REF:-main}
SOURCE_DIR=/opt/no-comment-booking/source
VENV_DIR=/opt/no-comment-booking/venv

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y full-upgrade
apt-get install -y --no-install-recommends \
  ca-certificates openssl python3 python3-venv python3-pip git caddy curl sqlite3 ufw \
  fail2ban unattended-upgrades
apt-get autoremove -y
apt-get clean

dpkg-reconfigure -f noninteractive unattended-upgrades

install -m 0644 "$SOURCE_DIR/deploy/oracle/99-frostbyte-hardening.conf" \
  /etc/ssh/sshd_config.d/99-frostbyte-hardening.conf
sshd -t
systemctl reload ssh

for unit in \
  apport.service \
  ModemManager.service \
  open-vm-tools.service \
  vgauth.service \
  rpcbind.service \
  rpcbind.socket \
  udisks2.service
do
  systemctl disable --now "$unit" 2>/dev/null || true
done

if ! id nocomment >/dev/null 2>&1; then
  useradd --system --home /var/lib/no-comment-booking --shell /usr/sbin/nologin nocomment
fi
install -d -o nocomment -g nocomment -m 0700 /var/lib/no-comment-booking/data
install -d -o root -g root -m 0755 /opt/no-comment-booking

if [[ -d "$SOURCE_DIR/.git" ]]; then
  git -C "$SOURCE_DIR" fetch --prune --depth 1 origin "$SOURCE_REF"
  git -C "$SOURCE_DIR" checkout --detach FETCH_HEAD
else
  git clone --branch "$SOURCE_REF" --depth 1 "$SOURCE_REPOSITORY" "$SOURCE_DIR"
fi
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install --no-cache-dir "$SOURCE_DIR"

app_password=$("$VENV_DIR/bin/python" -c 'import secrets; print(secrets.token_urlsafe(18))')
export APP_PASSWORD="$app_password"
password_hash=$(cd "$SOURCE_DIR" && "$VENV_DIR/bin/python" - <<'PY'
import os
from provtidsbevakaren.auth import hash_password
print(hash_password(os.environ["APP_PASSWORD"]))
PY
)
app_secret=$("$VENV_DIR/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')
data_key=$("$VENV_DIR/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
users_json=$(APP_USERNAME="$APP_USERNAME" PASSWORD_HASH="$password_hash" "$VENV_DIR/bin/python" - <<'PY'
import json, os
print(json.dumps({os.environ["APP_USERNAME"]: os.environ["PASSWORD_HASH"]}, separators=(",", ":")))
PY
)
unset APP_PASSWORD password_hash

install -m 0600 /dev/null /etc/no-comment-booking.env
cat > /etc/no-comment-booking.env <<EOF
APP_MODE=server
ENABLE_SERVER_MODE=true
APP_HOST=127.0.0.1
APP_PORT=8080
PUBLIC_ORIGIN=https://$PUBLIC_HOST
ALLOWED_HOSTS=$PUBLIC_HOST,127.0.0.1,localhost
APP_SECRET_KEY=$app_secret
SERVER_USERS_JSON=$users_json
DATA_ENCRYPTION_KEY=$data_key
DATABASE_PATH=/var/lib/no-comment-booking/data/service.db
REMOTE_WEBDRIVER_URL=
REMOTE_BROWSER_VIEW_URL=
EOF
chown root:nocomment /etc/no-comment-booking.env
chmod 0640 /etc/no-comment-booking.env

install -m 0600 /dev/null /root/frostbyte-app-login.txt
cat > /root/frostbyte-app-login.txt <<EOF
Username: $APP_USERNAME
Password: $app_password
EOF
unset app_password app_secret data_key users_json

install -m 0644 "$SOURCE_DIR/deploy/oracle/no-comment-booking.service" /etc/systemd/system/no-comment-booking.service
install -m 0755 "$SOURCE_DIR/deploy/oracle/backup.sh" /usr/local/sbin/no-comment-booking-backup
install -m 0755 "$SOURCE_DIR/deploy/oracle/smoke_test.py" /usr/local/sbin/no-comment-booking-smoke-test
install -m 0644 "$SOURCE_DIR/deploy/oracle/no-comment-booking-backup.service" /etc/systemd/system/no-comment-booking-backup.service
install -m 0644 "$SOURCE_DIR/deploy/oracle/no-comment-booking-backup.timer" /etc/systemd/system/no-comment-booking-backup.timer

sed "s/PUBLIC_HOST/$PUBLIC_HOST/g" "$SOURCE_DIR/deploy/oracle/Caddyfile.template" > /etc/caddy/Caddyfile
install -d -o caddy -g caddy -m 0750 /var/log/caddy
caddy validate --config /etc/caddy/Caddyfile

install -d -m 0755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/no-comment-booking.conf <<'EOF'
[Journal]
SystemMaxUse=100M
RuntimeMaxUse=40M
MaxRetentionSec=14day
EOF

if [[ ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile
  chmod 0600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
cat > /etc/sysctl.d/60-frostbyte-memory.conf <<'EOF'
vm.swappiness=20
vm.vfs_cache_pressure=80
EOF
sysctl --system >/dev/null

cat > /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
EOF

ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable --now fail2ban
systemctl enable --now no-comment-booking.service
systemctl enable --now caddy
systemctl enable --now no-comment-booking-backup.timer

for _ in $(seq 1 30); do
  if curl --fail --silent --show-error http://127.0.0.1:8080/api/health >/dev/null; then
    exit 0
  fi
  sleep 1
done
journalctl -u no-comment-booking.service -n 100 --no-pager >&2
exit 1
