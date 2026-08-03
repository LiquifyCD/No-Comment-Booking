from __future__ import annotations

import dataclasses
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import bankid, catalog, engine
from .settings import AppSettings
from .storage import StateStore

LOGGER = logging.getLogger("provtidsbevakaren.runtime")

STATUS_DEFINITIONS: dict[str, tuple[str, str]] = {
    "ready": ("Redo", "Identifiera dig med BankID för att börja."),
    "bankid_starting": ("Startar BankID", "En säker BankID-inloggning förbereds."),
    "bankid_waiting": ("Väntar på BankID", "Skanna QR-koden eller öppna BankID."),
    "bankid_connected": ("BankID anslutet", "Din identitet har verifierats."),
    "loading_options": ("Hämtar alternativ", "Dina bokningsalternativ hämtas."),
    "ready_to_start": ("Redo att starta", "Välj inställningar och starta bevakningen."),
    "monitor_starting": ("Startar bevakning", "Inställningarna kontrolleras på servern."),
    "monitoring": ("Bevakning aktiv", "Lediga tider kontrolleras automatiskt."),
    "match_found": ("Träff hittad", "En tid som matchar dina val har hittats."),
    "reserving": ("Reserverar tid", "Den valda tiden reserveras hos Trafikverket."),
    "booking": ("Bokar tid", "Reservationen slutförs med Pay later/faktura."),
    "booked": ("Bokning klar", "Tiden är bokad och bekräftad av Trafikverket."),
    "stopping": ("Stoppar", "Pågående arbete avslutas säkert."),
    "stopped": ("Stoppad", "Bevakningen är stoppad."),
    "reconnecting": ("Återansluter", "Anslutningen till serverstatus återställs."),
    "error": ("Ett fel uppstod", "Kontrollera felet och försök igen."),
    "action_required": ("Åtgärd krävs", "En reservation behöver hanteras innan du fortsätter."),
}

STOPPABLE_STATES = {
    "bankid_starting",
    "bankid_waiting",
    "monitor_starting",
    "monitoring",
    "match_found",
    "reserving",
    "booking",
}
STARTABLE_STATES = {"ready_to_start", "stopped", "booked", "error"}
AUTHENTICATABLE_STATES = {"ready", "stopped", "error"}
TERMINAL_STATES = {"booked", "error", "action_required"}


class RuntimeConflict(RuntimeError):
    pass


