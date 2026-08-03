from __future__ import annotations

import json
import os
import re
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
    server_accounts: dict[str, str] = field(default_factory=dict)
    admin_emails: tuple[str, ...] = ()
    legacy_email_map: dict[str, str] = field(default_factory=dict)
    data_encryption_key: str = ""
    database_path: Path = Path("data/service.db")
    remote_webdriver_url: str = ""
    remote_browser_view_url: str = ""

    @property
    def is_server(self) -> bool:
        return self.mode == "server"

    @property
    def has_remote_browser(self) -> bool:
        return bool(self.remote_webdriver_url and self.remote_browser_view_url)


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
        "DATA_ENCRYPTION_KEY",
    )
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise SettingsError(f"Missing server settings: {', '.join(missing)}")
    if len(env["APP_SECRET_KEY"]) < 32:
        raise SettingsError("APP_SECRET_KEY must contain at least 32 characters")
    if not env["PUBLIC_ORIGIN"].startswith("https://"):
        raise SettingsError("PUBLIC_ORIGIN must use HTTPS in server mode")
    remote_webdriver_url = env.get("REMOTE_WEBDRIVER_URL", "").strip()
    remote_browser_view_url = env.get("REMOTE_BROWSER_VIEW_URL", "").strip()
    if bool(remote_webdriver_url) != bool(remote_browser_view_url):
        raise SettingsError(
            "REMOTE_WEBDRIVER_URL and REMOTE_BROWSER_VIEW_URL must be configured together"
        )
    if remote_browser_view_url and "{session_id}" not in remote_browser_view_url:
        raise SettingsError("REMOTE_BROWSER_VIEW_URL must contain {session_id}")

    def json_map(name: str, *, required: bool = False) -> dict[str, str]:
        raw = env.get(name, "")
        if not raw:
            if required:
                raise SettingsError(f"Missing server settings: {name}")
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SettingsError(f"{name} is not valid JSON") from exc
        if not isinstance(value, dict) or (required and not value):
            raise SettingsError(f"{name} must be a non-empty JSON object")
        return {str(key).strip().casefold(): str(item) for key, item in value.items()}

    def email(value: str) -> str:
        normalized = value.strip().casefold()
        if len(normalized) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
            raise SettingsError("Account configuration contains an invalid email address")
        return normalized

    legacy_email_map = {
        username: email(address)
        for username, address in json_map("ACCOUNT_EMAIL_MIGRATION_JSON").items()
    }
    if env.get("SERVER_ACCOUNTS_JSON"):
        server_accounts = {
            email(address): password_hash
            for address, password_hash in json_map("SERVER_ACCOUNTS_JSON", required=True).items()
        }
        admin_emails = tuple(
            email(item) for item in env.get("ADMIN_EMAILS", "").split(",") if item.strip()
        )
    else:
        legacy_users = json_map("SERVER_USERS_JSON", required=True)
        missing_mappings = [name for name in legacy_users if name not in legacy_email_map]
        if missing_mappings:
            raise SettingsError(
                "ACCOUNT_EMAIL_MIGRATION_JSON must map every SERVER_USERS_JSON account to email"
            )
        server_accounts = {
            legacy_email_map[username]: password_hash
            for username, password_hash in legacy_users.items()
        }
        legacy_admins = tuple(
            item.strip().casefold()
            for item in env.get("ADMIN_USERS", "").split(",")
            if item.strip()
        ) or (next(iter(legacy_users)),)
        unknown_admins = [name for name in legacy_admins if name not in legacy_users]
        if unknown_admins:
            raise SettingsError("ADMIN_USERS must reference accounts in SERVER_USERS_JSON")
        admin_emails = tuple(legacy_email_map[name] for name in legacy_admins)
    if not admin_emails:
        admin_emails = (next(iter(server_accounts)),)
    if any(address not in server_accounts for address in admin_emails):
        raise SettingsError("ADMIN_EMAILS must reference accounts in SERVER_ACCOUNTS_JSON")
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
        server_accounts=server_accounts,
        admin_emails=admin_emails,
        legacy_email_map=legacy_email_map,
        data_encryption_key=env["DATA_ENCRYPTION_KEY"],
        database_path=Path(env.get("DATABASE_PATH", "data/service.db")),
        remote_webdriver_url=remote_webdriver_url,
        remote_browser_view_url=remote_browser_view_url,
    )
