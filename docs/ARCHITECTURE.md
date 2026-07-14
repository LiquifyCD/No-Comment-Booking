# Arkitektur

## Gemensamt flöde

```text
Responsive web UI
       │ HTTPS/localhost API
       ▼
FastAPI web layer ── AuthManager
       │
       ▼
RuntimeRegistry ── one MonitorJob per user
       │
       ├── TrafikverketClient (requests, cookies only in memory)
       ├── BrowserLoginSession (local or remote Selenium)
       ├── Discord notifier
       └── StateStore
             ├── VolatileStateStore (local)
             └── EncryptedSqliteStateStore (server)
```

`engine.py` innehåller den gemensamma domän- och integrationslogiken för filter,
API-anrop, reservation och bokning. `runtime.py` äger livscykel, trådar,
race-skydd, händelsebuffert och resursstädning. `web.py` ansvarar endast för
HTTP, autentisering, validering och statiska filer.

## Local mode

`launcher.py` skapar en slumpmässig engångstoken, binder Uvicorn till
`127.0.0.1` och öppnar systemets webbläsare. Token växlas omedelbart mot en
signerad HttpOnly-cookie och tas bort ur adressfältet genom redirect.

Konfigurationen använder `VolatileStateStore`. Stopp eller avslut stänger
Selenium, rensar `requests.Session`, tömmer lagringen och stänger servern.

## Server mode

Server mode använder samma UI och monitorlogik men byter adapters:

- lösenordsinloggning med PBKDF2-SHA256 och rate limiting;
- signerad Secure/HttpOnly/SameSite-cookie samt CSRF-token;
- en isolerad `MonitorJob` per användaridentitet;
- krypterad konfiguration i SQLite med Fernet;
- Remote WebDriver för en isolerad serverwebbläsare;
- extern, autentiserad viewer för BankID och manuell bokningsslutföring.

Serverläget har fail-closed-konfiguration. Det startar inte utan explicit flagga,
HTTPS-origin, tillåtna hosts, användarhashar, två separata kryptonycklar samt
Remote WebDriver- och viewer-adresser.

## Tillstånd och samtidighet

Ett jobb rör sig mellan `idle`, `starting`, `authentication`, `running`,
`action_required`, `stopping` och `error`. Lås skyddar start/stopp och endast ett
jobb kan köras per användare. Stoppsignalen delas med inloggning och monitorloop.
Resurser stängs i `finally`, även efter nätverks- eller bokningsfel.

Händelser har monotona ID:n i en begränsad ringbuffer. Frontend gör
självsekvenserad polling, vilket förhindrar överlappande statusanrop och kan
återuppta från senaste ID utan duplicerade notifieringar.
