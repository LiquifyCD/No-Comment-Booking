"""Bevakning av lediga provtider hos Trafikverket."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://fp.trafikverket.se/Boka"
BOOKING_APP_URL = f"{BASE_URL}/ng/"
RESERVATION_PAGE_URL = f"{BASE_URL}/ng/reservation"
DEFAULT_TIMEOUT = (5, 20)
EXAMPLE_DISCORD_WEBHOOK = ""
LOGGER = logging.getLogger("trafikverket-bot")

COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json; charset=utf-8",
    "Origin": "https://fp.trafikverket.se",
    "Referer": "https://fp.trafikverket.se/Boka/ng/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Language": "sv-SE,sv;q=0.9",
    "User-Agent": "Trafikverket-time-monitor/1.0",
}


class BotError(RuntimeError):
    """Fel som kan visas för användaren utan en traceback."""


class AuthenticationRequiredError(BotError):
    pass


class ApiResponseError(BotError):
    pass


class ReservationStateError(ApiResponseError):
    pass


@dataclass(frozen=True)
class Config:
    name: str
    ssn: str
    licence_id: int
    examination_type_id: int
    location_id: int
    nearby_location_ids: tuple[int, ...] = ()
    vehicle_type_id: int = 1
    tachograph_type_id: int = 1
    occasion_choice_id: int = 1
    language_id: int = 13
    date_from: str | None = None
    date_to: str | None = None
    earliest_time: str | None = None
    latest_time: str | None = None
    allowed_weekdays: tuple[int, ...] | None = None
    poll_interval_seconds: float = 60
    discord_webhook_url: str = ""
    auto_reserve: bool = False
    auto_book: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        if "cookie" in raw:
            raise BotError("Fältet 'cookie' stöds inte längre. Ta bort det från config-filen.")
        required = ("name", "ssn", "licence_id", "examination_type_id", "location_id")
        missing = [key for key in required if raw.get(key) in (None, "")]
        if missing:
            raise BotError(f"Saknade obligatoriska config-fält: {', '.join(missing)}")

        try:
            weekdays = raw.get("allowed_weekdays")
            config = cls(
                name=str(raw["name"]).strip(),
                ssn=str(raw["ssn"]).strip(),
                licence_id=int(raw["licence_id"]),
                examination_type_id=int(raw["examination_type_id"]),
                location_id=int(raw["location_id"]),
                nearby_location_ids=tuple(
                    int(value) for value in raw.get("nearby_location_ids", [])
                ),
                vehicle_type_id=int(raw.get("vehicle_type_id", 1)),
                tachograph_type_id=int(raw.get("tachograph_type_id", 1)),
                occasion_choice_id=int(raw.get("occasion_choice_id", 1)),
                language_id=int(raw.get("language_id", 13)),
                date_from=raw.get("date_from"),
                date_to=raw.get("date_to"),
                earliest_time=raw.get("earliest_time"),
                latest_time=raw.get("latest_time"),
                allowed_weekdays=None
                if weekdays is None
                else tuple(int(value) for value in weekdays),
                poll_interval_seconds=float(raw.get("poll_interval_seconds", 60)),
                discord_webhook_url=str(raw.get("discord_webhook_url", "")).strip(),
                auto_reserve=bool(raw.get("auto_reserve", False)),
                auto_book=bool(raw.get("auto_book", False)),
            )
        except (TypeError, ValueError) as exc:
            raise BotError(f"Ogiltig datatyp i config: {exc}") from exc
        config.validate()
        return config

    def validate(self) -> None:
        if not re.fullmatch(r"\d{8}-?\d{4}", self.ssn):
            raise BotError("ssn måste ha formatet ÅÅÅÅMMDD-XXXX.")
        if any(
            value <= 0 for value in (self.licence_id, self.examination_type_id, self.location_id)
        ):
            raise BotError("licence_id, examination_type_id och location_id måste vara positiva.")
        if self.poll_interval_seconds < 10:
            raise BotError("poll_interval_seconds måste vara minst 10 sekunder.")
        if self.allowed_weekdays is not None and any(
            day not in range(7) for day in self.allowed_weekdays
        ):
            raise BotError("allowed_weekdays får bara innehålla heltal 0–6.")
        for value, field_name, parser_format in (
            (self.date_from, "date_from", "%Y-%m-%d"),
            (self.date_to, "date_to", "%Y-%m-%d"),
            (self.earliest_time, "earliest_time", "%H:%M"),
            (self.latest_time, "latest_time", "%H:%M"),
        ):
            if value:
                try:
                    datetime.strptime(value, parser_format)
                except ValueError as exc:
                    raise BotError(f"{field_name} har ogiltigt format: {value}") from exc
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise BotError("date_from får inte vara senare än date_to.")
        if self.earliest_time and self.latest_time and self.earliest_time > self.latest_time:
            raise BotError("earliest_time får inte vara senare än latest_time.")
        if self.discord_webhook_url and not re.fullmatch(
            r"https://discord\.com/api/webhooks/\d+/[A-Za-z0-9_-]+",
            self.discord_webhook_url,
        ):
            raise BotError("discord_webhook_url har ogiltigt format.")
        if self.auto_reserve and self.auto_book:
            raise BotError(
                "Välj antingen automatisk reservation eller automatisk bokning, inte båda."
            )


class TrafikverketClient:
    """API-klient med cookies endast i processens minne."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update(COMMON_HEADERS)
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"POST"}),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.mount(
            f"{BASE_URL}/create-reservation",
            HTTPAdapter(max_retries=Retry(total=0, connect=0, read=0, redirect=0, status=0)),
        )
        self.session.mount(
            f"{BASE_URL}/invoice-payment",
            HTTPAdapter(max_retries=Retry(total=0, connect=0, read=0, redirect=0, status=0)),
        )

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.session.post(
                f"{BASE_URL}/{path}", json=payload or {}, timeout=DEFAULT_TIMEOUT
            )
        except requests.RequestException as exc:
            raise BotError(f"Nätverksfel vid {path}: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthenticationRequiredError(
                "Tjänsten kräver autentisering. Cookie-fri bevakning kan inte fortsätta."
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise ApiResponseError(f"{path} svarade med HTTP {response.status_code}") from exc

        content_type = response.headers.get("Content-Type", "").lower()
        if "json" not in content_type:
            raise AuthenticationRequiredError(
                "Tjänsten returnerade inte API-data; inloggning kan krävas."
            )
        try:
            data = response.json()
        except requests.JSONDecodeError as exc:
            raise ApiResponseError(f"{path} returnerade ogiltig JSON") from exc
        if not isinstance(data, dict):
            raise ApiResponseError(f"{path} returnerade oväntat dataformat")
        api_status = data.get("status")
        api_data = data.get("data")
        api_success = api_data.get("success") if isinstance(api_data, dict) else None
        if (isinstance(api_status, int) and api_status >= 400) or api_success is False:
            message = api_data.get("message") if isinstance(api_data, dict) else None
            message = str(message or f"API-fel {api_status or 'utan status'}")
            if data.get("type") == "LoginRequiredException" or api_status in (401, 403):
                raise AuthenticationRequiredError(message)
            raise ApiResponseError(message)
        return data

    def initialize(self) -> None:
        self.post("start")

    def booking_hindrances(self, booking_session: dict[str, Any]) -> dict[str, Any]:
        return self.post("booking-hindrances", {"bookingSession": booking_session})

    def occasion_bundles(
        self, booking_session: dict[str, Any], occasion_query: dict[str, Any]
    ) -> dict[str, Any]:
        return self.post(
            "occasion-bundles",
            {"bookingSession": booking_session, "occasionBundleQuery": occasion_query},
        )

    def create_reservation(
        self, booking_session: dict[str, Any], occasion_bundle: dict[str, Any]
    ) -> dict[str, Any]:
        return self.post(
            "create-reservation",
            {"bookingSession": booking_session, "occasionBundle": occasion_bundle},
        )

    def get_reservation_time(self, expiry_dates: list[str]) -> dict[str, Any]:
        return self.post("get-reservation-time", {"expiryDates": expiry_dates})

    def reservation_information(self, booking_session: dict[str, Any]) -> dict[str, Any]:
        return self.post("reservation-information", {"bookingSession": booking_session})

    def invoice_payment(
        self, booking_session: dict[str, Any], bundle_reservation: dict[str, Any]
    ) -> dict[str, Any]:
        return self.post(
            "invoice-payment",
            {"bookingSession": booking_session, "bundleReservation": bundle_reservation},
        )

    def summary(self, ssn: str, booking_id: str, licence_id: int) -> dict[str, Any]:
        return self.post(
            "summary",
            {"socialSecurityNumber": ssn, "bookingId": booking_id, "licenceId": licence_id},
        )

    def close(self) -> None:
        self.session.cookies.clear()
        self.session.close()


class BrowserLoginSession:
    """Owns the temporary authenticated browser until the monitor is stopped."""

    def __init__(self, driver: Any, profile: tempfile.TemporaryDirectory[str], imported: int):
        self.driver = driver
        self.profile = profile
        self.imported = imported
        self._closed = False

    def is_alive(self) -> bool:
        if self._closed:
            return False
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            return False

    def show_reservation_page(self, client: TrafikverketClient) -> None:
        """Copy refreshed in-memory cookies back and open the reservation route."""
        if self._closed:
            return
        self.driver.get(BOOKING_APP_URL)
        for cookie in client.session.cookies:
            if "trafikverket.se" not in (cookie.domain or "fp.trafikverket.se"):
                continue
            selenium_cookie: dict[str, Any] = {
                "name": cookie.name,
                "value": cookie.value,
                "path": cookie.path or "/",
            }
            if cookie.domain:
                selenium_cookie["domain"] = cookie.domain
            try:
                self.driver.delete_cookie(cookie.name)
                self.driver.add_cookie(selenium_cookie)
            except Exception as exc:
                LOGGER.warning("Kunde inte synkronisera cookie %s: %s", cookie.name, exc)
        self.driver.get(RESERVATION_PAGE_URL)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.driver.quit()
        except Exception:
            LOGGER.warning("Webbläsarfönstret kunde inte stängas automatiskt.")
        finally:
            self.profile.cleanup()


def manual_browser_login(
    client: TrafikverketClient,
    *,
    driver_factory: Callable[[str], Any] | None = None,
    validator: Callable[[], Any] | None = None,
    status_callback: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    timeout_seconds: float = 300,
    poll_interval: float = 1,
) -> BrowserLoginSession:
    """Öppna en privat webbläsare som behålls efter en godkänd inloggning."""
    status = status_callback or (lambda message: LOGGER.info("%s", message))
    cancelled = cancel_event or threading.Event()

    def import_cookies(cookies: list[dict[str, Any]]) -> int:
        client.session.cookies.clear()
        imported = 0
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain", "fp.trafikverket.se")
            if not name or value is None or "trafikverket.se" not in domain:
                continue
            client.session.cookies.set(
                name,
                value,
                domain=domain,
                path=cookie.get("path", "/"),
            )
            imported += 1
        return imported

    profile = tempfile.TemporaryDirectory(prefix="trafikverket-login-")
    profile_dir = profile.name
    driver = None
    succeeded = False
    try:
        if driver_factory is None:
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options as ChromeOptions
                from selenium.webdriver.edge.options import Options
            except ImportError as exc:
                raise BotError(
                    "Selenium saknas. Bygg om programmet eller installera requirements.txt."
                ) from exc
            chrome_paths = (
                Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", ""))
                / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
            )
            edge_paths = (
                Path(os.environ.get("PROGRAMFILES(X86)", ""))
                / "Microsoft/Edge/Application/msedge.exe",
                Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
            )
            chrome_path = next((path for path in chrome_paths if path.is_file()), None)
            edge_path = next((path for path in edge_paths if path.is_file()), None)
            if chrome_path is not None:
                options = ChromeOptions()
                options.binary_location = str(chrome_path)
                options.add_argument("--incognito")
                browser_name = "Chrome"
                browser_factory = webdriver.Chrome
            elif edge_path is not None:
                options = Options()
                options.binary_location = str(edge_path)
                options.add_argument("--inprivate")
                browser_name = "Edge"
                browser_factory = webdriver.Edge
            else:
                raise BotError("Varken Google Chrome eller Microsoft Edge hittades.")
            options.add_argument("--disable-sync")
            options.add_argument("--no-first-run")
            options.add_argument(f"--user-data-dir={profile_dir}")
            status(f"Öppnar {browser_name} för BankID-inloggning …")
            driver = browser_factory(options=options)
        else:
            driver = driver_factory(profile_dir)

        driver.get("https://fp.trafikverket.se/Boka/ng/")
        status("Slutför BankID-inloggningen i webbläsaren. Programmet fortsätter automatiskt.")
        baseline = {(cookie.get("name"), cookie.get("value")) for cookie in driver.get_cookies()}
        auth_cookie_names = {"FpsExternalIdentity", "LoginValid"}
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if cancelled.is_set():
                raise BotError("Inloggningen avbröts.")
            cookies = driver.get_cookies()
            snapshot = {(cookie.get("name"), cookie.get("value")) for cookie in cookies}
            names = {str(cookie.get("name")) for cookie in cookies}
            if auth_cookie_names.intersection(names) or snapshot != baseline:
                imported = import_cookies(cookies)
                if imported:
                    try:
                        if validator is not None:
                            validator()
                    except AuthenticationRequiredError:
                        client.session.cookies.clear()
                    else:
                        status("Inloggningen är klar. Webbläsaren hålls öppen under bevakningen.")
                        succeeded = True
                        return BrowserLoginSession(driver, profile, imported)
            cancelled.wait(poll_interval)
        raise AuthenticationRequiredError("Inloggningen hann inte slutföras inom fem minuter.")
    except AuthenticationRequiredError:
        raise
    except Exception as exc:
        raise BotError(f"Kunde inte genomföra webbläsarinloggningen: {exc}") from exc
    finally:
        if not succeeded:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    LOGGER.warning("Webbläsarfönstret kunde inte stängas automatiskt.")
            profile.cleanup()
            if cancelled.is_set():
                client.session.cookies.clear()


def nested(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ApiResponseError(f"API-svaret saknar fältet: {'.'.join(keys)}")
        value = value[key]
    return value


def notify_discord(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        return False
    try:
        response = requests.post(webhook_url, json={"content": message}, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        LOGGER.warning("Discord-notisen misslyckades: %s", exc)
        return False


def slot_key(occasion: dict[str, Any]) -> str:
    try:
        return f"{occasion['locationId']}|{occasion['date']}|{occasion['time']}"
    except KeyError as exc:
        raise ApiResponseError(f"En provtid saknar fältet {exc.args[0]}") from exc


def slot_matches_filters(occasion: dict[str, Any], cfg: Config) -> bool:
    try:
        date_str = str(occasion["date"])
        time_str = str(occasion["time"])[:5]
        slot_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        datetime.strptime(time_str, "%H:%M")
    except (KeyError, ValueError) as exc:
        raise ApiResponseError(f"Ogiltig provtid i API-svaret: {occasion}") from exc

    if cfg.date_from and date_str < cfg.date_from:
        return False
    if cfg.date_to and date_str > cfg.date_to:
        return False
    if cfg.earliest_time and time_str < cfg.earliest_time:
        return False
    if cfg.latest_time and time_str > cfg.latest_time:
        return False
    return cfg.allowed_weekdays is None or slot_date.weekday() in cfg.allowed_weekdays


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Kunde inte läsa %s; börjar med tom historik: %s", path, exc)
        return set()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        LOGGER.warning("%s har fel format; börjar med tom historik", path)
        return set()
    return set(value)


def save_seen(path: Path, seen: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(sorted(set(seen)), handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def prune_seen(seen: set[str], today: date | None = None) -> set[str]:
    cutoff = (today or date.today()).isoformat()
    return {key for key in seen if len(key.split("|")) == 3 and key.split("|")[1] >= cutoff}


def build_booking_session(cfg: Config) -> dict[str, Any]:
    return {
        "socialSecurityNumber": cfg.ssn,
        "licenceId": cfg.licence_id,
        "bookingModeId": 0,
        "ignoreDebt": False,
        "ignoreBookingHindrance": False,
        "examinationTypeId": cfg.examination_type_id,
        "excludeExaminationCategories": [],
        "rescheduleTypeId": 0,
        "paymentIsActive": False,
        "paymentReference": "",
        "paymentUrl": "",
        "searchedMonths": 0,
    }


def build_occasion_query(cfg: Config) -> dict[str, Any]:
    return {
        "startDate": datetime.now(UTC).isoformat(),
        "searchedMonths": 0,
        "locationId": cfg.location_id,
        "nearbyLocationIds": list(cfg.nearby_location_ids),
        "languageId": cfg.language_id,
        "vehicleTypeId": cfg.vehicle_type_id,
        "tachographTypeId": cfg.tachograph_type_id,
        "occasionChoiceId": cfg.occasion_choice_id,
        "examinationTypeId": cfg.examination_type_id,
    }


def extract_matching_occasions(result: dict[str, Any], cfg: Config) -> list[dict[str, Any]]:
    bundles = nested(result, "data", "bundles")
    if not isinstance(bundles, list):
        raise ApiResponseError("API-fältet data.bundles är inte en lista")
    matches: list[dict[str, Any]] = []
    for bundle in bundles:
        occasions = bundle.get("occasions") if isinstance(bundle, dict) else None
        if not isinstance(occasions, list):
            raise ApiResponseError("Ett bundle saknar listan occasions")
        reservation_bundle = {
            "cost": bundle.get("cost"),
            "occasions": [
                dict(item, examinationId=None) if isinstance(item, dict) else item
                for item in occasions
            ],
        }
        for raw_occasion in occasions:
            if not isinstance(raw_occasion, dict):
                raise ApiResponseError("En provtid har oväntat format")
            occasion = dict(raw_occasion)
            occasion["cost"] = bundle.get("cost")
            occasion["_reservation_bundle"] = reservation_bundle
            if slot_matches_filters(occasion, cfg):
                matches.append(occasion)
    return matches


def complete_invoice_booking(
    client: TrafikverketClient,
    cfg: Config,
    booking_session: dict[str, Any],
    bundle_reservation: dict[str, Any],
) -> dict[str, Any]:
    """Slutför en befintlig reservation med betalningssättet faktura/pay later."""
    payment_result = client.invoice_payment(booking_session, bundle_reservation)
    booking_id = nested(payment_result, "data", "bookingId")
    if not isinstance(booking_id, (str, int)) or not str(booking_id):
        raise ApiResponseError("Fakturabokningen saknar bookingId")
    summary = client.summary(cfg.ssn, str(booking_id), cfg.licence_id)
    return {"booking_id": str(booking_id), "summary": summary}


def verified_reservation_information(
    client: TrafikverketClient,
    booking_session: dict[str, Any],
    occasion: dict[str, Any],
) -> dict[str, Any]:
    """Read back the server state and ensure the requested slot is actually reserved."""
    information = nested(client.reservation_information(booking_session), "data")
    if not isinstance(information, dict):
        raise ApiResponseError("Reservationsinformationen har oväntat format")
    reservations = information.get("reservations")
    if not isinstance(reservations, list):
        raise ApiResponseError("Reservationsinformationen saknar reservationer")

    wanted_date = str(occasion.get("date", ""))
    wanted_time = str(occasion.get("time", ""))[:5]
    wanted_location = occasion.get("locationId")
    for reservation in reservations:
        if not isinstance(reservation, dict):
            continue
        same_slot = (
            str(reservation.get("date", "")) == wanted_date
            and str(reservation.get("time", ""))[:5] == wanted_time
            and (wanted_location is None or reservation.get("locationId") == wanted_location)
        )
        if same_slot:
            return information

    if reservations:
        active = reservations[0]
        active_description = (
            f"{active.get('date', '?')} {str(active.get('time', '?'))[:5]}"
            if isinstance(active, dict)
            else "en annan tid"
        )
        raise ReservationStateError(
            f"Rätt tid reserverades inte; aktiv reservation är {active_description}"
        )
    raise ReservationStateError("Trafikverket bekräftade ingen aktiv reservation")


def run_monitor(
    cfg: Config,
    client: TrafikverketClient,
    seen_path: Path,
    stop_event: threading.Event,
    *,
    max_polls: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    emit = event_callback or (lambda _event, _payload: None)
    booking_session = build_booking_session(cfg)
    seen = prune_seen(load_seen(seen_path))
    consecutive_errors = 0
    polls = 0

    client.initialize()
    hindrances = nested(client.booking_hindrances(booking_session), "data")
    if isinstance(hindrances, dict) and hindrances.get("canBookLicence") is False:
        LOGGER.warning("Bokningshinder: %s", hindrances.get("hindranceMessages", "okänt hinder"))

    LOGGER.info(
        "[%s] Bevakning startad, intervall %.0f sekunder", cfg.name, cfg.poll_interval_seconds
    )
    while not stop_event.is_set() and (max_polls is None or polls < max_polls):
        started = time.monotonic()
        try:
            result = client.occasion_bundles(booking_session, build_occasion_query(cfg))
            matches = extract_matching_occasions(result, cfg)
            new_matches = [occasion for occasion in matches if slot_key(occasion) not in seen]
            for occasion in new_matches:
                message = (
                    f"🚗 [{cfg.name}] Ny tid: {occasion['date']} {str(occasion['time'])[:5]} "
                    f"@ {occasion.get('locationName', 'okänd plats')} ({occasion.get('cost', '?')})"
                )
                LOGGER.info(message)
                notify_discord(cfg.discord_webhook_url, message)
                seen.add(slot_key(occasion))
                if cfg.auto_reserve or cfg.auto_book:
                    try:
                        reservation_bundle = occasion.get("_reservation_bundle")
                        if not isinstance(reservation_bundle, dict):
                            raise ApiResponseError("Provtiden saknar reservationsdata")
                        client.create_reservation(booking_session, reservation_bundle)
                        bundle_reservation = verified_reservation_information(
                            client, booking_session, occasion
                        )
                    except AuthenticationRequiredError:
                        raise
                    except ReservationStateError as exc:
                        popup_payload = {
                            "date": occasion["date"],
                            "time": str(occasion["time"])[:5],
                            "location": occasion.get("locationName", "okänd plats"),
                        }
                        failure = f"⚠️ [{cfg.name}] Reservationens serverstatus stämmer inte: {exc}"
                        LOGGER.error(failure)
                        notify_discord(cfg.discord_webhook_url, failure)
                        emit("booking_error", {**popup_payload, "error": str(exc)})
                        return
                    except BotError as exc:
                        LOGGER.warning("Automatisk reservation misslyckades: %s", exc)
                        notify_discord(
                            cfg.discord_webhook_url,
                            f"⚠️ [{cfg.name}] Reservationen misslyckades: {exc}",
                        )
                        continue
                    save_seen(seen_path, seen)
                    popup_payload = {
                        "date": occasion["date"],
                        "time": str(occasion["time"])[:5],
                        "location": occasion.get("locationName", "okänd plats"),
                    }
                    if cfg.auto_book:
                        try:
                            booking_result = complete_invoice_booking(
                                client, cfg, booking_session, bundle_reservation
                            )
                        except AuthenticationRequiredError:
                            raise
                        except BotError as exc:
                            failure = (
                                f"⚠️ [{cfg.name}] Tiden reserverades men fakturabokningen "
                                f"misslyckades: {exc}"
                            )
                            LOGGER.error(failure)
                            notify_discord(cfg.discord_webhook_url, failure)
                            emit("booking_error", {**popup_payload, "error": str(exc)})
                            return
                        booked_message = (
                            f"✅ [{cfg.name}] BOKAT {occasion['date']} "
                            f"{str(occasion['time'])[:5]} med Pay later/faktura. "
                            f"Boknings-ID: {booking_result['booking_id']}"
                        )
                        LOGGER.info(booked_message)
                        notify_discord(cfg.discord_webhook_url, booked_message)
                        emit(
                            "booked",
                            {**popup_payload, "booking_id": booking_result["booking_id"]},
                        )
                        return
                    reserved_message = (
                        f"✅ [{cfg.name}] Tiden {occasion['date']} {str(occasion['time'])[:5]} "
                        "är reserverad. Slutför bokningen innan reservationen löper ut."
                    )
                    LOGGER.info(reserved_message)
                    notify_discord(cfg.discord_webhook_url, reserved_message)
                    emit("reserved", popup_payload)
                    return
            save_seen(seen_path, seen)
            consecutive_errors = 0
        except (AuthenticationRequiredError, ApiResponseError):
            raise
        except BotError as exc:
            consecutive_errors += 1
            delay = min(cfg.poll_interval_seconds * (2 ** min(consecutive_errors - 1, 4)), 900)
            LOGGER.warning(
                "Tillfälligt fel (%d): %s; nytt försök om %.0f s", consecutive_errors, exc, delay
            )
            if max_polls is None and stop_event.wait(delay):
                break
        polls += 1
        if stop_event.is_set() or (max_polls is not None and polls >= max_polls):
            break
        elapsed = time.monotonic() - started
        delay = max(0, cfg.poll_interval_seconds - elapsed)
        if sleep is time.sleep:
            if stop_event.wait(delay):
                break
        else:
            sleep(delay)


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BotError(f"Config-filen finns inte: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise BotError(f"Kunde inte läsa config-filen: {exc}") from exc
    if not isinstance(raw, dict):
        raise BotError("Config-filens rot måste vara ett JSON-objekt.")
    return Config.from_dict(raw)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", type=Path)
    parser.add_argument("--once", action="store_true", help="Gör en sökning och avsluta")
    parser.add_argument("--test", action="store_true", help="Kör ett lokalt test utan API-anrop")
    parser.add_argument(
        "--discord-test", action="store_true", help="Skicka en testnotis till Discord"
    )
    return parser.parse_args(argv)


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def run_self_test(cfg: Config) -> None:
    sample = {
        "locationId": cfg.location_id,
        "locationName": "Testplats",
        "date": cfg.date_from or date.today().isoformat(),
        "time": cfg.earliest_time or "09:00",
    }
    slot_key(sample)
    slot_matches_filters(sample, cfg)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "seen.json"
        save_seen(path, {slot_key(sample)})
        if load_seen(path) != {slot_key(sample)}:
            raise BotError("Det lokala lagringstestet misslyckades.")
    LOGGER.info("Lokalt test godkänt: config, filter och lagring fungerar.")


def prompt_text(
    label: str,
    *,
    default: str | None = None,
    required: bool = True,
    input_fn: Callable[[str], str] = input,
) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input_fn(f"{label}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if value or not required:
            return value
        print("  Värdet måste anges.")


def prompt_int(
    label: str,
    default: int,
    *,
    minimum: int = 1,
    input_fn: Callable[[str], str] = input,
) -> int:
    while True:
        raw = prompt_text(label, default=str(default), input_fn=input_fn)
        try:
            value = int(raw)
        except ValueError:
            print("  Ange ett heltal.")
            continue
        if value < minimum:
            print(f"  Värdet måste vara minst {minimum}.")
            continue
        return value


def prompt_date_value(
    label: str,
    default: str,
    *,
    earliest: str | None = None,
    input_fn: Callable[[str], str] = input,
) -> str:
    while True:
        value = prompt_text(label, default=default, input_fn=input_fn)
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            print("  Använd formatet ÅÅÅÅ-MM-DD, exempelvis 2026-08-01.")
            continue
        if earliest and value < earliest:
            print(f"  Datumet får inte vara tidigare än {earliest}.")
            continue
        return value


def prompt_time_value(
    label: str,
    default: str,
    *,
    earliest: str | None = None,
    input_fn: Callable[[str], str] = input,
) -> str:
    while True:
        value = prompt_text(label, default=default, input_fn=input_fn)
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            print("  Använd 24-timmarsformatet TT:MM, exempelvis 08:00.")
            continue
        if earliest and value < earliest:
            print(f"  Tiden får inte vara tidigare än {earliest}.")
            continue
        return value


def prompt_int_list(
    label: str,
    *,
    default: str = "",
    minimum: int = 1,
    maximum: int | None = None,
    input_fn: Callable[[str], str] = input,
) -> tuple[int, ...]:
    while True:
        raw = prompt_text(label, default=default, required=False, input_fn=input_fn)
        if not raw:
            return ()
        try:
            values = tuple(dict.fromkeys(int(part.strip()) for part in raw.split(",")))
        except ValueError:
            print("  Ange heltal separerade med kommatecken.")
            continue
        if any(value < minimum or (maximum is not None and value > maximum) for value in values):
            limit = f"{minimum}–{maximum}" if maximum is not None else f"minst {minimum}"
            print(f"  Alla värden måste vara {limit}.")
            continue
        return values


def prompt_ssn(*, input_fn: Callable[[str], str] = input) -> str:
    while True:
        value = prompt_text("Personnummer (ÅÅÅÅMMDD-XXXX)", required=True, input_fn=input_fn)
        if re.fullmatch(r"\d{8}-?\d{4}", value):
            return value
        print("  Ogiltigt format. Använd ÅÅÅÅMMDD-XXXX.")


def prompt_webhook(*, input_fn: Callable[[str], str] = input) -> str:
    while True:
        value = prompt_text(
            "Discord webhook URL",
            default=EXAMPLE_DISCORD_WEBHOOK,
            input_fn=input_fn,
        )
        if re.fullmatch(r"https://discord\.com/api/webhooks/\d+/[A-Za-z0-9_-]+", value):
            return value
        print("  Ogiltig Discord-webhook. Den ska börja med https://discord.com/api/webhooks/")


def prompt_yes_no(
    label: str,
    *,
    default: bool = True,
    input_fn: Callable[[str], str] = input,
) -> bool:
    suffix = "J/n" if default else "j/N"
    while True:
        value = input_fn(f"{label} [{suffix}]: ").strip().lower()
        if not value:
            return default
        if value in ("j", "ja", "y", "yes"):
            return True
        if value in ("n", "nej", "no"):
            return False
        print("  Svara j eller n.")


def interactive_config(*, input_fn: Callable[[str], str] = input) -> Config:
    today = date.today()
    while True:
        print("\n=== Inställningsguide ===")
        print("Tryck Enter för att använda värdet inom [hakparenteser].")
        print("Inga inställningar eller inloggningscookies sparas när programmet stängs.\n")

        name = prompt_text("Namn som visas i notiser", default="Alfred", input_fn=input_fn)
        ssn = prompt_ssn(input_fn=input_fn)
        print("\nProvinställningar (standardvärdena passar projektets ursprungliga flöde):")
        licence_id = prompt_int("Körkortsbehörighetens ID", 23, input_fn=input_fn)
        examination_type_id = prompt_int("Provtypens ID", 52, input_fn=input_fn)
        location_id = prompt_int("Huvudortens plats-ID", 1000130, input_fn=input_fn)
        nearby = prompt_int_list(
            "Närliggande plats-ID:n, kommaseparerade (tomt = inga)", input_fn=input_fn
        )
        vehicle_type_id = prompt_int("Fordonstypens ID", 1, input_fn=input_fn)
        tachograph_type_id = prompt_int("Färdskrivartypens ID", 1, input_fn=input_fn)
        occasion_choice_id = prompt_int("Tidstypens ID", 1, input_fn=input_fn)
        language_id = prompt_int("Språkets ID", 13, input_fn=input_fn)

        print("\nFilter för önskade tider:")
        date_from = prompt_date_value("Tidigaste datum", today.isoformat(), input_fn=input_fn)
        date_to = prompt_date_value(
            "Senaste datum",
            (today + timedelta(days=90)).isoformat(),
            earliest=date_from,
            input_fn=input_fn,
        )
        earliest_time = prompt_time_value("Tidigaste klockslag", "08:00", input_fn=input_fn)
        latest_time = prompt_time_value(
            "Senaste klockslag", "17:00", earliest=earliest_time, input_fn=input_fn
        )
        weekdays = prompt_int_list(
            "Veckodagar 0=måndag … 6=söndag",
            default="0,1,2,3,4",
            minimum=0,
            maximum=6,
            input_fn=input_fn,
        )
        poll_interval = prompt_int("Sekunder mellan kontroller", 60, minimum=10, input_fn=input_fn)

        print("\nDiscord:")
        if prompt_yes_no("Aktivera Discord-notiser?", input_fn=input_fn):
            print("Ersätt exempel-webhooken med din riktiga webhook.")
            webhook = prompt_webhook(input_fn=input_fn)
        else:
            webhook = ""

        cfg = Config.from_dict(
            {
                "name": name,
                "ssn": ssn,
                "licence_id": licence_id,
                "examination_type_id": examination_type_id,
                "location_id": location_id,
                "nearby_location_ids": list(nearby),
                "vehicle_type_id": vehicle_type_id,
                "tachograph_type_id": tachograph_type_id,
                "occasion_choice_id": occasion_choice_id,
                "language_id": language_id,
                "date_from": date_from,
                "date_to": date_to,
                "earliest_time": earliest_time,
                "latest_time": latest_time,
                "allowed_weekdays": list(weekdays),
                "poll_interval_seconds": poll_interval,
                "discord_webhook_url": webhook,
            }
        )

        masked_ssn = f"{ssn[:8]}-****"
        print("\n=== Sammanfattning ===")
        print(f"Namn: {cfg.name} | Personnummer: {masked_ssn}")
        print(
            f"Prov: behörighet {cfg.licence_id}, typ {cfg.examination_type_id}, plats {cfg.location_id}"
        )
        print(f"Period: {cfg.date_from}–{cfg.date_to}, {cfg.earliest_time}–{cfg.latest_time}")
        print(f"Veckodagar: {','.join(map(str, cfg.allowed_weekdays or ())) or 'alla'}")
        print(
            f"Intervall: {cfg.poll_interval_seconds:.0f} s | Discord: {'angiven' if webhook else 'av'}"
        )
        if prompt_yes_no("Är inställningarna korrekta?", input_fn=input_fn):
            return cfg
        print("Vi börjar om guiden.")


def interactive_choice() -> str:
    print("\nTrafikverket provtidsbevakare")
    print("1. Kör lokalt test (inga API-anrop)")
    print("2. Skicka testnotis till Discord")
    print("3. Starta livebevakning")
    print("4. Avsluta")
    return input("Välj 1–4: ").strip()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    interactive = argv is None and len(sys.argv) == 1
    if interactive:
        from gui import launch_gui

        return launch_gui(sys.modules[__name__])
    stop_event = threading.Event()
    client: TrafikverketClient | None = None
    browser_session: BrowserLoginSession | None = None
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: stop_event.set())
    try:
        config_path = args.config or (application_dir() / "config.json")
        cfg = load_config(config_path)
        state_directory = config_path.resolve().parent
        if args.test:
            run_self_test(cfg)
            return 0
        if args.discord_test:
            if not cfg.discord_webhook_url:
                raise BotError("Discord-notiser är avstängda i inställningarna.")
            if not notify_discord(
                cfg.discord_webhook_url,
                f"✅ [{cfg.name}] Test notification from No-Comment-Booking",
            ):
                raise BotError("Discord-testet misslyckades.")
            LOGGER.info("Discord-testet lyckades.")
            return 0
        safe_name = "".join(character if character.isalnum() else "_" for character in cfg.name)
        seen_path = state_directory / f"seen_{safe_name}.json"
        client = TrafikverketClient()
        try:
            run_monitor(
                cfg,
                client,
                seen_path,
                stop_event,
                max_polls=1 if args.once else None,
            )
        except AuthenticationRequiredError:
            LOGGER.info("Tjänsten kräver inloggning. Startar manuellt inloggningsfönster.")
            browser_session = manual_browser_login(
                client,
                validator=lambda: client.occasion_bundles(
                    build_booking_session(cfg), build_occasion_query(cfg)
                ),
                cancel_event=stop_event,
            )
            run_monitor(
                cfg,
                client,
                seen_path,
                stop_event,
                max_polls=1 if args.once else None,
            )
        return 0
    except AuthenticationRequiredError as exc:
        LOGGER.error("Inloggningen godkändes inte: %s", exc)
        return 3
    except (BotError, KeyboardInterrupt) as exc:
        LOGGER.error("%s", exc)
        return 2
    finally:
        if browser_session is not None:
            browser_session.close()
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
