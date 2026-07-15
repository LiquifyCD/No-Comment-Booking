import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from provtidsbevakaren.auth import hash_password
from provtidsbevakaren.settings import load_settings
from provtidsbevakaren.web import create_app

VALID_CONFIG = {
    "name": "Test",
    "ssn": "00000000-0000",
    "licence_id": 23,
    "examination_type_id": 52,
    "location_id": 10,
    "nearby_location_ids": [],
    "date_from": (date.today() + timedelta(days=1)).isoformat(),
    "date_to": (date.today() + timedelta(days=31)).isoformat(),
    "earliest_time": "08:00",
    "latest_time": "17:00",
    "allowed_weekdays": [0, 1, 2, 3, 4],
    "poll_interval_seconds": 60,
    "discord_webhook_url": "",
    "auto_reserve": False,
    "auto_book": False,
}


class LocalWebTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings({})
        self.app = create_app(self.settings)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")

    def tearDown(self):
        self.client.close()

    def login(self):
        response = self.client.get(
            f"/?token={self.settings.local_launch_token}", follow_redirects=False
        )
        self.assertEqual(303, response.status_code)
        bootstrap = self.client.get("/api/bootstrap")
        self.assertEqual(200, bootstrap.status_code)
        return bootstrap.json()

    def test_local_token_is_exchanged_for_httponly_cookie(self):
        response = self.client.get(
            f"/?token={self.settings.local_launch_token}", follow_redirects=False
        )
        self.assertEqual(303, response.status_code)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn(self.settings.local_launch_token, cookie)

    def test_mutating_endpoint_requires_csrf(self):
        bootstrap = self.login()
        self.assertEqual(403, self.client.post("/api/monitor/start", json=VALID_CONFIG).status_code)
        with patch("provtidsbevakaren.runtime.MonitorJob.start") as start:
            response = self.client.post(
                "/api/monitor/start",
                json=VALID_CONFIG,
                headers={"X-CSRF-Token": bootstrap["csrfToken"]},
            )
        self.assertEqual(200, response.status_code)
        start.assert_called_once()

    def test_health_and_security_headers(self):
        response = self.client.get("/api/health")
        self.assertEqual({"status": "ok", "mode": "local", "version": "2.1.0"}, response.json())
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual("no-store", response.headers["cache-control"])

    def test_bankid_and_catalog_endpoints_keep_sensitive_input_in_request_body(self):
        bootstrap = self.login()
        headers = {"X-CSRF-Token": bootstrap["csrfToken"]}
        job = self.app.state.registry.for_user("local")
        with patch.object(job, "start_authentication") as start:
            response = self.client.post("/api/bankid/start", json={}, headers=headers)
        self.assertEqual(200, response.status_code)
        start.assert_called_once()
        with patch.object(job, "bankid_qr_svg", return_value=b"<svg/>"):
            response = self.client.get("/api/bankid/qr.svg")
        self.assertEqual("image/svg+xml", response.headers["content-type"])
        with patch.object(
            job,
            "refresh_catalog",
            return_value={"licences": [], "examinationTypes": [], "locations": []},
        ) as refresh:
            response = self.client.post(
                "/api/catalog/refresh",
                json={"ssn": "00000000-0000", "licence_id": 23},
                headers=headers,
            )
        self.assertEqual(200, response.status_code)
        self.assertNotIn("00000000", str(response.request.url))
        refresh.assert_called_once_with("00000000-0000", 23)


if __name__ == "__main__":
    unittest.main()


class ServerWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = load_settings(
            {
                "APP_MODE": "server",
                "ENABLE_SERVER_MODE": "true",
                "APP_SECRET_KEY": "s" * 48,
                "PUBLIC_ORIGIN": "https://service.example",
                "ALLOWED_HOSTS": "service.example",
                "SERVER_USERS_JSON": json.dumps(
                    {
                        "alice": hash_password("alice-password", iterations=10_000),
                        "bob": hash_password("bob-password", iterations=10_000),
                    }
                ),
                "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "DATABASE_PATH": str(Path(self.temp.name) / "service.db"),
                "REMOTE_WEBDRIVER_URL": "http://browser:4444/wd/hub",
                "REMOTE_BROWSER_VIEW_URL": "https://viewer.example/session/{session_id}",
            }
        )
        self.app = create_app(self.settings)
        self.client = TestClient(self.app, base_url="https://service.example")

    def tearDown(self):
        self.client.close()
        self.app.state.registry.shutdown()
        self.temp.cleanup()

    def login(self, username="alice", password="alice-password"):
        return self.client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )

    def test_login_uses_secure_cookie_and_generic_failure(self):
        self.assertEqual(401, self.login(password="wrong").status_code)
        response = self.login()
        self.assertEqual(204, response.status_code)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)

    def test_users_have_isolated_jobs_and_events(self):
        self.login()
        alice = self.client.get("/api/bootstrap").json()
        self.app.state.registry.for_user("alice").events.add("status", "alice-only")
        self.client.cookies.clear()
        self.login("bob", "bob-password")
        bob = self.client.get("/api/bootstrap").json()
        self.assertEqual("alice", alice["user"])
        self.assertEqual("bob", bob["user"])
        self.assertFalse(any(event["message"] == "alice-only" for event in bob["events"]))

    def test_server_cannot_exit_process_through_api(self):
        self.login()
        bootstrap = self.client.get("/api/bootstrap").json()
        response = self.client.post(
            "/api/app/exit", json={}, headers={"X-CSRF-Token": bootstrap["csrfToken"]}
        )
        self.assertEqual(404, response.status_code)
