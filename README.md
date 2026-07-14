# Provtidsbevakaren

En local-first tjänst som bevakar provtider, filtrerar träffar och kan notifiera,
reservera eller boka med **Pay later/faktura**. Standardläget körs helt lokalt
och öppnas som ett modernt webbgränssnitt utan konsolfönster.

Projektet har även ett färdigt, separat serverläge. Det är avsiktligt spärrat
tills driftmiljö, HTTPS, fjärrwebbläsare och hemligheter har konfigurerats.
Ingen publik instans distribueras från detta repository.

## Local mode – standard

1. Ladda ned `Provtidsbevakaren.exe` från en release eller bygg den själv.
2. Dubbelklicka på filen.
3. Programmet startar en server endast på `127.0.0.1` och öppnar kontrollpanelen.
4. Fyll i inställningarna och starta bevakningen.
5. När inloggning behövs öppnas ett privat Chrome/Edge-fönster för BankID.

Samma webbläsarsession hålls öppen för reservation och manuell slutföring.
Trafikverkets cookies finns bara i processminnet och raderas vid stopp. Formulär,
personnummer och webhook sparas inte lokalt efter att programmet stängts.

### Köra från källkod

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python run.py
```

### Test och EXE-bygge

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m compileall -q provtidsbevakaren run.py
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

Den färdiga filen skapas som `dist\Provtidsbevakaren.exe`.

## Körlägen

| Egenskap | Local mode | Server mode |
|---|---|---|
| Standard | Ja | Nej, explicit spärrat |
| UI | Lokal webbläsare | Publik HTTPS-webbplats |
| Sessionscookies | Endast processminne | Isolerat processminne per användare |
| Konfiguration | Endast minne | Krypterad SQLite |
| BankID-webbläsare | Lokal Chrome/Edge | Isolerad Remote WebDriver |
| Åtkomst | Engångstoken på localhost | Lösenord, signerad HttpOnly-cookie och CSRF |

## Aktivera server mode senare

Serverläget startar inte om något obligatoriskt skydd saknas.

1. Sätt upp en HTTPS-reverse proxy.
2. Sätt upp en isolerad Selenium-kompatibel Remote WebDriver och en autentiserad
   viewer/noVNC-adress för användaren.
3. Skapa hemligheter lokalt:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -m provtidsbevakaren.launcher --hash-password
```

4. Kopiera `.env.server.example` till en hemlig miljökonfiguration och ange:
   `APP_SECRET_KEY`, `DATA_ENCRYPTION_KEY`, `SERVER_USERS_JSON`, `PUBLIC_ORIGIN`,
   `ALLOWED_HOSTS`, `REMOTE_WEBDRIVER_URL` och `REMOTE_BROWSER_VIEW_URL`.
5. Sätt sist `APP_MODE=server` och `ENABLE_SERVER_MODE=true`.
6. Starta containern bakom HTTPS och verifiera `/api/health`.

Exempel på format för användare, där hashvärdet kommer från kommandot ovan:

```json
{"alfred":"pbkdf2_sha256$600000$..."}
```

Miljöfilen ska aldrig checkas in. Mer information finns i
[arkitekturdokumentet](docs/ARCHITECTURE.md) och [säkerhetsguiden](docs/SECURITY.md).

## Viktiga garantier

- Bokning sker aldrig innan servern har bekräftat exakt datum, tid och plats.
- Muterande reservations- och faktura-anrop återförs inte automatiskt.
- Vid bokningsfel öppnas reservationssidan i samma autentiserade session.
- Varje användare har ett separat bevakningsjobb och separat webbläsarsession.
- Loggar och API-svar exponerar inte personnummer, cookies eller webhook-adresser.
- Serverläge kräver HTTPS, signerad session, CSRF, krypterad lagring och
  explicit aktivering.

Projektet är inte en officiell tjänst från Trafikverket.
