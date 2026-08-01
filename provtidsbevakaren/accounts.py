from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")


class AccountError(RuntimeError):
    pass


class UsernameUnavailable(AccountError):
    pass


class PendingAccountLimit(AccountError):
    pass


class AccountChangeRejected(AccountError):
    pass


@dataclass(frozen=True)
class Account:
    username: str
    password_hash: str
    role: str
    status: str
    paid: bool
    access_source: str
    created_at: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def public_dict(self) -> dict[str, str | bool]:
        return {
            "username": self.username,
            "role": self.role,
            "status": self.status,
            "paid": self.paid,
            "accessSource": self.access_source,
            "createdAt": self.created_at,
        }


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "Användarnamnet måste vara 3–32 tecken och får bara innehålla a–z, 0–9, punkt, bindestreck och understreck."
        )
    return username


class SqliteAccountStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS accounts ("
                "username TEXT PRIMARY KEY, "
                "password_hash TEXT NOT NULL, "
                "role TEXT NOT NULL CHECK(role IN ('admin','user')), "
                "status TEXT NOT NULL CHECK(status IN ('pending','active','disabled')), "
                "paid INTEGER NOT NULL DEFAULT 0, "
                "access_source TEXT NOT NULL DEFAULT 'pending', "
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

    @staticmethod
    def _account(row: sqlite3.Row | None) -> Account | None:
        if row is None:
            return None
        return Account(
            username=str(row["username"]),
            password_hash=str(row["password_hash"]),
            role=str(row["role"]),
            status=str(row["status"]),
            paid=bool(row["paid"]),
            access_source=str(row["access_source"]),
            created_at=str(row["created_at"]),
        )

    def seed(self, users: dict[str, str], admin_users: tuple[str, ...]) -> None:
        admins = {normalize_username(value) for value in admin_users}
        with self._lock, self._connection:
            for raw_username, password_hash in users.items():
                username = normalize_username(raw_username)
                role = "admin" if username in admins else "user"
                self._connection.execute(
                    "INSERT INTO accounts(username,password_hash,role,status,paid,access_source) "
                    "VALUES(?,?,?,'active',1,'legacy') ON CONFLICT(username) DO NOTHING",
                    (username, password_hash, role),
                )
                self._connection.execute(
                    "UPDATE accounts SET password_hash=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE username=? AND access_source IN ('legacy','admin')",
                    (password_hash, username),
                )
                if username in admins:
                    self._connection.execute(
                        "UPDATE accounts SET role='admin',status='active',access_source='admin',updated_at=CURRENT_TIMESTAMP "
                        "WHERE username=?",
                        (username,),
                    )

    def get(self, username: str) -> Account | None:
        try:
            normalized = normalize_username(username)
        except ValueError:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT username,password_hash,role,status,paid,access_source,created_at "
                "FROM accounts WHERE username=?",
                (normalized,),
            ).fetchone()
        return self._account(row)

    def register(self, username: str, password_hash: str) -> Account:
        normalized = normalize_username(username)
        with self._lock, self._connection:
            pending_count = self._connection.execute(
                "SELECT COUNT(*) FROM accounts WHERE status='pending'"
            ).fetchone()[0]
            if int(pending_count) >= 100:
                raise PendingAccountLimit("För många konton väntar på granskning.")
            try:
                self._connection.execute(
                    "INSERT INTO accounts(username,password_hash,role,status,paid,access_source) "
                    "VALUES(?,?,'user','pending',0,'pending')",
                    (normalized, password_hash),
                )
            except sqlite3.IntegrityError as exc:
                raise UsernameUnavailable("Användarnamnet är inte tillgängligt.") from exc
        account = self.get(normalized)
        if account is None:
            raise RuntimeError("Kontot kunde inte läsas efter registrering.")
        return account

    def list_accounts(self) -> list[Account]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT username,password_hash,role,status,paid,access_source,created_at "
                "FROM accounts ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at"
            ).fetchall()
        return [account for row in rows if (account := self._account(row)) is not None]

    def approve(self, username: str, *, paid: bool) -> Account:
        normalized = normalize_username(username)
        source = "payment" if paid else "admin"
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE accounts SET status='active',paid=?,access_source=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE username=? AND role='user'",
                (1 if paid else 0, source, normalized),
            )
            if cursor.rowcount != 1:
                raise AccountChangeRejected("Kontot kunde inte godkännas.")
        account = self.get(normalized)
        if account is None:
            raise RuntimeError("Kontot kunde inte läsas efter godkännande.")
        return account

    def disable(self, username: str) -> Account:
        normalized = normalize_username(username)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE accounts SET status='disabled',access_source='admin',updated_at=CURRENT_TIMESTAMP "
                "WHERE username=? AND role='user'",
                (normalized,),
            )
            if cursor.rowcount != 1:
                raise AccountChangeRejected("Administratörskonton kan inte stängas av här.")
        account = self.get(normalized)
        if account is None:
            raise RuntimeError("Kontot kunde inte läsas efter avstängning.")
        return account

    def close(self) -> None:
        with self._lock:
            self._connection.close()