class EventBuffer:
    def __init__(self, max_events: int = 100):
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._next_id = 1
        self._lock = threading.RLock()

    def add(self, kind: str, message: str, data: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._events.append(
                {
                    "id": self._next_id,
                    "type": kind,
                    "message": message,
                    "data": data or {},
                    "timestamp": time.time(),
                }
            )
            self._next_id += 1

    def after(self, event_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events if event["id"] > event_id]


class MonitorJob:
    def __init__(
        self,
        user_id: str,
        settings: AppSettings,
        state_store: StateStore,
        data_dir: Path,
    ):
        self.user_id = user_id
        self.settings = settings
        self.state_store = state_store
        self.data_dir = data_dir
        self.events = EventBuffer()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._state = "ready"
        self._status_description = STATUS_DEFINITIONS["ready"][1]
        self._status_updated_at = time.time()
        self._browser_session: engine.BrowserLoginSession | None = None
        self._remote_view_url = ""
        self._client = engine.TrafikverketClient()
        self._bankid = bankid.BankIdFlow(self._client)
        self._auth_thread: threading.Thread | None = None
        self._force_browser_fallback = threading.Event()
        self._cancel_authentication = threading.Event()
        self._retry_authentication = threading.Event()
        self._pending_booking: dict[str, Any] | None = None
        self._pending_done = threading.Event()
        self._booking_lock = threading.Lock()
        self._catalog_lock = threading.Lock()
        self._catalog: catalog.BookingCatalog | None = None
        self._catalog_updated_at = 0.0
        self._translations: dict[str, str] | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set_state(self, code: str, description: str | None = None) -> None:
        if code not in STATUS_DEFINITIONS:
            raise ValueError(f"Unknown runtime state: {code}")
        resolved_description = description or STATUS_DEFINITIONS[code][1]
        with self._lock:
            if code != self._state or resolved_description != self._status_description:
                self._state = code
                self._status_description = resolved_description
                self._status_updated_at = time.time()

    def _status_payload(self) -> dict[str, Any]:
        with self._lock:
            code = self._state
            label, default_description = STATUS_DEFINITIONS[code]
            description = self._status_description or default_description
            updated_at = self._status_updated_at
            catalog_ready = self._catalog is not None
        bankid_snapshot = self._bankid.snapshot()
        return {
            "code": code,
            "label": label,
            "description": description,
            "updatedAt": updated_at,
            "canStart": bool(
                code in STARTABLE_STATES and bankid_snapshot["authenticated"] and catalog_ready
            ),
            "canStop": code in STOPPABLE_STATES,
            "canAuthenticate": bool(
                code in AUTHENTICATABLE_STATES and not bankid_snapshot["authenticated"]
            ),
        }

    def snapshot(self, after: int = 0) -> dict[str, Any]:
        with self._lock:
            reservation = dict(self._pending_booking["display"]) if self._pending_booking else None
        status = self._status_payload()
        return {
            "state": status["code"],
            "status": status,
            "events": self.events.after(after),
            "browserViewUrl": self._remote_view_url,
            "bankId": self._bankid.snapshot(),
            "reservation": reservation,
            "catalogUpdatedAt": self._catalog_updated_at,
        }

    def status_snapshot(self) -> dict[str, Any]:
        """Minimal live state for regular users; detailed events stay admin-only."""
        status = self._status_payload()
        return {
            "state": status["code"],
            "status": status,
            "bankId": self._bankid.snapshot(),
            "catalogUpdatedAt": self._catalog_updated_at,
        }

    def start(self, raw_config: dict[str, Any]) -> None:
        resolved_config = dict(raw_config)
        resolved_config["poll_interval_seconds"] = 15
        resolved_config["auto_reserve"] = False
        resolved_config["name"] = str(resolved_config.get("name") or "Min provtidsbevakning")
        resolved_config["ssn"] = str(
            resolved_config.get("ssn") or self._bankid.personal_number()
        )
        if not re.fullmatch(r"\d{8}-?\d{4}", resolved_config["ssn"]):
            raise engine.BotError(
                "Personnumret kunde inte hämtas från BankID. Använd reservfältet och försök igen."
            )
        config = engine.Config.from_dict(resolved_config)
        selected_locations = [config.location_id, *config.nearby_location_ids]
        if len(selected_locations) > 4:
            raise engine.BotError("Du kan välja högst fyra provorter.")
        if len(selected_locations) != len(set(selected_locations)):
            raise engine.BotError("Varje provort får bara väljas en gång.")
        with self._lock:
            if (self._thread and self._thread.is_alive()) or self._pending_booking:
                raise RuntimeConflict("En bevakning körs redan för användaren")
            if self._auth_thread and self._auth_thread.is_alive():
                raise RuntimeConflict("Vänta tills BankID-inloggningen är klar.")
            self._validate_catalog_config(config)
            self._stop_event = threading.Event()
            self._pending_done = threading.Event()
            self._set_state("monitor_starting")
            raw_date = resolved_config.get("date_from")
            if raw_date and raw_date != config.date_from:
                self.events.add(
                    "warning",
                    f"Från datum flyttades automatiskt till dagens datum ({config.date_from}).",
                )
            self.state_store.save_config(self.user_id, dataclasses.asdict(config))
            self._thread = threading.Thread(
                target=self._run,
                args=(config,),
                name=f"monitor-{self.user_id}",
                daemon=True,
            )
            self._thread.start()

    def _driver_factory(self) -> Callable[[str], Any] | None:
        if not self.settings.is_server or not self.settings.has_remote_browser:
            return None

        def create_remote_driver(_profile: str) -> Any:
            from selenium import webdriver

            options = webdriver.ChromeOptions()
            options.add_argument("--incognito")
            options.add_argument("--disable-sync")
            options.add_argument("--no-first-run")
            driver = webdriver.Remote(
                command_executor=self.settings.remote_webdriver_url,
                options=options,
            )
            self._remote_view_url = self.settings.remote_browser_view_url.replace(
                "{session_id}", str(driver.session_id)
            )
            self.events.add(
                "authentication",
                "Fjärrwebbläsaren är redo för BankID-inloggning.",
                {"url": self._remote_view_url},
            )
            return driver

        return create_remote_driver

    def _integrated_authentication(self) -> None:
        self._force_browser_fallback.clear()
        self._cancel_authentication.clear()
        self._retry_authentication.clear()
        while True:
            try:
                self._bankid.authenticate(
                    validator=self._client.ensure_authorized,
                    status_callback=self._status,
                    external_cancel=self._stop_event,
                )
                return
            except engine.BotError as exc:
                if self._stop_event.is_set():
                    raise
                self._set_state("action_required", f"BankID-inloggningen behöver åtgärdas: {exc}")
                self.events.add(
                    "warning",
                    f"Den integrerade BankID-inloggningen kunde inte fortsätta: {exc}. "
                    "Försök igen, välj webbläsarfallback eller avbryt.",
                )
                while True:
                    if self._stop_event.is_set() or self._cancel_authentication.is_set():
                        raise
                    if self._force_browser_fallback.is_set():
                        self._browser_authentication()
                        return
                    if self._retry_authentication.wait(0.2):
                        self._retry_authentication.clear()
                        self._set_state("bankid_starting")
                        self.events.add("authentication", "Ett nytt BankID-försök startar.")
                        break

    def _browser_authentication(self) -> None:
        self.events.add(
            "authentication",
            "Den separata säkra webbläsaren används som fallback.",
        )
        self._browser_session = engine.manual_browser_login(
            self._client,
            driver_factory=self._driver_factory(),
            validator=self._client.ensure_authorized,
            status_callback=self._status,
            cancel_event=self._stop_event,
        )
        self._bankid.mark_authenticated()

    def start_authentication(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeConflict("Bevakningen hanterar redan autentisering.")
            if self._auth_thread and self._auth_thread.is_alive():
                raise RuntimeConflict("En BankID-inloggning pågår redan.")
            if self._bankid.snapshot()["authenticated"]:
                return
            self._stop_event = threading.Event()
            self._cancel_authentication = threading.Event()
            self._set_state("bankid_starting")
            self._auth_thread = threading.Thread(
                target=self._run_authentication,
                name=f"bankid-{self.user_id}",
                daemon=True,
            )
            self._auth_thread.start()

    def _run_authentication(self) -> None:
        try:
            self.events.add("authentication", "Mobilt BankID förbereds i kontrollpanelen.")
            self._integrated_authentication()
            self._set_state("bankid_connected")
            self.events.add("authenticated", "BankID-inloggningen är klar.")
            self._initialize_catalog_after_authentication()
        except engine.BotError as exc:
            if self._cancel_authentication.is_set():
                self._set_state("stopped", "BankID-inloggningen avbröts.")
                self.events.add("status", "BankID-inloggningen avbröts.")
            elif not self._stop_event.is_set():
                self._set_state("error", str(exc))
                self.events.add("error", str(exc))

    def cancel_authentication(self) -> None:
        self._set_state("stopping", "BankID-inloggningen avbryts.")
        self._cancel_authentication.set()
        self._bankid.cancel()

    def retry_authentication(self) -> None:
        with self._lock:
            authentication_active = bool(
                (self._auth_thread and self._auth_thread.is_alive())
                or (self._thread and self._thread.is_alive())
            )
        if authentication_active and self._bankid.snapshot()["state"] == "error":
            self._set_state("bankid_starting")
            self._retry_authentication.set()
            return
        self.start_authentication()

    def use_browser_fallback(self) -> None:
        self._set_state("bankid_starting", "Säker webbläsarfallback förbereds.")
        self._force_browser_fallback.set()
        self._bankid.cancel()

    def bankid_qr_svg(self) -> bytes:
        return self._bankid.qr_svg()

    def bankid_uri(self) -> str:
        return self._bankid.bankid_uri()

    def bankid_personal_number(self) -> str:
        return self._bankid.personal_number()

    def _catalog_ssn(self, ssn: str) -> str:
        candidate = ssn.strip() or self._bankid.personal_number()
        if not re.fullmatch(r"\d{8}-?\d{4}", candidate):
            raise engine.BotError(
                "Personnumret kunde inte hämtas från BankID. Använd reservfältet för att hämta alternativen."
            )
        return candidate

    def _initialize_catalog_after_authentication(self) -> None:
        try:
            self.refresh_catalog("", 0)
            self.events.add("catalog", "Tillgängliga behörigheter har hämtats automatiskt.")
        except (engine.BotError, RuntimeConflict) as exc:
            self._set_state("action_required", f"Bokningsalternativen kunde inte hämtas: {exc}")
            self.events.add(
                "warning", f"Bokningsalternativen kunde inte hämtas automatiskt: {exc}"
            )

    @staticmethod
    def _booking_session(ssn: str, licence_id: int) -> dict[str, Any]:
        if not re.fullmatch(r"\d{8}-?\d{4}", ssn):
            raise engine.BotError("Personnumret har ogiltigt format.")
        if licence_id < 0:
            raise engine.BotError("Behörighets-ID får inte vara negativt.")
        return {
            "socialSecurityNumber": ssn,
            "licenceId": licence_id,
            "bookingModeId": 0,
            "ignoreDebt": False,
            "ignoreBookingHindrance": False,
            "examinationTypeId": 0,
            "excludeExaminationCategories": [],
            "rescheduleTypeId": 0,
            "paymentIsActive": False,
            "paymentReference": "",
            "paymentUrl": "",
            "searchedMonths": 0,
        }

    def refresh_catalog(self, ssn: str, licence_id: int = 0) -> dict[str, Any]:
        if not self._bankid.snapshot()["authenticated"] and not self._browser_session:
            raise engine.AuthenticationRequiredError("Logga in med BankID innan katalogen hämtas.")
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeConflict("Bokningsalternativ kan inte uppdateras under bevakning.")
        if not self._catalog_lock.acquire(blocking=False):
            raise RuntimeConflict("Bokningsalternativen uppdateras redan.")
        self._set_state("loading_options")
        try:
            if self._translations is None:
                self._translations = catalog.parse_translations(self._client.language_support())
            try:
                response = (
                    self._client.licence_information()
                    if licence_id == 0
                    else self._client.search_information(
                        self._booking_session(self._catalog_ssn(ssn), licence_id)
                    )
                )
            except engine.AuthenticationRequiredError:
                self._bankid.invalidate()
                raise
            fresh = catalog.parse_booking_catalog(response, self._translations)
            if licence_id == 0 and not fresh.licences:
                raise engine.ApiResponseError("Tjänsten returnerade inga behörigheter.")
            if licence_id > 0 and (not fresh.examination_types or not fresh.locations):
                raise engine.ApiResponseError(
                    "Tjänsten returnerade inte provtyper och orter för vald behörighet."
                )
            with self._lock:
                if self._catalog:
                    licences = {item.id: item for item in self._catalog.licences}
                    licences.update({item.id: item for item in fresh.licences})
                    fresh = catalog.BookingCatalog(
                        tuple(sorted(licences.values(), key=lambda item: item.name.casefold())),
                        fresh.examination_types,
                        fresh.locations,
                        fresh.vehicle_types,
                        fresh.occasion_choices,
                    )
                self._catalog = fresh
                self._catalog_updated_at = time.time()
                self._set_state("ready_to_start")
                return {**fresh.as_dict(), "updatedAt": self._catalog_updated_at}
        except Exception as exc:
            self._set_state("action_required", f"Bokningsalternativen kunde inte hämtas: {exc}")
            raise
        finally:
            self._catalog_lock.release()

    def _validate_catalog_config(self, config: engine.Config) -> None:
        current = self._catalog
        if not current:
            return
        checks = (
            (current.licences, config.licence_id, "behörighet"),
            (current.examination_types, config.examination_type_id, "provtyp"),
            (current.locations, config.location_id, "provort"),
            (current.vehicle_types, config.vehicle_type_id, "fordonstyp"),
            (current.occasion_choices, config.occasion_choice_id, "hyrbilsalternativ"),
        )
        for items, selected, label in checks:
            if items and selected not in {item.id for item in items}:
                raise engine.BotError(
                    f"Vald {label} är inte tillgänglig i den senast hämtade katalogen."
                )
        location_ids = {item.id for item in current.locations}
        if location_ids and any(value not in location_ids for value in config.nearby_location_ids):
            raise engine.BotError(
                "En vald närliggande ort är inte tillgänglig för den valda behörigheten."
            )

    def cached_catalog(self) -> dict[str, Any] | None:
        with self._lock:
            return (
                {**self._catalog.as_dict(), "updatedAt": self._catalog_updated_at}
                if self._catalog
                else None
            )

    def complete_pending_booking(self) -> dict[str, Any]:
        if not self._booking_lock.acquire(blocking=False):
            raise RuntimeConflict("Bokningen behandlas redan.")
        try:
            with self._lock:
                pending = dict(self._pending_booking) if self._pending_booking else None
            if not pending:
                raise RuntimeConflict("Det finns ingen reservation att boka.")
            self._set_state("booking")
            try:
                result = engine.complete_invoice_booking(
                    self._client,
                    pending["config"],
                    pending["booking_session"],
                    pending["bundle_reservation"],
                )
            except engine.BookingConfirmationError as exc:
                with self._lock:
                    self._pending_booking = None
                    self._pending_done.set()
                self._set_state("action_required", str(exc))
                self.events.add(
                    "confirmation_required",
                    "Bokningen fick ett boknings-ID men slutstatus måste kontrolleras manuellt. Betalningen upprepas inte.",
                    {**pending["display"], "booking_id": exc.booking_id},
                )
                raise
            except engine.BotError as exc:
                self._set_state("action_required", f"Bokningen misslyckades: {exc}")
                self.events.add(
                    "booking_error",
                    "Pay later-bokningen misslyckades. Reservationen finns kvar för ett nytt försök.",
                    {**pending["display"], "error": str(exc)},
                )
                raise
            display = {**pending["display"], "booking_id": result["booking_id"]}
            with self._lock:
                self._pending_booking = None
                self._pending_done.set()
            self._set_state("booked")
            self.events.add("booked", "Tiden är bokad med Pay later/faktura.", display)
            engine.notify_discord(
                pending["config"].discord_webhook_url,
                f"✅ [{pending['config'].name}] BOKAT {display.get('date', '')} "
                f"{display.get('time', '')} med Pay later/faktura. "
                f"Boknings-ID: {result['booking_id']}",
            )
            return display
        finally:
            self._booking_lock.release()

    def _status(self, message: str) -> None:
        if message.startswith("Skanna QR-koden"):
            self._set_state("bankid_waiting")
        elif "BankID-inloggningen är klar" in message:
            self._set_state("bankid_connected")
        self.events.add("status", message)

    def _handle_monitor_event(
        self,
        client: engine.TrafikverketClient,
        config: engine.Config,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        public_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
        booking_session = payload.get("_booking_session")
        bundle_reservation = payload.get("_bundle_reservation")
        if (
            kind in {"reserved", "booking_error"}
            and isinstance(booking_session, dict)
            and isinstance(bundle_reservation, dict)
        ):
            with self._lock:
                self._pending_booking = {
                    "config": config,
                    "booking_session": booking_session,
                    "bundle_reservation": bundle_reservation,
                    "display": public_payload,
                }
        if kind in {"reserved", "booking_error"} and self._browser_session:
            try:
                self._browser_session.show_reservation_page(client)
                self.events.add(
                    "browser",
                    "Reservationssidan öppnades i samma autentiserade webbläsare.",
                    {"url": self._remote_view_url},
                )
            except Exception as exc:
                LOGGER.exception("Could not open reservation page")
                self.events.add("warning", f"Reservationssidan kunde inte öppnas: {exc}")
        messages = {
            "match_found": "En matchande tid har hittats.",
            "reserving": "Tiden reserveras hos Trafikverket.",
            "booking": "Tiden bokas med Pay later/faktura.",
            "reservation_failed": "Reservationen misslyckades. Bevakningen fortsätter.",
            "confirmation_required": "Bokningen fick ett boknings-ID men slutstatus måste kontrolleras manuellt.",
            "reserved": "Tiden är reserverad. Slutför bokningen innan reservationen löper ut.",
            "booked": "Tiden är bokad med Pay later/faktura.",
            "booking_error": "Automatisk bokning misslyckades. Reservationen kan slutföras i webbläsaren.",
        }
        state_by_event = {
            "match_found": "match_found",
            "reserving": "reserving",
            "booking": "booking",
            "reservation_failed": "monitoring",
            "reserved": "action_required",
            "booked": "booked",
            "booking_error": "action_required" if self._pending_booking else "error",
            "confirmation_required": "action_required",
        }
        if kind in state_by_event:
            self._set_state(state_by_event[kind], messages.get(kind, kind))
        self.events.add(kind, messages.get(kind, kind), public_payload)

    def _run(self, config: engine.Config) -> None:
        client = self._client
        safe_user = re.sub(r"[^A-Za-z0-9_-]", "_", self.user_id)
        seen_path = self.data_dir / safe_user / "seen.json"
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        terminal_browser_handoff = threading.Event()

        def event_callback(kind: str, payload: dict[str, Any]) -> None:
            self._handle_monitor_event(client, config, kind, payload)
            if kind in {"reserved", "booking_error", "confirmation_required"}:
                terminal_browser_handoff.set()

        try:
            self._set_state("monitoring")
            self.events.add("status", "Bevakningen är aktiv.")
            while not self._stop_event.is_set():
                try:
                    engine.run_monitor(
                        config,
                        client,
                        seen_path,
                        self._stop_event,
                        event_callback=event_callback,
                    )
                    break
                except engine.AuthenticationRequiredError:
                    self._bankid.invalidate()
                    self._set_state("bankid_starting", "BankID behöver anslutas igen.")
                    self.events.add(
                        "authentication",
                        "Mobilt BankID visas nu direkt i kontrollpanelen.",
                    )
                    self._integrated_authentication()
                    if self._stop_event.is_set():
                        return
                    self._set_state("monitoring")
            if terminal_browser_handoff.is_set() and self._pending_booking:
                self._set_state("action_required")
                while not self._stop_event.wait(0.5) and not self._pending_done.is_set():
                    if self._browser_session and not self._browser_session.is_alive():
                        break
        except engine.BotError as exc:
            if not self._stop_event.is_set():
                self.events.add("error", str(exc))
                self._set_state("error", str(exc))
        except Exception:
            LOGGER.exception("Unexpected monitor failure")
            self.events.add("error", "Ett oväntat internt fel stoppade bevakningen.")
            self._set_state("error", "Ett oväntat internt fel stoppade bevakningen.")
        finally:
            if self._browser_session:
                self._browser_session.close()
                self._browser_session = None
                self._remote_view_url = ""
            if self.state not in TERMINAL_STATES:
                if self._stop_event.is_set():
                    self._set_state("stopped")
                    self.events.add("stopped", "Bevakningen har stoppats.")
                else:
                    self._set_state("ready_to_start")

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            auth_thread = self._auth_thread
            active = bool(
                (thread and thread.is_alive()) or (auth_thread and auth_thread.is_alive())
            )
            self._set_state("stopping" if active else "stopped")
        self._stop_event.set()
        self._cancel_authentication.set()
        self._bankid.cancel()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=15)
        if auth_thread and auth_thread.is_alive() and auth_thread is not threading.current_thread():
            auth_thread.join(timeout=15)
        if thread and thread.is_alive():
            self.events.add(
                "warning", "Bevakningen stoppar fortfarande ett pågående nätverksanrop."
            )
        if auth_thread and auth_thread.is_alive():
            self.events.add("warning", "BankID-inloggningen stoppar fortfarande ett nätverksanrop.")
        if not (thread and thread.is_alive()) and not (auth_thread and auth_thread.is_alive()):
            self._set_state("stopped")

    def close(self) -> None:
        self.stop()
        if self._browser_session:
            self._browser_session.close()
            self._browser_session = None
        self._bankid.close()
        self._client.close()


class RuntimeRegistry:
    def __init__(self, settings: AppSettings, state_store: StateStore, data_dir: Path):
        self.settings = settings
        self.state_store = state_store
        self.data_dir = data_dir
        self._jobs: dict[str, MonitorJob] = {}
        self._lock = threading.RLock()

    def for_user(self, user_id: str) -> MonitorJob:
        with self._lock:
            return self._jobs.setdefault(
                user_id,
                MonitorJob(user_id, self.settings, self.state_store, self.data_dir),
            )

    def remove_user(self, user_id: str, *, delete_config: bool = False) -> None:
        with self._lock:
            job = self._jobs.pop(user_id, None)
        if job:
            job.close()
        if delete_config:
            self.state_store.delete_config(user_id)

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            job.close()
        self.state_store.close()
