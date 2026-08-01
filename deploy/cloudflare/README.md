# Cloudflare Worker proxy

The application remains on Frostbyte. The Worker provides a free same-origin
frontend and API URL at:

`https://no-comment-booking.liquifycd.workers.dev`

Frostbyte uses `https://frostbyte.158-179-207-206.sslip.io` only as the
publicly trusted HTTPS origin. API and authentication responses retain
`Cache-Control: no-store` through the Worker. Caddy allows application traffic
only from Cloudflare's published IPv4 and IPv6 ranges, so direct origin access
returns HTTP 403.

## Deploy the Worker

```bash
cd deploy/cloudflare
npx wrangler@latest deploy --dry-run
npx wrangler@latest deploy
curl -fsS https://no-comment-booking.liquifycd.workers.dev/api/health
```

## Frostbyte configuration

Install `Caddyfile.frostbyte` as `/etc/caddy/Caddyfile`, then set only these
non-secret values in `/etc/no-comment-booking.env`:

```dotenv
PUBLIC_ORIGIN=https://no-comment-booking.liquifycd.workers.dev
ALLOWED_HOSTS=frostbyte.158-179-207-206.sslip.io,no-comment-booking.liquifycd.workers.dev,127.0.0.1,localhost
```

Validate before reloading Caddy and restart the application after changing its
environment:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl restart no-comment-booking
```

## Worker rollback

```bash
cd deploy/cloudflare
npx wrangler@latest versions list
npx wrangler@latest rollback
```

The pre-migration Frostbyte configuration is stored under
`/var/backups/no-comment-booking/cloudflare-migration-20260801T1925Z`.
