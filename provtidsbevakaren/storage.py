from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken


class StateStore(Protocol):
    def save_config(self, user_id: str, value: dict[str, Any]) -> None: ...
    def load_config(self, user_id: str) -> dict[str, Any] | None: ...
    def delete_config(self, user_id: str) -> None: ...
    def close(self) -> None: ...


class VolatileStateStore:
    def __init__(self):
        self._values: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def save_config(self, user_id: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._values[user_id] = dict(value)

    def load_config(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._values.get(user_id)
            return dict(value) if value else None

    def delete_config(self, user_id: str) -> None:
        with self._lock:
            self._values.pop(user_id, None)

    def close(self) -> None:
        with self._lock:
            self._values.clear()


class EncryptedSqliteStateStore:
    """Persists configuration encrypted at rest; browser cookies are never stored."""

    def __init__(self, path: Path, encryption_key: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._cipher = Fernet(encryption_key.encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("DATA_ENCRYPTION_KEY must be a valid Fernet key") from exc
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS user_state ("
                "user_id TEXT PRIMARY KEY, encrypted_config BLOB NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

    def save_config(self, user_id: str, value: dict[str, Any]) -> None:
        encrypted = self._cipher.encrypt(json.dumps(value, separators=(",", ":")).encode())
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO user_state(user_id, encrypted_config) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET encrypted_config=excluded.encrypted_config, updated_at=CURRENT_TIMESTAMP",
                (user_id, encrypted),
            )

    def load_config(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT encrypted_config FROM user_state WHERE user_id=?", (user_id,)
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(self._cipher.decrypt(row[0]))
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise RuntimeError("Stored configuration could not be decrypted") from exc
        return value if isinstance(value, dict) else None

    def delete_config(self, user_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM user_state WHERE user_id=?", (user_id,))

    def close(self) -> None:
        with self._lock:
            self._connection.close()
