import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from provtidsbevakaren import engine
from provtidsbevakaren.bankid import BankIdFlow
from provtidsbevakaren.catalog import (
    BookingCatalog,
    CatalogItem,
    parse_booking_catalog,
    parse_translations,
    resolve_item_id,
)
from provtidsbevakaren.runtime import MonitorJob
from provtidsbevakaren.settings import load_settings
from provtidsbevakaren.storage import VolatileStateStore


class BankIdFlowTests(unittest.TestCase):
    def test_rotating_qr_completes_without_exposing_challenge_secrets(self):
        client = Mock(spec=engine.TrafikverketClient)
        client.begin_authentication.return_value = {
            "data": {
                "referenceId": "reference-secret",
                "qrStartToken": "qr-token-secret",
                "qrStartTime": "1",
                "qrStartSecret": "qr-start-secret",
                "autostartToken": "autostart-secret",
                "qrCode": "bankid.first",
            }
        }
        complete = threading.Event()

        def status(**_credentials):
            if complete.is_set():
                return {
                    "data": {
                        "collectionStatus": "Completed",
                        "loginStatus": "Success",
                        "qrCode": "bankid.final",
                    }
                }
            return {
                "data": {
                    "collectionStatus": "OutstandingTransaction",
                    "qrCode": "bankid.rotated",
                }
            }

        client.check_authentication_status.side_effect = status
        client.ensure_authorized.return_value = None
        flow = BankIdFlow(client)
        worker = threading.Thread(
            target=lambda: flow.authenticate(timeout_seconds=2, poll_interval=0.01)
        )
        worker.start()
        for _ in range(100):
            if flow.snapshot()["state"] == "pending":
                break
            time.sleep(0.005)
        snapshot = flow.snapshot()
        self.assertEqual("pending", snapshot["state"])
        self.assertNotIn("reference", repr(snapshot).casefold())
        self.assertNotIn("secret", repr(snapshot).casefold())
        self.assertTrue(flow.qr_svg().startswith(b"<?xml"))
        self.assertTrue(flow.bankid_uri().startswith("bankid:///?autostarttoken="))
        complete.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(flow.snapshot()["authenticated"])
        client.ensure_authorized.assert_called_once()

    def test_duplicate_authentication_is_rejected(self):
        client = Mock(spec=engine.TrafikverketClient)
        flow = BankIdFlow(client)
        flow._state = "pending"
        with self.assertRaisesRegex(engine.BotError, "pågår redan"):
            flow.authenticate(timeout_seconds=0)

    def test_expired_challenge_clears_all_secret_state(self):
        client = Mock(spec=engine.TrafikverketClient)
        client.begin_authentication.return_value = {
            "data": {
                "referenceId": "reference-secret",
                "qrStartToken": "qr-token-secret",
                "qrStartTime": "1",
                "qrStartSecret": "qr-start-secret",
                "autostartToken": "autostart-secret",
                "qrCode": "bankid.first",
            }
        }
        client.check_authentication_status.return_value = {
            "data": {"collectionStatus": "OutstandingTransaction", "qrCode": "bankid.next"}
        }
        flow = BankIdFlow(client)
        with self.assertRaisesRegex(engine.AuthenticationRequiredError, "löpte ut"):
            flow.authenticate(timeout_seconds=0.02, poll_interval=0.005)
        self.assertEqual("error", flow.snapshot()["state"])
        self.assertNotIn("secret", repr(flow.snapshot()).casefold())
        with self.assertRaises(engine.BotError):
            flow.qr_svg()

    def test_external_fallback_can_mark_the_same_session_authenticated(self):
        flow = BankIdFlow(Mock(spec=engine.TrafikverketClient))
        flow._state = "error"
        flow._reference_id = "secret"
        flow.mark_authenticated()
        self.assertTrue(flow.snapshot()["authenticated"])
        self.assertFalse(flow.snapshot()["canOpenOnDevice"])


