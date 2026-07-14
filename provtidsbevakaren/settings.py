from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppSettings:
    mode: Literal["local", "server"] = "local"
    host: str = "127.0.0.1"
    port: int = 8765
    secret_key: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    local_launch_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    public_origin: str = "http://127.0.0.1:8765"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    server_users: dict[str, str] = field(default_factory=dict)
    data_encryption_key: str = ""
    database_path: Path = Path("data/service.db")
    remote_webdriver_url: str = ""
    remote_browser_view_url: str = ""

    @property
    def is_server(self) -> bool:
        return self.mode == "server"


def load_settings(environ: dict[str, str] | None = None) -> AppSettings:
    env = os.environ if environ is None else environ
    mode = env.get("APP_MODE", "local").strip().lower()
    if mode not in {"local", "server"}:
        raise SettingsError("APP_MODE must be local or server")

    if mode == "local":
        port = int(env.get("APP_PORT", "8765"))
        return AppSettings(
            mode="local",
            host="127.0.0.1",
            port=port,
            public_origin=f"http://127.0.0.1:{port}",
        )

    if env.get("ENABLE_SERVER_MODE", "").lower() != "true":
        raise SettingsError(
            "Server mode is disabled. Set ENABLE_SERVER_MODE=true only when the server infrastructure is ready."
        )
    required = (
        "APP_SECRET_KEY",
        "PUBLIC_ORIGIN",
        "SERVER_USERS_JSON",
        "DATA_ENCRYPTION_KEY",
        "REMOTE_WEBDRIVER_URL",
        "REMOTE_BROWSER_VIEW_URL",
    )
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise SettingsError(f"Missing server settings: {', '.join(missing)}")
    if len(env["APP_SECRET_KEY"]) < 32:
        raise SettingsError("APP_SECRET_KEY must contain at least 32 characters")
    if not env["PUBLIC_ORIGIN"].startswith("https://"):
        raise SettingsError("PUBLIC_ORIGIN must use HTTPS in server mode")
    if "{session_id}" not in env["REMOTE_BROWSER_VIEW_URL"]:
        raise SettingsError("REMOTE_BROWSER_VIEW_URL must contain {session_id}")
    try:
        users = json.loads(env["SERVER_USERS_JSON"])
    except json.JSONDecodeError as exc:
        raise SettingsError("SERVER_USERS_JSON is not valid JSON") from exc
    if not isinstance(users, dict) or not users:
        raise SettingsError("SERVER_USERS_JSON must be a non-empty username-to-hash object")
    allowed_hosts = tuple(
        item.strip() for item in env.get("ALLOWED_HOSTS", "").split(",") if item.strip()
    )
    if not allowed_hosts:
        raise SettingsError("ALLOWED_HOSTS is required in server mode")
    return AppSettings(
        mode="server",
        host=env.get("APP_HOST", "0.0.0.0"),
        port=int(env.get("APP_PORT", "8080")),
        secret_key=env["APP_SECRET_KEY"],
        local_launch_token="",
        public_origin=env["PUBLIC_ORIGIN"].rstrip("/"),
        allowed_hosts=allowed_hosts,
        server_users={str(key): str(value) for key, value in users.items()},
        data_encryption_key=env["DATA_ENCRYPTION_KEY"],
        database_path=Path(env.get("DATABASE_PATH", "data/service.db")),
        remote_webdriver_url=env["REMOTE_WEBDRIVER_URL"],
        remote_browser_view_url=env["REMOTE_BROWSER_VIEW_URL"],
    )
