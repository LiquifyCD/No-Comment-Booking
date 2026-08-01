#!/opt/no-comment-booking/venv/bin/python
import argparse
import time
from pathlib import Path

import requests
import urllib3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument(
        "--credentials", default="/root/frostbyte-app-login.txt"
    )
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--exercise-monitor", action="store_true")
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

    health = requests.get(f"{args.base_url}/api/health", timeout=15, verify=verify)
    health.raise_for_status()
    assert health.json()["status"] == "ok"

    invalid = requests.post(
        f"{args.base_url}/api/auth/login",
        json={"username": credentials["Username"], "password": "invalid"},
        timeout=15,
        verify=verify,
    )
    assert invalid.status_code == 401

    session = requests.Session()
    login = session.post(
        f"{args.base_url}/api/auth/login",
        json={
            "username": credentials["Username"],
            "password": credentials["Password"],
        },
        timeout=15,
        verify=verify,
    )
    assert login.status_code == 204
    cookie = login.headers.get("set-cookie", "")
    assert all(value in cookie for value in ("HttpOnly", "Secure", "SameSite=strict"))

    bootstrap = session.get(
        f"{args.base_url}/api/bootstrap", timeout=15, verify=verify
    )
    bootstrap.raise_for_status()
    bootstrap_data = bootstrap.json()
    assert bootstrap_data["browserFallbackAvailable"] is False
    print("Public health, authentication, secure cookie, and bootstrap: OK")

    if args.exercise_monitor:
        config = bootstrap_data.get("config")
        assert config, "No saved monitor configuration is available"
        csrf_headers = {"X-CSRF-Token": bootstrap_data["csrfToken"]}

        initial_stop = session.post(
            f"{args.base_url}/api/monitor/stop",
            json={},
            headers=csrf_headers,
            timeout=30,
            verify=verify,
        )
        initial_stop.raise_for_status()

        started = session.post(
            f"{args.base_url}/api/monitor/start",
            json=config,
            headers=csrf_headers,
            timeout=30,
            verify=verify,
        )
        started.raise_for_status()
        assert started.json()["status"] == "starting"

        try:
            time.sleep(2)
            polled = session.get(
                f"{args.base_url}/api/events?after=0", timeout=15, verify=verify
            )
            polled.raise_for_status()
            assert "state" in polled.json()
        finally:
            stopped = session.post(
                f"{args.base_url}/api/monitor/stop",
                json={},
                headers=csrf_headers,
                timeout=30,
                verify=verify,
            )
            stopped.raise_for_status()
            assert stopped.json()["status"] == "stopped"

        print("Monitor start, status polling, and stop: OK")


if __name__ == "__main__":
    main()
