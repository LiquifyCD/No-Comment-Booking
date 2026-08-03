import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from provtidsbevakaren.accounts import AccountMigrationRequired, SqliteAccountStore
from provtidsbevakaren.auth import AuthManager, hash_password, verify_password
from provtidsbevakaren.settings import SettingsError, load_settings
from provtidsbevakaren.storage import EncryptedSqliteStateStore, VolatileStateStore


class SettingsTests(unittest.TestCase):
    def test_local_is_default_and_forces_loopback(self):
        settings = load_settings({"APP_HOST": "0.0.0.0", "APP_PORT": "9000"})
        self.assertEqual("local", settings.mode)
        self.assertEqual("127.0.0.1", settings.host)
        self.assertEqual(9000, settings.port)

    def test_server_mode_is_fail_closed(self):
        with self.assertRaisesRegex(SettingsError, "disabled"):
            load_settings({"APP_MODE": "server"})
        with self.assertRaisesRegex(SettingsError, "Missing"):
            load_settings({"APP_MODE": "server", "ENABLE_SERVER_MODE": "true"})

    def test_server_mode_accepts_complete_secure_configuration(self):
        settings = load_settings(
            {
                "APP_MODE": "server",
                "ENABLE_SERVER_MODE": "true",
                "APP_SECRET_KEY": "x" * 48,
                "PUBLIC_ORIGIN": "https://service.example",
                "ALLOWED_HOSTS": "service.example",
                "SERVER_ACCOUNTS_JSON": '{"user@example.com":"hash"}',
                "ADMIN_EMAILS": "user@example.com",
                "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "REMOTE_WEBDRIVER_URL": "http://browser:4444/wd/hub",
                "REMOTE_BROWSER_VIEW_URL": "https://viewer.example/session/{session_id}",
            }
        )
        self.assertTrue(settings.is_server)
        self.assertEqual({"user@example.com": "hash"}, settings.server_accounts)
        self.assertTrue(settings.has_remote_browser)

    def test_server_mode_can_disable_remote_browser_fallback(self):
        settings = load_settings(
            {
                "APP_MODE": "server",
                "ENABLE_SERVER_MODE": "true",
                "APP_SECRET_KEY": "x" * 48,
                "PUBLIC_ORIGIN": "https://service.example",
                "ALLOWED_HOSTS": "service.example",
                "SERVER_ACCOUNTS_JSON": '{"user@example.com":"hash"}',
                "ADMIN_EMAILS": "user@example.com",
                "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            }
        )
        self.assertFalse(settings.has_remote_browser)

    def test_remote_browser_settings_must_be_paired(self):
        with self.assertRaisesRegex(SettingsError, "configured together"):
            load_settings(
                {
                    "APP_MODE": "server",
                    "ENABLE_SERVER_MODE": "true",
                    "APP_SECRET_KEY": "x" * 48,
                    "PUBLIC_ORIGIN": "https://service.example",
                    "ALLOWED_HOSTS": "service.example",
                    "SERVER_ACCOUNTS_JSON": '{"user@example.com":"hash"}',
                    "ADMIN_EMAILS": "user@example.com",
                    "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                    "REMOTE_WEBDRIVER_URL": "http://browser:4444/wd/hub",
                }
            )

    def test_legacy_configuration_requires_explicit_email_mapping(self):
        base = {
            "APP_MODE": "server",
            "ENABLE_SERVER_MODE": "true",
            "APP_SECRET_KEY": "x" * 48,
            "PUBLIC_ORIGIN": "https://service.example",
            "ALLOWED_HOSTS": "service.example",
            "SERVER_USERS_JSON": '{"legacy-admin":"hash"}',
            "ADMIN_USERS": "legacy-admin",
            "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        }
        with self.assertRaisesRegex(SettingsError, "ACCOUNT_EMAIL_MIGRATION_JSON"):
            load_settings(base)
        settings = load_settings(
            {
                **base,
                "ACCOUNT_EMAIL_MIGRATION_JSON": ('{"legacy-admin":"admin@example.com"}'),
            }
        )
        self.assertEqual({"admin@example.com": "hash"}, settings.server_accounts)
        self.assertEqual(("admin@example.com",), settings.admin_emails)


class AuthTests(unittest.TestCase):
    def test_password_hash_and_signed_session_reject_tampering(self):
        encoded = hash_password("correct horse battery staple", iterations=10_000)
        self.assertTrue(verify_password("correct horse battery staple", encoded))
        self.assertFalse(verify_password("wrong", encoded))
        settings = load_settings({})
        auth = AuthManager(settings)
        session = auth.authenticate_local_token(settings.local_launch_token)
        self.assertIsNotNone(session)
        self.assertIsNone(auth.authenticate_local_token(settings.local_launch_token))
        token = auth.encode(session)
        self.assertEqual(session, auth.decode(token))
        self.assertIsNone(auth.decode(token[:-1] + ("A" if token[-1] != "A" else "B")))


class StorageTests(unittest.TestCase):
    def test_volatile_store_clears_values(self):
        store = VolatileStateStore()
        store.save_config("u", {"ssn": "00000000-0000"})
        store.set_monitor_desired("u", True)
        self.assertTrue(store.monitor_desired("u"))
        store.close()
        self.assertIsNone(store.load_config("u"))
        self.assertFalse(store.monitor_desired("u"))

    def test_sqlite_store_encrypts_sensitive_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = EncryptedSqliteStateStore(path, Fernet.generate_key().decode())
            value = {
                "ssn": "00000000-0000",
                "discord_webhook_url": "https://discord.invalid/secret",
            }
            store.save_config("user", value)
            store.set_monitor_desired("user", True)
            self.assertEqual(value, store.load_config("user"))
            self.assertTrue(store.monitor_desired("user"))
            store.close()
            raw = path.read_bytes()
            self.assertNotIn(b"00000000-0000", raw)
            self.assertNotIn(b"discord.invalid", raw)
            reopened = EncryptedSqliteStateStore(path, Fernet.generate_key().decode())
            self.assertTrue(reopened.monitor_desired("user"))
            reopened.set_monitor_desired("user", False)
            self.assertFalse(reopened.monitor_desired("user"))
            reopened.close()

    def test_legacy_accounts_migrate_atomically_to_email_and_keep_internal_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            connection = sqlite3.connect(path)
            with connection:
                connection.execute(
                    "CREATE TABLE accounts ("
                    "username TEXT PRIMARY KEY,password_hash TEXT NOT NULL,"
                    "role TEXT NOT NULL,status TEXT NOT NULL,paid INTEGER NOT NULL,"
                    "access_source TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO accounts VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                    ("legacy-admin", "hash", "admin", "active", 1, "admin"),
                )
            connection.close()
            with self.assertRaises(AccountMigrationRequired):
                SqliteAccountStore(path)
            connection = sqlite3.connect(path)
            self.assertIn(
                "username",
                {row[1] for row in connection.execute("PRAGMA table_info(accounts)")},
            )
            connection.close()
            store = SqliteAccountStore(path, {"legacy-admin": "Admin@Example.com"})
            account = store.get_by_email("admin@example.com")
            self.assertIsNotNone(account)
            self.assertEqual("legacy-admin", account.id)
            self.assertNotIn(
                "username",
                {row[1] for row in store._connection.execute("PRAGMA table_info(accounts)")},
            )
            store.close()

    def test_legacy_migration_rejects_duplicate_email_mapping_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            connection = sqlite3.connect(path)
            with connection:
                connection.execute(
                    "CREATE TABLE accounts ("
                    "username TEXT PRIMARY KEY,password_hash TEXT NOT NULL,"
                    "role TEXT NOT NULL,status TEXT NOT NULL,paid INTEGER NOT NULL,"
                    "access_source TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
                )
                for username in ("legacy-one", "legacy-two"):
                    connection.execute(
                        "INSERT INTO accounts VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                        (username, "hash", "user", "active", 1, "legacy"),
                    )
            connection.close()
            with self.assertRaisesRegex(AccountMigrationRequired, "duplicerade"):
                SqliteAccountStore(
                    path,
                    {
                        "legacy-one": "same@example.com",
                        "legacy-two": "same@example.com",
                    },
                )
            connection = sqlite3.connect(path)
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            self.assertIn(
                "username",
                {row[1] for row in connection.execute("PRAGMA table_info(accounts)")},
            )
            connection.close()


if __name__ == "__main__":
    unittest.main()
