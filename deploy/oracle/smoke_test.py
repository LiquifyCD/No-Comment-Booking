#!/opt/no-comment-booking/venv/bin/python
import argparse
import secrets
import sqlite3
from pathlib import Path

import requests
import urllib3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--credentials", default="/root/frostbyte-app-login.txt")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--exercise-monitor", action="store_true")
    parser.add_argument("--exercise-accounts", action="store_true")
    parser.add_argument("--database-path", default="/var/lib/no-comment-booking/data/service.db")
    args = parser.parse_args()

    credentials = dict(
        line.split(": ", 1)
        for line in Path(args.credentials).read_text().splitlines()
        if ": " in line
    )
    verify = not args.insecure
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    root = requests.get(args.base_url, timeout=15, verify=verify)
    assert root.status_code == 200
    assert 'class="sidebar"' not in root.text
    assert 'class="brand-mark"' not in root.text
    assert 'data-step="bankid"' in root.text

    health = requests.get(f"{args.base_url}/api/health", timeout=15, verify=verify)
    health.raise_for_status()
    assert health.json()["status"] == "ok"

    invalid = requests.post(
        f"{args.base_url}/api/auth/login",
        json={"email": credentials["Email"], "password": "invalid"},
        timeout=15,
        verify=verify,
    )
    assert invalid.status_code == 401

    session = requests.Session()
    login = session.post(
        f"{args.base_url}/api/auth/login",
        json={
            "email": credentials["Email"],
            "password": credentials["Password"],
        },
        timeout=15,
        verify=verify,
    )
    assert login.status_code == 204
    cookie = login.headers.get("set-cookie", "")
    assert all(value in cookie for value in ("HttpOnly", "Secure", "SameSite=strict"))

    bootstrap = session.get(f"{args.base_url}/api/bootstrap", timeout=15, verify=verify)
    bootstrap.raise_for_status()
    bootstrap_data = bootstrap.json()
    assert bootstrap_data["browserFallbackAvailable"] is False
    with session.get(
        f"{args.base_url}/api/live/stream?after=0",
        timeout=(15, 15),
        verify=verify,
        stream=True,
    ) as stream:
        stream.raise_for_status()
        event = b""
        for chunk in stream.iter_content(chunk_size=1):
            event += chunk
            if b"\n\n" in event:
                break
        assert b"data: " in event
    print("Public health, authentication, secure cookie, and bootstrap: OK")

    if args.exercise_accounts:
        assert bootstrap_data["isAdmin"] is True
        email = f"smoke-{secrets.token_hex(5)}@example.com"
        password = f"Smoke-{secrets.token_urlsafe(18)}"
        registered = False
        account_id = None
        try:
            registration = requests.post(
                f"{args.base_url}/api/auth/register",
                json={"email": email, "password": password},
                timeout=30,
                verify=verify,
            )
            registration.raise_for_status()
            registered = True
            assert registration.json()["status"] == "pending"

            pending = requests.post(
                f"{args.base_url}/api/auth/login",
                json={"email": email, "password": password},
                timeout=30,
                verify=verify,
            )
            assert pending.status_code == 403

            csrf_headers = {"X-CSRF-Token": bootstrap_data["csrfToken"]}
            account_id = registration.json()["id"]
            approved = session.patch(
                f"{args.base_url}/api/admin/users/{account_id}",
                json={"status": "active"},
                headers=csrf_headers,
                timeout=30,
                verify=verify,
            )
            approved.raise_for_status()

            user_session = requests.Session()
            user_login = user_session.post(
                f"{args.base_url}/api/auth/login",
                json={"email": email, "password": password},
                timeout=30,
                verify=verify,
            )
            user_login.raise_for_status()
            user_bootstrap = user_session.get(
                f"{args.base_url}/api/bootstrap", timeout=15, verify=verify
            )
            user_bootstrap.raise_for_status()
            user_data = user_bootstrap.json()
            assert user_data["isAdmin"] is False
            assert user_data["discordAllowed"] is False
            assert "events" not in user_data
            assert (
                user_session.get(
                    f"{args.base_url}/api/status", timeout=15, verify=verify
                ).status_code
                == 200
            )
            assert (
                user_session.get(
                    f"{args.base_url}/api/live", timeout=15, verify=verify
                ).status_code
                == 403
            )
            discord_allowed = session.patch(
                f"{args.base_url}/api/admin/users/{account_id}",
                json={"discord_allowed": True},
                headers=csrf_headers,
                timeout=30,
                verify=verify,
            )
            discord_allowed.raise_for_status()
            assert discord_allowed.json()["discordAllowed"] is True

            disabled = session.patch(
                f"{args.base_url}/api/admin/users/{account_id}",
                json={"status": "disabled"},
                headers=csrf_headers,
                timeout=30,
                verify=verify,
            )
            disabled.raise_for_status()
            assert (
                user_session.get(
                    f"{args.base_url}/api/bootstrap", timeout=15, verify=verify
                ).status_code
                == 401
            )
        finally:
            if registered and account_id:
                with sqlite3.connect(args.database_path) as database:
                    database.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        print("Registration, pending gate, admin approval, and disable: OK")

    if args.exercise_monitor:
        assert "name" not in (bootstrap_data.get("config") or {})
        assert "ssn" not in (bootstrap_data.get("config") or {})
        live = session.get(f"{args.base_url}/api/live?after=0", timeout=15, verify=verify)
        live.raise_for_status()
        assert "state" in live.json()
        assert (
            session.post(
                f"{args.base_url}/api/reservation/book",
                json={},
                headers={"X-CSRF-Token": bootstrap_data["csrfToken"]},
                timeout=15,
                verify=verify,
            ).status_code
            == 404
        )
        print("Authenticated live status and PII redaction: OK")


if __name__ == "__main__":
    main()
