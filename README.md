# No-Comment-Booking

No-Comment-Booking is a local-first service for monitoring driving-test appointments. It can notify, reserve, or complete a reservation using **Pay later/invoice**. The default Windows application opens a responsive web dashboard and does not require a console window.

An opt-in server runtime is included for later deployment. It remains disabled until HTTPS, authentication, encryption, and isolated browser infrastructure are configured.

## Local mode

1. Download or build `No-Comment-Booking.exe`.
2. Double-click it. The application binds only to `127.0.0.1` and opens the dashboard.
3. Enter the identity number and select **Connect Mobile BankID**.
4. Scan the rotating QR code shown inside the dashboard, or open BankID on the same device.
5. Select a licence, examination type, and searchable test location by name. Their numeric IDs are resolved automatically from Trafikverket's API.
6. Start monitoring.

The start-date minimum is always the user's current local date and is revalidated by the backend. Trafikverket cookies and BankID challenge data exist only in process memory and are cleared when the program closes. The separate browser flow is retained only as an explicit fallback if integrated authentication fails.

### Run from source

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python run.py
```

### Verify and build

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest
.venv\Scripts\python -m compileall -q provtidsbevakaren run.py
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

The executable is written to `dist\No-Comment-Booking.exe`.

## Main workflow

- Mobile BankID starts and rotates its QR code inside the dashboard.
- The backend keeps BankID reference, QR secret, and autostart token out of frontend state.
- After login, `licence-information` supplies readable licence choices.
- Selecting a licence loads its examination types and all available locations from `search-information`.
- Locations are deduplicated, alphabetically sorted, and searchable.
- A found slot can trigger notification, automatic reservation, or automatic Pay later booking.
- A reserved slot remains available in the dashboard for a guarded, single-click Pay later completion attempt.

## Operating modes

| Capability | Local mode | Server mode |
|---|---|---|
| Default | Yes | No, explicitly disabled |
| Interface | Local web dashboard | Public HTTPS website |
| Trafikverket cookies | Process memory only | Isolated process memory per user |
| Configuration | Memory only | Encrypted SQLite |
| BankID | Integrated QR/same-device link | Integrated QR/same-device link |
| Browser fallback | Local Chrome or Edge | Isolated Remote WebDriver |
| Access control | One-time localhost token | Password, signed HttpOnly cookie, and CSRF |

## Enabling server mode later

Server mode fails closed if any mandatory protection is missing.

1. Configure an HTTPS reverse proxy.
2. Configure an isolated Selenium-compatible Remote WebDriver and authenticated viewer/noVNC URL per user for browser fallback.
3. Generate secrets:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -m provtidsbevakaren.launcher --hash-password
```

4. Copy `.env.server.example` to a private environment file and set `APP_SECRET_KEY`, `DATA_ENCRYPTION_KEY`, `SERVER_USERS_JSON`, `PUBLIC_ORIGIN`, `ALLOWED_HOSTS`, `REMOTE_WEBDRIVER_URL`, and `REMOTE_BROWSER_VIEW_URL`.
5. Set `APP_MODE=server` and `ENABLE_SERVER_MODE=true`.
6. Start the container behind HTTPS and verify `/api/health`.

Never commit the environment file. See [architecture](docs/ARCHITECTURE.md) and [security](docs/SECURITY.md).

## Reliability and safety

- Reservation and invoice mutations are never retried automatically.
- Server state is read back before a reservation is treated as successful.
- Duplicate BankID, catalog, monitoring, and booking actions are guarded.
- Failed catalog refreshes leave the last valid in-memory catalog intact.
- Missing fields and expired authentication produce explicit UI errors.
- Logs and public API state exclude identity numbers, cookies, webhook URLs, and BankID challenge secrets.

No-Comment-Booking is not an official Trafikverket service.
