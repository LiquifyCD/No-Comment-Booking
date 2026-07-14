from __future__ import annotations

import argparse
import getpass
import os
import sys
import threading
import time
import webbrowser

import uvicorn

from .auth import hash_password
from .settings import SettingsError, load_settings
from .web import create_app


def ensure_windowed_streams() -> None:
    """PyInstaller --windowed sets standard streams to None; Uvicorn expects file objects."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def run() -> int:
    ensure_windowed_streams()
    settings = load_settings()
    server: uvicorn.Server | None = None

    def request_shutdown() -> None:
        if server is not None:
            server.should_exit = True

    app = create_app(settings, request_shutdown)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info" if settings.is_server else "warning",
        access_log=settings.is_server,
    )
    server = uvicorn.Server(config)
    if not settings.is_server:
        launch_url = f"{settings.public_origin}/?token={settings.local_launch_token}"

        def open_when_ready() -> None:
            for _ in range(50):
                if server and server.started:
                    webbrowser.open(launch_url)
                    return
                time.sleep(0.1)

        threading.Thread(target=open_when_ready, daemon=True).start()
    server.run()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Provtidsbevakaren")
    parser.add_argument("--hash-password", action="store_true")
    args = parser.parse_args()
    if args.hash_password:
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation or len(password) < 12:
            parser.error("Passwords must match and contain at least 12 characters")
        print(hash_password(password))
        return 0
    try:
        return run()
    except SettingsError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
