# Oracle Frostbyte deployment

This deployment targets Ubuntu 24.04 on `VM.Standard.E2.1.Micro`. The FastAPI app runs directly in a Python virtual environment under systemd. Caddy is the only public application service. SQLite data is stored outside the source checkout and backed up daily.

The optional Selenium browser fallback is disabled because a secure isolated browser and viewer do not fit reliably in the micro instance's 1 GB RAM. Integrated Mobile BankID remains available.

## Install

After the OCI instance and its Frostbyte-only NSG allow TCP 22, 80, and 443:

```bash
sudo PUBLIC_HOST=<public-ip-or-domain> APP_USERNAME=liquify \
  bash /opt/no-comment-booking/source/deploy/oracle/install.sh
```

The installer generates all application secrets locally on Frostbyte. Retrieve the initial application login through SSH with `sudo cat /root/frostbyte-app-login.txt`, then delete that file after saving the credential in a password manager.

When `PUBLIC_HOST` is an IP address, Caddy uses its internal CA. Public health checks therefore require `curl -k`. Replace the site address with a Cloudflare-managed hostname and remove `tls internal` after DNS points to Frostbyte; Caddy will then obtain a trusted certificate automatically.

## Verify

```bash
sudo systemctl status no-comment-booking caddy fail2ban --no-pager
curl -fsS http://127.0.0.1:8080/api/health
curl -kfsS https://<public-ip>/api/health
sudo systemctl restart no-comment-booking
sudo reboot
```

After reboot, repeat both health checks and inspect `systemctl is-active no-comment-booking caddy fail2ban`.

## Redeploy

```bash
sudo git -C /opt/no-comment-booking/source fetch --prune origin
sudo git -C /opt/no-comment-booking/source checkout --detach origin/main
sudo /opt/no-comment-booking/venv/bin/python -m pip install --no-cache-dir --force-reinstall /opt/no-comment-booking/source
sudo systemctl restart no-comment-booking
curl -fsS http://127.0.0.1:8080/api/health
```

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
