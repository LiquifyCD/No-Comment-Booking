import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from provtidsbevakaren import engine as bot


def config(**overrides):
    values = {
        "name": "Test",
        "ssn": "00000000-0000",
        "licence_id": 23,
        "examination_type_id": 52,
        "location_id": 10,
        "date_from": "2026-08-01",
        "date_to": "2026-08-31",
        "earliest_time": "08:00",
        "latest_time": "17:00",
        "allowed_weekdays": [0, 1, 2, 3, 4],
        "poll_interval_seconds": 10,
    }
    values.update(overrides)
    return bot.Config.from_dict(values)


class ConfigTests(unittest.TestCase):
    def test_rejects_cookie_and_conflicting_booking_modes(self):
        with self.assertRaisesRegex(bot.BotError, "cookie"):
            config(cookie="secret")
        self.assertTrue(config(auto_book=True).auto_book)
        with self.assertRaisesRegex(bot.BotError, "antingen"):
            config(auto_reserve=True, auto_book=True)

    def test_validates_ranges_and_formats(self):
        with self.assertRaisesRegex(bot.BotError, "minst 10"):
            config(poll_interval_seconds=1)
        with self.assertRaisesRegex(bot.BotError, "allowed_weekdays"):
            config(allowed_weekdays=[7])
        with self.assertRaisesRegex(bot.BotError, "date_from"):
            config(date_from="2026/08/01")
        with self.assertRaisesRegex(bot.BotError, "senare"):
            config(earliest_time="18:00", latest_time="17:00")
        with self.assertRaisesRegex(bot.BotError, "ssn"):
            config(ssn="inte-ett-personnummer")
        with self.assertRaisesRegex(bot.BotError, "discord_webhook_url"):
            config(discord_webhook_url="https://example.invalid")


class FilterTests(unittest.TestCase):
    def test_filters_date_time_and_weekday(self):
        cfg = config()
        self.assertTrue(bot.slot_matches_filters({"date": "2026-08-03", "time": "08:00:00"}, cfg))
        self.assertFalse(bot.slot_matches_filters({"date": "2026-08-02", "time": "12:00"}, cfg))
        self.assertFalse(bot.slot_matches_filters({"date": "2026-08-03", "time": "07:59"}, cfg))

    def test_extracts_every_occasion_in_bundle(self):
        result = {
            "data": {
                "bundles": [
                    {
                        "cost": 100,
                        "occasions": [
                            {"locationId": 1, "date": "2026-08-03", "time": "09:00"},
                            {"locationId": 2, "date": "2026-08-04", "time": "10:00"},
                        ],
                    }
                ]
            }
        }
        matches = bot.extract_matching_occasions(result, config())
        self.assertEqual(2, len(matches))
        self.assertEqual([100, 100], [match["cost"] for match in matches])

    def test_rejects_malformed_api_data(self):
        with self.assertRaises(bot.ApiResponseError):
            bot.extract_matching_occasions({"data": {}}, config())