class CatalogTests(unittest.TestCase):
    def test_catalog_is_sorted_deduplicated_and_resolves_names(self):
        response = {
            "data": {
                "licences": [
                    {"licenceId": 23, "licenceName": "B - Personbil"},
                    {"licenceId": 23, "licenceName": "B - Personbil"},
                ],
                "examinationTypes": [
                    {"examinationTypeId": 52, "examinationTypeName": "Körprov"},
                    {"examinationTypeId": 51, "examinationTypeName": "Kunskapsprov"},
                ],
                "locations": [
                    {"locationId": 2, "locationName": "Örebro"},
                    {"locationId": 1, "locationName": "Alingsås"},
                ],
                "nearbyLocations": [{"locationId": 2, "locationName": "Örebro"}],
            }
        }
        parsed = parse_booking_catalog(response)
        self.assertEqual([23], [item.id for item in parsed.licences])
        self.assertEqual(
            ["Kunskapsprov", "Körprov"], [item.name for item in parsed.examination_types]
        )
        self.assertEqual([1, 2], [item.id for item in parsed.locations])
        self.assertEqual(52, resolve_item_id(parsed.examination_types, "körprov"))

    def test_empty_catalog_is_rejected(self):
        with self.assertRaises(engine.ApiResponseError):
            parse_booking_catalog({"data": {}})

    def test_language_resource_keys_become_readable_names(self):
        translations = parse_translations(
            {"data": {"resources": [{"key": "licenceB", "value": "B - Personbil"}]}}
        )
        parsed = parse_booking_catalog(
            {
                "data": {
                    "licenceCategories": [{"licences": [{"id": 23, "languageKeyName": "licenceB"}]}]
                }
            },
            translations,
        )
        self.assertEqual("B - Personbil", parsed.licences[0].name)

    def test_runtime_discovers_licences_then_exam_types_and_all_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            job = MonitorJob("test", load_settings({}), VolatileStateStore(), Path(directory))
            job._bankid._state = "complete"
            job._client.language_support = Mock(
                return_value={
                    "data": {"resources": [{"key": "licenceB", "value": "B - Personbil"}]}
                }
            )
            job._client.licence_information = Mock(
                return_value={
                    "data": {
                        "licenceCategories": [
                            {"licences": [{"id": 23, "languageKeyName": "licenceB"}]}
                        ]
                    }
                }
            )
            initial = job.refresh_catalog("00000000-0000")
            self.assertEqual([23], [item["id"] for item in initial["licences"]])
            job._client.search_information = Mock(
                return_value={
                    "data": {
                        "examinationTypes": [{"id": 52, "name": "Körprov"}],
                        "locations": [
                            {"id": 2, "name": "Örebro"},
                            {"id": 1, "name": "Alingsås"},
                        ],
                    }
                }
            )
            selected = job.refresh_catalog("00000000-0000", 23)
            self.assertEqual([52], [item["id"] for item in selected["examinationTypes"]])
            self.assertEqual(
                ["Alingsås", "Örebro"], [item["name"] for item in selected["locations"]]
            )
            job.close()

    def test_runtime_rejects_a_stale_or_mismatched_catalog_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            job = MonitorJob("test", load_settings({}), VolatileStateStore(), Path(directory))
            job._catalog = BookingCatalog(
                (CatalogItem(5, "B"),),
                (CatalogItem(52, "Körprov"),),
                (CatalogItem(1, "Alingsås"),),
            )
            with self.assertRaisesRegex(engine.BotError, "behörighet"):
                job.start(
                    {
                        "name": "Test",
                        "ssn": "00000000-0000",
                        "licence_id": 23,
                        "examination_type_id": 52,
                        "location_id": 1,
                    }
                )
            job.close()

    def test_retry_signals_the_existing_authentication_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            job = MonitorJob("test", load_settings({}), VolatileStateStore(), Path(directory))
            job._bankid._state = "error"
            job._auth_thread = Mock()
            job._auth_thread.is_alive.return_value = True
            job.retry_authentication()
            self.assertTrue(job._retry_authentication.is_set())
            job._auth_thread.is_alive.return_value = False
            job.close()


class DateAndReservationTests(unittest.TestCase):
    def test_past_start_date_moves_to_local_today(self):
        config = engine.Config.from_dict(
            {
                "name": "Test",
                "ssn": "00000000-0000",
                "licence_id": 23,
                "examination_type_id": 52,
                "location_id": 1,
                "date_from": "2020-01-01",
                "timezone": "UTC",
            }
        )
        self.assertEqual(datetime.now(UTC).date().isoformat(), config.date_from)
        self.assertEqual(
            "2026-07-16",
            engine.effective_date_from(config, datetime(2026, 7, 16, 0, 1, tzinfo=UTC)),
        )

    def test_pending_booking_is_sanitized_and_can_complete_in_app(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VolatileStateStore()
            job = MonitorJob("test", load_settings({}), store, Path(directory))
            config = engine.Config.from_dict(
                {
                    "name": "Test",
                    "ssn": "00000000-0000",
                    "licence_id": 23,
                    "examination_type_id": 52,
                    "location_id": 1,
                    "date_from": datetime.now(UTC).date().isoformat(),
                    "timezone": "UTC",
                }
            )
            job._handle_monitor_event(
                job._client,
                config,
                "reserved",
                {
                    "date": config.date_from,
                    "time": "09:00",
                    "location": "Teststad",
                    "_booking_session": {"secret": "server-only"},
                    "_bundle_reservation": {"secret": "server-only"},
                },
            )
            snapshot = job.snapshot()
            self.assertNotIn("secret", repr(snapshot))
            with patch(
                "provtidsbevakaren.runtime.engine.complete_invoice_booking",
                side_effect=[
                    engine.ApiResponseError("temporary failure"),
                    {"booking_id": "B1"},
                ],
            ):
                with self.assertRaises(engine.ApiResponseError):
                    job.complete_pending_booking()
                self.assertIsNotNone(job.snapshot()["reservation"])
                result = job.complete_pending_booking()
            self.assertEqual("B1", result["booking_id"])
            self.assertIsNone(job.snapshot()["reservation"])
            close = Mock(wraps=job._client.close)
            job._client.close = close
            job.close()
            close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
