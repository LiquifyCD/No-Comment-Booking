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
    def set_monitor_desired(self, user_id: str, active: bool) -> None: ...
    def monitor_desired(self, user_id: str) -> bool: ...
    def set_terminal_state(self, user_id: str, state: str | None) -> None: ...
    def terminal_state(self, user_id: str) -> str | None: ...
    def delete_config(self, user_id: str) -> None: ...
    def close(self) -> None: ...


class VolatileStateStore:
    def __init__(self):
        self._values: dict[str, dict[str, Any]] = {}
        self._monitor_intents: dict[str, bool] = {}
        self._terminal_states: dict[str, str] = {}
        self._lock = threading.RLock()

    def save_config(self, user_id: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._values[user_id] = dict(value)

    def load_config(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._values.get(user_id)
            return dict(value) if value else None

    def set_monitor_desired(self, user_id: str, active: bool) -> None:
        with self._lock:
            self._monitor_intents[user_id] = bool(active)

    def monitor_desired(self, user_id: str) -> bool:
        with self._lock:
            return bool(self._monitor_intents.get(user_id, False))

    def set_terminal_state(self, user_id: str, state: str | None) -> None:
        with self._lock:
            if state:
                self._terminal_states[user_id] = state
            else:
                self._terminal_states.pop(user_id, None)

    def terminal_state(self, user_id: str) -> str | None:
        with self._lock:
            return self._terminal_states.get(user_id)

    def delete_config(self, user_id: str) -> None:
        with self._lock:
            self._values.pop(user_id, None)
            self._monitor_intents.pop(user_id, None)
            self._terminal_states.pop(user_id, None)

    def close(self) -> None:
        with self._lock:
            self._values.clear()
            self._monitor_intents.clear()
            self._terminal_states.clear()


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
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS monitor_intent ("
                "user_id TEXT PRIMARY KEY, desired_active INTEGER NOT NULL DEFAULT 0, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS monitor_result ("
                "user_id TEXT PRIMARY KEY, terminal_state TEXT NOT NULL, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
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

    def set_monitor_desired(self, user_id: str, active: bool) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO monitor_intent(user_id, desired_active) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "desired_active=excluded.desired_active, updated_at=CURRENT_TIMESTAMP",
                (user_id, int(bool(active))),
            )

    def monitor_desired(self, user_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT desired_active FROM monitor_intent WHERE user_id=?", (user_id,)
            ).fetchone()
        return bool(row and row[0])

    def set_terminal_state(self, user_id: str, state: str | None) -> None:
        with self._lock, self._connection:
            if state:
                self._connection.execute(
                    "INSERT INTO monitor_result(user_id, terminal_state) VALUES(?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET terminal_state=excluded.terminal_state, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (user_id, state),
                )
            else:
                self._connection.execute("DELETE FROM monitor_result WHERE user_id=?", (user_id,))

    def terminal_state(self, user_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT terminal_state FROM monitor_result WHERE user_id=?", (user_id,)
            ).fetchone()
        return str(row[0]) if row else None

    def delete_config(self, user_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM user_state WHERE user_id=?", (user_id,))
            self._connection.execute("DELETE FROM monitor_intent WHERE user_id=?", (user_id,))
            self._connection.execute("DELETE FROM monitor_result WHERE user_id=?", (user_id,))

    def close(self) -> None:
        with self._lock:
            self._connection.close()
