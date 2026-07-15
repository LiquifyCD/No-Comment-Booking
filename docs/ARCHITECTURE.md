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

`engine.py` contains domain rules and Trafikverket calls. `bankid.py` owns the integrated authentication state machine and exposes only sanitized state. `catalog.py` normalizes, translates, sorts, and deduplicates API catalog data. `runtime.py` owns lifecycle, concurrency guards, events, pending reservations, and cleanup. `web.py` handles HTTP, authentication, validation, and static assets.

## Authentication and catalog sequence

1. The backend starts BankID once; this mutation has retries disabled.
2. The QR code rotates from status responses every two seconds.
3. The frontend receives only a backend-rendered SVG and sanitized status.
4. Completion is accepted only after a separate authorization check succeeds.
5. The backend loads Swedish language resources and `licence-information`.
6. Selecting a licence calls `search-information` for examination types and locations.

The same in-memory `requests.Session` is reused for monitoring and reservation. No Trafikverket cookie or BankID secret is persisted.

## Local and server modes

Local mode exchanges a random launch token for a signed HttpOnly cookie, binds Uvicorn to `127.0.0.1`, and uses volatile storage. Shutdown clears the session, catalog, pending challenge, configuration, and cookies.

Server mode uses password login, Secure/HttpOnly/SameSite cookies, CSRF protection, encrypted SQLite configuration, and an isolated runtime per user. It refuses to start without explicit activation, HTTPS origin, allowed hosts, credentials, separate cryptographic keys, Remote WebDriver, and a viewer URL.

## State and concurrency

A job moves through `idle`, `starting`, `authentication`, `authenticated`, `running`, `action_required`, `stopping`, and `error`. Locks reject overlapping authentication, catalog refresh, monitor start, and invoice completion. Catalog access is disabled during monitoring so the shared Trafikverket session cannot issue competing stateful calls.

Events use monotonic IDs in a bounded buffer. Frontend polling is self-sequenced, preventing overlapping requests and duplicate rendering. Date minimums are refreshed in the UI and recalculated in the configured IANA timezone for every backend validation and slot filter.
