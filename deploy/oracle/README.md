# Oracle Frostbyte deployment

This deployment targets Ubuntu 24.04 on `VM.Standard.E2.1.Micro`. The FastAPI app runs directly in a Python virtual environment under systemd. Caddy is the only public application service. SQLite data is stored outside the source checkout and backed up daily.

The optional Selenium browser fallback is disabled because a secure isolated browser and viewer do not fit reliably in the micro instance's 1 GB RAM. Integrated Mobile BankID remains available.

## Install

Create a dedicated `Frostbyte-vcn` with `Frostbyte-public-subnet`; do not attach
the instance to another application's VCN. Allow inbound TCP 22, 80, and 443
in Frostbyte's security list, then run:

```bash
sudo PUBLIC_HOST=<public-ip-or-domain> APP_EMAIL=admin@example.com \
  bash /opt/no-comment-booking/source/deploy/oracle/install.sh
```

The installer generates all application secrets locally on Frostbyte. Retrieve the initial application login through SSH with `sudo cat /root/frostbyte-app-login.txt`, then delete that file after saving the credential in a password manager.

When `PUBLIC_HOST` is an IP address, Caddy uses its internal CA. Public health checks therefore require `curl -k`. Replace the site address with a Cloudflare-managed hostname and remove `tls internal` after DNS points to Frostbyte; Caddy will then obtain a trusted certificate automatically.

## Verify

```bash
sudo systemctl status no-comment-booking caddy fail2ban --no-pager
curl -fsS http://127.0.0.1:8080/api/health
curl -kfsS https://<public-ip>/api/health
sudo no-comment-booking-smoke-test https://<public-ip> --insecure
sudo systemctl restart no-comment-booking
sudo reboot
```

After reboot, repeat both health checks and inspect `systemctl is-active no-comment-booking caddy fail2ban`.

## Redeploy

Before the first 2.4.0 or later restart of an existing username-based installation, add a
complete mapping to `/etc/no-comment-booking.env`, for example:

```bash
ACCOUNT_EMAIL_MIGRATION_JSON={"liquify":"admin@example.com"}
```

Keep the old `SERVER_USERS_JSON` and `ADMIN_USERS` entries during this one-time
migration. The service refuses to start and leaves the database unchanged if any
existing account lacks a unique email mapping.

```bash
sudo git -C /opt/no-comment-booking/source fetch --prune --depth 1 origin main
sudo git -C /opt/no-comment-booking/source checkout --detach FETCH_HEAD
sudo /usr/local/sbin/no-comment-booking-backup
sudo /opt/no-comment-booking/venv/bin/python -m pip install --no-cache-dir --force-reinstall /opt/no-comment-booking/source
sudo systemctl restart no-comment-booking
for attempt in {1..30}; do
  curl -fsS http://127.0.0.1:8080/api/health && break
  sleep 1
done
curl -fsS http://127.0.0.1:8080/api/health
sudo no-comment-booking-smoke-test https://<public-ip> --insecure
```

Version 2.5.0 adds an automatic SQLite migration for Discord permissions and a global default. It is additive and keeps existing accounts denied by default. Configure it from the administrator user view; webhook values remain encrypted in the state store and are never returned by bootstrap.

## Operations

```bash
sudo journalctl -u no-comment-booking -n 100 --no-pager
sudo journalctl -u caddy -n 100 --no-pager
sudo caddy validate --config /etc/caddy/Caddyfile
sudo ufw status verbose
sudo fail2ban-client status sshd
sudo systemctl list-timers no-comment-booking-backup.timer
sudo /usr/local/sbin/no-comment-booking-backup
ls -lh /var/backups/no-comment-booking
```
