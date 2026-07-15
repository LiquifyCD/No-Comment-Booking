from __future__ import annotations

import io
import threading
import time
from collections.abc import Callable
from typing import Any

import qrcode
import qrcode.image.svg

from . import engine


class BankIdFlow:
    """Keeps BankID challenge secrets in memory and exposes only sanitized state."""

    def __init__(self, client: engine.TrafikverketClient):
        self.client = client
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._state = "idle"
        self._status = ""
        self._error = ""
        self._qr_code = ""
        self._qr_version = 0
        self._expires_at = 0.0
        self._reference_id = ""
        self._qr_start_token = ""
        self._qr_start_time = ""
        self._qr_start_secret = ""
        self._autostart_token = ""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "status": self._status,
                "error": self._error,
                "qrVersion": self._qr_version,
                "expiresAt": self._expires_at,
                "authenticated": self._state == "complete",
                "canOpenOnDevice": bool(self._autostart_token and self._state == "pending"),
            }

    def _clear_challenge(self, *, keep_qr: bool = False) -> None:
        self._reference_id = ""
        self._qr_start_token = ""
        self._qr_start_time = ""
        self._qr_start_secret = ""
        self._autostart_token = ""
        if not keep_qr:
            self._qr_code = ""

    @staticmethod
    def _data(response: dict[str, Any]) -> dict[str, Any]:
        data = response.get("data")
        if not isinstance(data, dict):
            raise engine.ApiResponseError("BankID-svaret saknar ett giltigt data-objekt")
        return data

    def authenticate(
        self,
        *,
        timeout_seconds: float = 180,
        poll_interval: float = 2,
        validator: Callable[[], Any] | None = None,
        status_callback: Callable[[str], None] | None = None,
        external_cancel: threading.Event | None = None,
    ) -> None:
        callback = status_callback or (lambda _message: None)
        with self._lock:
            if self._state == "pending":
                raise engine.BotError("En BankID-inloggning pågår redan.")
            self._cancel = threading.Event()
            self._state = "starting"
            self._status = "Starting"
            self._error = ""
            self._qr_code = ""
            self._qr_version += 1
            self._expires_at = time.time() + timeout_seconds

        try:
            self.client.initialize()
            data = self._data(self.client.begin_authentication())
            required = (
                "referenceId",
                "qrStartToken",
                "qrStartTime",
                "qrStartSecret",
                "qrCode",
            )
            missing = [key for key in required if not data.get(key)]
            if missing:
                raise engine.ApiResponseError(f"BankID-starten saknar fält: {', '.join(missing)}")
            with self._lock:
                self._reference_id = str(data["referenceId"])
                self._qr_start_token = str(data["qrStartToken"])
                self._qr_start_time = str(data["qrStartTime"])
                self._qr_start_secret = str(data["qrStartSecret"])
                self._autostart_token = str(data.get("autostartToken", ""))
                self._qr_code = str(data["qrCode"])
                self._qr_version += 1
                self._state = "pending"
                self._status = "OutstandingTransaction"
            callback("Skanna QR-koden med Mobilt BankID.")

            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if self._cancel.is_set() or (external_cancel and external_cancel.is_set()):
                    raise engine.BotError("BankID-inloggningen avbröts.")
                if self._cancel.wait(poll_interval):
                    raise engine.BotError("BankID-inloggningen avbröts.")
                with self._lock:
                    credentials = {
                        "reference_id": self._reference_id,
                        "qr_start_token": self._qr_start_token,
                        "qr_start_time": self._qr_start_time,
                        "qr_start_secret": self._qr_start_secret,
                    }
                status_data = self._data(self.client.check_authentication_status(**credentials))
                collection_status = str(status_data.get("collectionStatus", ""))
                qr_code = str(status_data.get("qrCode", ""))
                with self._lock:
                    if qr_code and qr_code != self._qr_code:
                        self._qr_code = qr_code
                        self._qr_version += 1
                    self._status = collection_status or self._status
                if collection_status.casefold() == "completed":
                    if validator is not None:
                        validator()
                    else:
                        self.client.ensure_authorized()
                    with self._lock:
                        self._state = "complete"
                        self._status = str(status_data.get("loginStatus") or "Complete")
                        self._clear_challenge(keep_qr=True)
                    callback("BankID-inloggningen är klar.")
                    return
            raise engine.AuthenticationRequiredError("BankID-inloggningen löpte ut.")
        except Exception as exc:
            with self._lock:
                self._state = "cancelled" if self._cancel.is_set() else "error"
                self._error = str(exc)
                self._status = ""
                self._clear_challenge()
            raise

    def cancel(self) -> None:
        with self._lock:
            if self._state in {"starting", "pending"}:
                self._cancel.set()

    def invalidate(self, message: str = "Sessionen har löpt ut.") -> None:
        with self._lock:
            self._cancel.set()
            self._state = "idle"
            self._status = ""
            self._error = message
            self._clear_challenge()

    def mark_authenticated(self) -> None:
        with self._lock:
            self._state = "complete"
            self._status = "Complete"
            self._error = ""
            self._clear_challenge()

    def qr_svg(self) -> bytes:
        with self._lock:
            qr_code = self._qr_code if self._state == "pending" else ""
        if not qr_code:
            raise engine.BotError("Det finns ingen aktiv QR-kod.")
        image = qrcode.make(qr_code, image_factory=qrcode.image.svg.SvgPathImage, border=2)
        output = io.BytesIO()
        image.save(output)
        return output.getvalue()

    def bankid_uri(self) -> str:
        with self._lock:
            token = self._autostart_token if self._state == "pending" else ""
        if not token:
            raise engine.BotError("BankID kan inte öppnas för den aktuella inloggningen.")
        return f"bankid:///?autostarttoken={token}&redirect="

    def close(self) -> None:
        self.cancel()
        with self._lock:
            self._clear_challenge()
            if self._state != "complete":
                self._state = "idle"
