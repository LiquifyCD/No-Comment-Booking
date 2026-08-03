# Architecture

## Shared flow

```text
Responsive web UI
       | localhost/HTTPS API
       v
FastAPI web layer -- AuthManager
       |
       v
RuntimeRegistry -- one MonitorJob per user
       |
       +-- BankIdFlow (memory-only challenge and rotating QR)
       +-- BookingCatalog (licences, examination types, locations)
       +-- TrafikverketClient (requests, memory-only cookies)
       +-- BrowserLoginSession (explicit fallback only)
       +-- Discord notifier
       `-- StateStore
             +-- VolatileStateStore (local)
             `-- EncryptedSqliteStateStore (server)
```

`engine.py` contains domain rules and Trafikverket calls. `bankid.py` owns the integrated authentication state machine and exposes only sanitized state. `catalog.py` normalizes, translates, sorts, and deduplicates API catalog data. `runtime.py` owns lifecycle, concurrency guards, bounded events, and cleanup. `web.py` handles HTTP, authentication, validation, permissions, and static assets.

## Authentication and catalog sequence

1. The backend starts BankID once; this mutation has retries disabled.
2. The QR code rotates from status responses every two seconds.
3. The frontend receives only a backend-rendered SVG and sanitized status.
4. Completion is accepted only after a separate authorization check succeeds.
5. The backend captures an allowlisted identity field from the authenticated API response without returning it to the browser.
6. The backend loads Swedish language resources and `licence-information`; the UI selects the saved or first available licence.
7. `search-information` then loads examination types, vehicle/rental choices, cities, and locations. The UI permits at most four unique locations, and the backend repeats that validation.

The same in-memory `requests.Session` is reused for authentication, catalog lookup, and monitoring. No Trafikverket cookie or BankID secret is persisted.

## Local and server modes

Local mode exchanges a random launch token for a signed HttpOnly cookie, binds Uvicorn to `127.0.0.1`, and uses volatile storage. Shutdown clears the session, catalog, pending challenge, configuration, and cookies.

Server mode uses email and password login, Secure/HttpOnly/SameSite cookies, CSRF protection, encrypted SQLite configuration, and an isolated runtime per account ID. Existing username databases migrate atomically only when a complete explicit username-to-email mapping is configured. It refuses to start without explicit activation, HTTPS origin, allowed hosts, credentials, and separate cryptographic keys. Browser fallback is exposed only when both a Remote WebDriver and a protected viewer URL are configured.

## State and concurrency

A job moves through `idle`, `starting`, `authentication`, `authenticated`, `running`, `stopping`, and `error`. Locks reject overlapping authentication, catalog refresh, and monitor start. Catalog access is disabled during monitoring so the shared Trafikverket session cannot issue competing stateful calls.

Events use monotonic IDs in a 100-entry bounded buffer. Admins use `/api/live`; regular users use `/api/status`, which omits detailed events. Both maintain one same-origin SSE connection and perform only a bounded recovery snapshot if streaming fails. Date minimums are recalculated in the configured IANA timezone for every backend validation and slot filter. The server forces a 15-second polling interval and disables all automatic reservation/booking flags regardless of client input.

Python remains the backend runtime because measured idle use on Frostbyte is about 65 MB RSS and the mature BankID/Trafikverket behavior is already covered by tests. A Go rewrite was rejected for this release because it would duplicate identity-sensitive behavior without a demonstrated memory bottleneck or guaranteed parity.
