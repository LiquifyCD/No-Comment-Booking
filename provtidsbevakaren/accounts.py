from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


class AccountError(RuntimeError):
    pass


class EmailUnavailable(AccountError):
    pass


class PendingAccountLimit(AccountError):
    pass


class AccountChangeRejected(AccountError):
    pass


class AccountMigrationRequired(AccountError):
    pass


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    password_hash: str
    role: str
    status: str
    paid: bool
    access_source: str
    display_name: str
    created_at: str
    reset_expires_at: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def public_dict(self) -> dict[str, str | bool]:
        return {
            "id": self.id,
            "email": self.email,
            "displayName": self.display_name,
            "role": self.role,
            "status": self.status,
            "paid": self.paid,
            "accessSource": self.access_source,
            "createdAt": self.created_at,
            "passwordResetPending": bool(
                self.reset_expires_at and self.reset_expires_at > int(time.time())
            ),
        }


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Ange en giltig e-postadress.")
    return email


class SqliteAccountStore:
    def __init__(self, path: Path, legacy_email_map: dict[str, str] | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self._ensure_schema(legacy_email_map or {})
        except Exception:
            self._connection.close()
            raise

    def _ensure_schema(self, legacy_email_map: dict[str, str]) -> None:
        with self._lock:
            exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
            ).fetchone()
            if not exists:
                with self._connection:
                    self._create_schema()
                return
            columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(accounts)").fetchall()
            }
            if {"id", "email"}.issubset(columns):
                return
            if "username" not in columns:
                raise AccountMigrationRequired("Kontodatabasens format känns inte igen.")
            self._migrate_legacy_schema(legacy_email_map)

    def _create_schema(self) -> None:
        self._connection.execute(
            "CREATE TABLE accounts ("
            "id TEXT PRIMARY KEY, "
            "email TEXT NOT NULL COLLATE NOCASE UNIQUE, "
            "password_hash TEXT NOT NULL, "
            "role TEXT NOT NULL CHECK(role IN ('admin','user')), "
            "status TEXT NOT NULL CHECK(status IN ('pending','active','disabled')), "
            "paid INTEGER NOT NULL DEFAULT 0, "
            "access_source TEXT NOT NULL DEFAULT 'pending', "
            "display_name TEXT NOT NULL DEFAULT '', "
            "reset_token_hash TEXT UNIQUE, "
            "reset_expires_at INTEGER, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS accounts_status_created_idx ON accounts(status,created_at)"
        )

    def _migrate_legacy_schema(self, legacy_email_map: dict[str, str]) -> None:
        rows = self._connection.execute(
            "SELECT username,password_hash,role,status,paid,access_source,created_at,updated_at "
            "FROM accounts"
        ).fetchall()
        normalized_map = {
            str(username).strip().casefold(): normalize_email(email)
            for username, email in legacy_email_map.items()
        }
        missing = [
            row for row in rows if str(row["username"]).strip().casefold() not in normalized_map
        ]
        if missing:
            raise AccountMigrationRequired(
                f"E-postmappning saknas för {len(missing)} befintliga konto(n). "
                "Ange ACCOUNT_EMAIL_MIGRATION_JSON innan uppgradering."
            )
        mapped_emails = [normalized_map[str(row["username"]).strip().casefold()] for row in rows]
        if len(mapped_emails) != len(set(mapped_emails)):
            raise AccountMigrationRequired(
                "ACCOUNT_EMAIL_MIGRATION_JSON innehåller duplicerade e-postadresser."
            )
        try:
            with self._connection:
                self._connection.execute("ALTER TABLE accounts RENAME TO accounts_legacy")
                self._create_schema()
                for row in rows:
                    legacy_id = str(row["username"])
                    email = normalized_map[legacy_id.strip().casefold()]
                    self._connection.execute(
                        "INSERT INTO accounts("
                        "id,email,password_hash,role,status,paid,access_source,created_at,updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            legacy_id,
                            email,
                            row["password_hash"],
                            row["role"],
                            row["status"],
                            row["paid"],
                            row["access_source"],
                            row["created_at"],
                            row["updated_at"],
                        ),
                    )
                self._connection.execute("DROP TABLE accounts_legacy")
        except sqlite3.DatabaseError as exc:
            raise AccountMigrationRequired(
                "Kontomigreringen misslyckades och återställdes utan dataförlust."
            ) from exc

    @staticmethod
    def _account(row: sqlite3.Row | None) -> Account | None:
        if row is None:
            return None
        return Account(
            id=str(row["id"]),
            email=str(row["email"]),
            password_hash=str(row["password_hash"]),
            role=str(row["role"]),
            status=str(row["status"]),
            paid=bool(row["paid"]),
            access_source=str(row["access_source"]),
            display_name=str(row["display_name"]),
            created_at=str(row["created_at"]),
            reset_expires_at=(
                int(row["reset_expires_at"]) if row["reset_expires_at"] is not None else None
            ),
        )

    @staticmethod
    def _select() -> str:
        return (
            "SELECT id,email,password_hash,role,status,paid,access_source,display_name,"
            "created_at,reset_expires_at FROM accounts"
        )

    def seed(self, accounts: dict[str, str], admin_emails: tuple[str, ...]) -> None:
        admins = {normalize_email(value) for value in admin_emails}
        with self._lock, self._connection:
            for raw_email, password_hash in accounts.items():
                email = normalize_email(raw_email)
                role = "admin" if email in admins else "user"
                self._connection.execute(
                    "INSERT INTO accounts(id,email,password_hash,role,status,paid,access_source) "
                    "VALUES(?,?,?,?,'active',1,'legacy') ON CONFLICT(email) DO NOTHING",
                    (str(uuid.uuid4()), email, password_hash, role),
                )
                self._connection.execute(
                    "UPDATE accounts SET password_hash=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE email=? AND access_source IN ('legacy','admin')",
                    (password_hash, email),
                )
                if email in admins:
                    self._connection.execute(
                        "UPDATE accounts SET role='admin',status='active',access_source='admin',"
                        "updated_at=CURRENT_TIMESTAMP WHERE email=?",
                        (email,),
                    )

    def get_by_email(self, email: str) -> Account | None:
        try:
            normalized = normalize_email(email)
        except ValueError:
            return None
        with self._lock:
            row = self._connection.execute(
                f"{self._select()} WHERE email=?", (normalized,)
            ).fetchone()
        return self._account(row)

    def get_by_id(self, account_id: str) -> Account | None:
        with self._lock:
            row = self._connection.execute(f"{self._select()} WHERE id=?", (account_id,)).fetchone()
        return self._account(row)

    def register(self, email: str, password_hash: str) -> Account:
        normalized = normalize_email(email)
        with self._lock, self._connection:
            pending_count = self._connection.execute(
                "SELECT COUNT(*) FROM accounts WHERE status='pending'"
            ).fetchone()[0]
            if int(pending_count) >= 100:
                raise PendingAccountLimit("För många konton väntar på granskning.")
            account_id = str(uuid.uuid4())
            try:
                self._connection.execute(
                    "INSERT INTO accounts(id,email,password_hash,role,status,paid,access_source) "
                    "VALUES(?,?,?,'user','pending',0,'pending')",
                    (account_id, normalized, password_hash),
                )
            except sqlite3.IntegrityError as exc:
                raise EmailUnavailable("E-postadressen används redan.") from exc
        account = self.get_by_id(account_id)
        if account is None:
            raise RuntimeError("Kontot kunde inte läsas efter registrering.")
        return account

    def create_invited(self, email: str, password_hash: str, display_name: str = "") -> Account:
        normalized = normalize_email(email)
        account_id = str(uuid.uuid4())
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    "INSERT INTO accounts("
                    "id,email,password_hash,role,status,paid,access_source,display_name"
                    ") VALUES(?,?,?,'user','pending',0,'admin',?)",
                    (account_id, normalized, password_hash, display_name.strip()[:120]),
                )
        except sqlite3.IntegrityError as exc:
            raise EmailUnavailable("E-postadressen används redan.") from exc
        account = self.get_by_id(account_id)
        if account is None:
            raise RuntimeError("Kontot kunde inte läsas efter skapande.")
        return account

    def search_accounts(
        self,
        query: str = "",
        *,
        status: str = "",
        role: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Account], int]:
        clauses: list[str] = []
        values: list[object] = []
        if query.strip():
            clauses.append("(email LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\')")
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values.extend((f"%{escaped}%", f"%{escaped}%"))
        if status:
            clauses.append("status=?")
            values.append(status)
        if role:
            clauses.append("role=?")
            values.append(role)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM accounts{where}", values
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"{self._select()}{where} "
                "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at "
                "LIMIT ? OFFSET ?",
                (*values, max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()
        return [account for row in rows if (account := self._account(row))], total

    def update_account(
        self,
        account_id: str,
        *,
        acting_id: str,
        email: str | None = None,
        display_name: str | None = None,
        role: str | None = None,
        status: str | None = None,
        paid: bool | None = None,
    ) -> Account:
        current = self.get_by_id(account_id)
        if current is None:
            raise AccountChangeRejected("Kontot hittades inte.")
        if role not in {None, "admin", "user"} or status not in {
            None,
            "pending",
            "active",
            "disabled",
        }:
            raise AccountChangeRejected("Ogiltig roll eller kontostatus.")
        if account_id == acting_id and (role == "user" or status in {"pending", "disabled"}):
            raise AccountChangeRejected("Du kan inte ta bort din egen administratörsåtkomst.")
        if current.is_admin and role == "user" and self._admin_count() <= 1:
            raise AccountChangeRejected("Den sista administratören kan inte nedgraderas.")
        fields: list[str] = []
        values: list[object] = []
        if email is not None:
            fields.append("email=?")
            values.append(normalize_email(email))
        if display_name is not None:
            fields.append("display_name=?")
            values.append(display_name.strip()[:120])
        if role is not None:
            fields.append("role=?")
            values.append(role)
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if paid is not None:
            fields.extend(("paid=?", "access_source=?"))
            values.extend((1 if paid else 0, "payment" if paid else "admin"))
        if not fields:
            return current
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    f"UPDATE accounts SET {','.join(fields)},updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (*values, account_id),
                )
        except sqlite3.IntegrityError as exc:
            raise EmailUnavailable("E-postadressen används redan.") from exc
        updated = self.get_by_id(account_id)
        if updated is None:
            raise RuntimeError("Kontot kunde inte läsas efter uppdatering.")
        return updated

    def set_password_reset(self, account_id: str, token_hash: str, expires_at: int) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE accounts SET reset_token_hash=?,reset_expires_at=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (token_hash, expires_at, account_id),
            )
            if cursor.rowcount != 1:
                raise AccountChangeRejected("Kontot hittades inte.")

    def consume_password_reset(self, token_hash: str, password_hash: str) -> Account | None:
        now = int(time.time())
        with self._lock, self._connection:
            row = self._connection.execute(
                f"{self._select()} WHERE reset_token_hash=? AND reset_expires_at>=?",
                (token_hash, now),
            ).fetchone()
            if row is None:
                return None
            self._connection.execute(
                "UPDATE accounts SET password_hash=?,reset_token_hash=NULL,reset_expires_at=NULL,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (password_hash, row["id"]),
            )
        return self.get_by_id(str(row["id"]))

    def delete_account(self, account_id: str, *, acting_id: str) -> Account:
        account = self.get_by_id(account_id)
        if account is None:
            raise AccountChangeRejected("Kontot hittades inte.")
        if account_id == acting_id:
            raise AccountChangeRejected("Du kan inte radera ditt eget konto.")
        if account.is_admin and self._admin_count() <= 1:
            raise AccountChangeRejected("Den sista administratören kan inte raderas.")
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        return account

    def _admin_count(self) -> int:
        with self._lock:
            return int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM accounts WHERE role='admin'"
                ).fetchone()[0]
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
