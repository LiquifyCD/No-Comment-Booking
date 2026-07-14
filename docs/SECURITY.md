# Säkerhet

## Hemligheter och personuppgifter

- Lägg aldrig `.env`, `config.json`, databasfiler, cookies, personnummer eller
  Discord-webhooks i Git.
- Local mode sparar formulär och cookies endast i minnet.
- Server mode krypterar formulärkonfiguration före SQLite-skrivning.
- Trafikverkets cookies serialiseras aldrig till lagringen.
- Serverloggar får inte innehålla request bodies eller hemligheter.

## Driftkrav för server mode

- Avsluta TLS i en betrodd reverse proxy och vidarebefordra endast till den
  privata applikationsporten.
- Begränsa Remote WebDriver till applikationsnätverket. Exponera den aldrig
  direkt mot internet.
- Skydda viewer/noVNC med samma användaridentitet eller en kortlivad signerad URL.
- Kör en separat browsercontainer/profil per användare och radera den vid stopp.
- Säkerhetskopiera krypteringsnyckeln separat från databasen och rotera
  sessionsnyckeln vid misstänkt exponering.
- Begränsa containerresurser, loggretention och samtidiga användarsessioner.

## Webbskydd

Applikationen använder Secure/HttpOnly/SameSite-cookie i server mode,
serverlagrad sessionsrevokering, CSRF-token på alla muterande API-anrop,
Trusted Host-validering, strikt Content Security Policy, HSTS och no-store.
Lösenord hashades med PBKDF2-SHA256 och individuellt salt; misslyckade
inloggningar begränsas per klientadress.

## Incidentstopp

Stäng av tjänsten genom att sätta `ENABLE_SERVER_MODE=false`, stoppa containern,
rotera `APP_SECRET_KEY`, återkalla viewer-sessioner och radera aktiva
browsercontainers. Byt `DATA_ENCRYPTION_KEY` genom kontrollerad dekryptering och
omkryptering; att bara ersätta nyckeln gör befintlig data oläsbar.
