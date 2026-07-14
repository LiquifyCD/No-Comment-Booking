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

from . import engine
from .settings import AppSettings
from .storage import StateStore

LOGGER = logging.getLogger("provtidsbevakaren.runtime")


class RuntimeConflict(RuntimeError):
    pass


class EventBuffer:
    def __init__(self, max_events: int = 500):
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
        self._state = "idle"
        self._browser_session: engine.BrowserLoginSession | None = None
        self._remote_view_url = ""

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def snapshot(self, after: int = 0) -> dict[str, Any]:
        return {
            "state": self.state,
            "events": self.events.after(after),
            "browserViewUrl": self._remote_view_url,
        }

    def start(self, raw_config: dict[str, Any]) -> None:
        config = engine.Config.from_dict(raw_config)
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeConflict("En bevakning körs redan för användaren")
            self._stop_event = threading.Event()
            self._state = "starting"
            self.state_store.save_config(self.user_id, dataclasses.asdict(config))
            self._thread = threading.Thread(
                target=self._run,
                args=(config,),
                name=f"monitor-{self.user_id}",
                daemon=True,
            )
            self._thread.start()

    def _driver_factory(self) -> Callable[[str], Any] | None:
        if not self.settings.is_server:
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

    def _status(self, message: str) -> None:
        self.events.add("status", message)

    def _handle_monitor_event(
        self,
        client: engine.TrafikverketClient,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
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
            "reserved": "Tiden är reserverad. Slutför bokningen innan reservationen löper ut.",
            "booked": "Tiden är bokad med Pay later/faktura.",
            "booking_error": "Automatisk bokning misslyckades. Reservationen kan slutföras i webbläsaren.",
        }
        self.events.add(kind, messages.get(kind, kind), payload)

    def _run(self, config: engine.Config) -> None:
        client = engine.TrafikverketClient()
        safe_user = re.sub(r"[^A-Za-z0-9_-]", "_", self.user_id)
        seen_path = self.data_dir / safe_user / "seen.json"
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        terminal_browser_handoff = threading.Event()

        def event_callback(kind: str, payload: dict[str, Any]) -> None:
            self._handle_monitor_event(client, kind, payload)
            if kind in {"reserved", "booking_error"}:
                terminal_browser_handoff.set()

        try:
            with self._lock:
                self._state = "running"
            self.events.add("status", "Bevakningen startar.")
            try:
                engine.run_monitor(
                    config,
                    client,
                    seen_path,
                    self._stop_event,
                    event_callback=event_callback,
                )
            except engine.AuthenticationRequiredError:
                with self._lock:
                    self._state = "authentication"
                if self.settings.is_server:
                    self.events.add(
                        "authentication",
                        "En isolerad fjärrwebbläsare förbereds.",
                    )
                else:
                    self.events.add(
                        "authentication",
                        "Ett privat webbläsarfönster öppnas för BankID-inloggning.",
                    )
                booking_session = engine.build_booking_session(config)
                self._browser_session = engine.manual_browser_login(
                    client,
                    driver_factory=self._driver_factory(),
                    validator=lambda: client.occasion_bundles(
                        booking_session, engine.build_occasion_query(config)
                    ),
                    status_callback=self._status,
                    cancel_event=self._stop_event,
                )
                if self._stop_event.is_set():
                    return
                with self._lock:
                    self._state = "running"
                engine.run_monitor(
                    config,
                    client,
                    seen_path,
                    self._stop_event,
                    event_callback=event_callback,
                )
            if terminal_browser_handoff.is_set() and self._browser_session:
                with self._lock:
                    self._state = "action_required"
                while not self._stop_event.wait(0.5) and self._browser_session.is_alive():
                    pass
        except engine.BotError as exc:
            if not self._stop_event.is_set():
                self.events.add("error", str(exc))
                with self._lock:
                    self._state = "error"
        except Exception:
            LOGGER.exception("Unexpected monitor failure")
            self.events.add("error", "Ett oväntat internt fel stoppade bevakningen.")
            with self._lock:
                self._state = "error"
        finally:
            if self._browser_session:
                self._browser_session.close()
                self._browser_session = None
                self._remote_view_url = ""
            client.close()
            with self._lock:
                if self._state not in {"error"}:
                    self._state = "idle"
            self.events.add("stopped", "Bevakningen har stoppats.")

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._state = "stopping" if thread and thread.is_alive() else "idle"
        self._stop_event.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=15)
        if thread and thread.is_alive():
            self.events.add(
                "warning", "Bevakningen stoppar fortfarande ett pågående nätverksanrop."
            )


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
            job.stop()
        if delete_config:
            self.state_store.delete_config(user_id)

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            job.stop()
        self.state_store.close()
