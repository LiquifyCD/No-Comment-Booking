# Security

## Sensitive data

- Never commit `.env`, configuration files, databases, cookies, identity numbers, or Discord webhook URLs.
- Local configuration, Trafikverket cookies, catalog data, and BankID challenge fields stay in memory.
- BankID reference IDs, QR secrets, and autostart tokens are never returned in JSON or logged.
- The QR is rendered server-side. Same-device BankID opening uses a backend redirect.
- Server mode encrypts configuration before writing SQLite; Trafikverket cookies are never serialized.

## Web controls

The application uses signed HttpOnly/SameSite cookies, CSRF validation on mutations, Trusted Host validation, a strict Content Security Policy, `no-store`, and generic authentication errors. Server mode additionally requires Secure cookies and HSTS. Passwords use salted PBKDF2-SHA256, with failed-login rate limiting by client address.

## Server deployment requirements

- Terminate TLS at a trusted reverse proxy and expose only the application port privately.
- Restrict Remote WebDriver to the application network; never publish it directly.
- Protect viewer/noVNC with the same user identity or a short-lived signed URL.
- Use a separate browser container/profile per user and destroy it after use.
- Keep encryption keys separate from database backups and limit log retention and container resources.

## Failure behavior

- Authentication start, reservation creation, and invoice payment are not automatically retried.
- A completed BankID status is independently checked before the session is trusted.
- Failed or incomplete API payloads are rejected instead of silently producing empty choices.
- A failed Pay later attempt keeps the pending reservation available for a controlled retry.
- Shutdown cancels active work, closes browser resources, clears cookies, and removes challenge secrets.

## Emergency shutdown

Set `ENABLE_SERVER_MODE=false`, stop the container, rotate `APP_SECRET_KEY`, revoke viewer sessions, and delete active browser containers. Rotate `DATA_ENCRYPTION_KEY` only through a controlled decrypt-and-re-encrypt procedure; replacing it directly makes stored data unreadable.