class SeenStateTests(unittest.TestCase):
    def test_atomic_round_trip_and_pruning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            bot.save_seen(path, {"1|2026-08-02|09:00", "1|2026-07-01|09:00"})
            self.assertEqual(2, len(bot.load_seen(path)))
            self.assertEqual(
                {"1|2026-08-02|09:00"},
                bot.prune_seen(bot.load_seen(path), date(2026, 8, 1)),
            )

    def test_corrupt_state_is_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(set(), bot.load_seen(path))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = bot.TrafikverketClient()
        self.client.session.post = Mock()

    def response(self, status=200, payload=None, content_type="application/json"):
        response = Mock(spec=requests.Response)
        response.status_code = status
        response.headers = {"Content-Type": content_type}
        response.json.return_value = {} if payload is None else payload
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError("bad status")
        else:
            response.raise_for_status.return_value = None
        return response

    def test_retains_in_memory_cookie_and_sets_timeout(self):
        self.client.session.cookies.set("unexpected", "value")
        self.client.session.post.return_value = self.response(payload={"data": {}})
        self.assertEqual({"data": {}}, self.client.post("start"))
        self.assertEqual(bot.DEFAULT_TIMEOUT, self.client.session.post.call_args.kwargs["timeout"])
        self.assertEqual(1, len(self.client.session.cookies))

    def test_close_erases_in_memory_cookies(self):
        self.client.session.cookies.set("temporary", "value")
        self.client.close()
        self.assertEqual(0, len(self.client.session.cookies))

    def test_reservation_endpoint_has_no_automatic_retries(self):
        adapter = self.client.session.get_adapter(f"{bot.BASE_URL}/create-reservation")
        self.assertEqual(0, adapter.max_retries.total)
        invoice_adapter = self.client.session.get_adapter(f"{bot.BASE_URL}/invoice-payment")
        self.assertEqual(0, invoice_adapter.max_retries.total)

    def test_auth_and_non_json_are_clear_errors(self):
        self.client.session.post.return_value = self.response(status=401)
        with self.assertRaises(bot.AuthenticationRequiredError):
            self.client.post("start")
        self.client.session.post.return_value = self.response(content_type="text/html")
        with self.assertRaises(bot.AuthenticationRequiredError):
            self.client.post("start")

    def test_api_login_required_envelope_is_clear_error(self):
        payload = {
            "status": 400,
            "data": {"success": False, "message": "Du måste vara inloggad."},
            "type": "LoginRequiredException",
        }
        self.client.session.post.return_value = self.response(payload=payload)
        with self.assertRaisesRegex(bot.AuthenticationRequiredError, "inloggad"):
            self.client.post("occasion-bundles")

    def test_authorization_must_be_explicitly_confirmed(self):
        self.client.session.post.return_value = self.response(payload={"data": False})
        with self.assertRaises(bot.AuthenticationRequiredError):
            self.client.ensure_authorized()
        self.client.session.post.return_value = self.response(payload={"data": True})
        self.client.ensure_authorized()


class BrowserTests(unittest.TestCase):
    def test_manual_login_imports_only_trafikverket_cookies(self):
        client = bot.TrafikverketClient()
        driver = Mock()
        authenticated_cookies = [
            {
                "name": "LoginValid",
                "value": "temporary",
                "domain": ".trafikverket.se",
                "path": "/",
            },
            {
                "name": "other",
                "value": "ignored",
                "domain": ".example.com",
                "path": "/",
            },
        ]
        consent_cookies = [
            {
                "name": "TrvCookieConsent",
                "value": "functional=false",
                "domain": ".trafikverket.se",
                "path": "/",
            }
        ]
        driver.get_cookies.side_effect = [[], consent_cookies, authenticated_cookies]
        validator = Mock(
            side_effect=[
                bot.AuthenticationRequiredError("inte inloggad"),
                {"data": {"bundles": []}},
            ]
        )
        statuses = []
        browser = bot.manual_browser_login(
            client,
            driver_factory=lambda _profile: driver,
            validator=validator,
            status_callback=statuses.append,
            poll_interval=0,
        )
        self.assertEqual(1, browser.imported)
        self.assertEqual("temporary", client.session.cookies.get("LoginValid"))
        driver.get.assert_called_once_with("https://fp.trafikverket.se/Boka/ng/")
        driver.quit.assert_not_called()
        self.assertEqual(2, validator.call_count)
        self.assertTrue(any("klar" in status for status in statuses))
        browser.show_reservation_page(client)
        self.assertEqual(bot.RESERVATION_PAGE_URL, driver.get.call_args_list[-1].args[0])
        driver.add_cookie.assert_called()
        browser.close()
        driver.quit.assert_called_once()
        client.close()


class MonitorTests(unittest.TestCase):
    def test_notifies_new_slot_once(self):
        client = Mock(spec=bot.TrafikverketClient)
        client.booking_hindrances.return_value = {"data": {"canBookLicence": True}}
        client.occasion_bundles.return_value = {
            "data": {
                "bundles": [
                    {
                        "cost": 900,
                        "occasions": [
                            {
                                "locationId": 10,
                                "locationName": "Teststad",
                                "date": "2026-08-03",
                                "time": "09:00",
                            }
                        ],
                    }
                ]
            }
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("provtidsbevakaren.engine.notify_discord") as notify,
        ):
            path = Path(directory) / "seen.json"
            bot.run_monitor(
                config(discord_webhook_url=bot.EXAMPLE_DISCORD_WEBHOOK),
                client,
                path,
                threading.Event(),
                max_polls=2,
                sleep=lambda _: None,
            )
            notify.assert_called_once()
            self.assertEqual(1, len(bot.load_seen(path)))
            self.assertEqual(2, client.occasion_bundles.call_count)

    def test_auto_reserve_reserves_first_match_and_stops(self):
        client = Mock(spec=bot.TrafikverketClient)
        client.booking_hindrances.return_value = {"data": {"canBookLicence": True}}
        client.occasion_bundles.return_value = {
            "data": {
                "bundles": [
                    {
                        "cost": 900,
                        "occasions": [
                            {
                                "locationId": 10,
                                "locationName": "Teststad",
                                "date": "2026-08-03",
                                "time": "09:00",
                            }
                        ],
                    }
                ]
            }
        }
        client.create_reservation.return_value = {"data": {"success": True}}
        client.reservation_information.return_value = {
            "data": {"reservations": [{"date": "2026-08-03", "time": "09:00", "locationId": 10}]}
        }
        events = Mock()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("provtidsbevakaren.engine.notify_discord") as notify,
        ):
            bot.run_monitor(
                config(auto_reserve=True),
                client,
                Path(directory) / "seen.json",
                threading.Event(),
                max_polls=2,
                sleep=lambda _: None,
                event_callback=events,
            )
        client.create_reservation.assert_called_once()
        self.assertEqual("reserved", events.call_args.args[0])
        event_payload = events.call_args.args[1]
        self.assertEqual(
            {"date": "2026-08-03", "time": "09:00", "location": "Teststad"},
            {key: value for key, value in event_payload.items() if not key.startswith("_")},
        )
        self.assertIn("_booking_session", event_payload)
        self.assertIn("_bundle_reservation", event_payload)
        self.assertEqual(1, client.occasion_bundles.call_count)
        self.assertEqual(2, notify.call_count)

    def test_auto_book_uses_invoice_and_emits_popup_event(self):
        client = Mock(spec=bot.TrafikverketClient)
        client.booking_hindrances.return_value = {"data": {"canBookLicence": True}}
        client.occasion_bundles.return_value = {
            "data": {
                "bundles": [
                    {
                        "cost": 900,
                        "occasions": [
                            {
                                "locationId": 10,
                                "locationName": "Teststad",
                                "date": "2026-08-03",
                                "time": "09:00",
                            }
                        ],
                    }
                ]
            }
        }
        client.create_reservation.return_value = {"data": {"success": True}}
        client.reservation_information.return_value = {
            "data": {"reservations": [{"date": "2026-08-03", "time": "09:00", "locationId": 10}]}
        }
        client.invoice_payment.return_value = {"data": {"bookingId": "B1"}}
        client.summary.return_value = {"data": {"confirmedExaminations": []}}
        events = Mock()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("provtidsbevakaren.engine.notify_discord") as notify,
        ):
            bot.run_monitor(
                config(auto_book=True),
                client,
                Path(directory) / "seen.json",
                threading.Event(),
                event_callback=events,
            )
        client.create_reservation.assert_called_once()
        client.invoice_payment.assert_called_once()
        client.summary.assert_called_once_with("00000000-0000", "B1", 23)
        events.assert_called_once_with(
            "booked",
            {"date": "2026-08-03", "time": "09:00", "location": "Teststad", "booking_id": "B1"},
        )
        self.assertEqual(2, notify.call_count)

    def test_auto_book_never_books_when_server_kept_another_reservation(self):
        client = Mock(spec=bot.TrafikverketClient)
        client.booking_hindrances.return_value = {"data": {"canBookLicence": True}}
        client.occasion_bundles.return_value = {
            "data": {
                "bundles": [
                    {
                        "cost": 1000,
                        "occasions": [
                            {
                                "locationId": 10,
                                "locationName": "Teststad",
                                "date": "2026-11-03",
                                "time": "11:25",
                            }
                        ],
                    }
                ]
            }
        }
        client.create_reservation.return_value = {"data": None, "status": 204}
        client.reservation_information.return_value = {
            "data": {"reservations": [{"date": "2026-11-03", "time": "10:50", "locationId": 10}]}
        }
        events = Mock()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("provtidsbevakaren.engine.notify_discord"),
        ):
            bot.run_monitor(
                config(
                    auto_book=True,
                    date_from="2026-11-01",
                    date_to="2026-11-30",
                ),
                client,
                Path(directory) / "seen.json",
                threading.Event(),
                event_callback=events,
            )
        client.invoice_payment.assert_not_called()
        self.assertEqual("booking_error", events.call_args.args[0])
        self.assertIn("10:50", events.call_args.args[1]["error"])


if __name__ == "__main__":
    unittest.main()
