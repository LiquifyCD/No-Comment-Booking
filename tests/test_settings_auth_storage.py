import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

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
                "SERVER_USERS_JSON": '{"user":"hash"}',
                "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "REMOTE_WEBDRIVER_URL": "http://browser:4444/wd/hub",
                "REMOTE_BROWSER_VIEW_URL": "https://viewer.example/session/{session_id}",
            }
        )
        self.assertTrue(settings.is_server)
        self.assertEqual({"user": "hash"}, settings.server_users)


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
        store.close()
        self.assertIsNone(store.load_config("u"))

    def test_sqlite_store_encrypts_sensitive_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = EncryptedSqliteStateStore(path, Fernet.generate_key().decode())
            value = {
                "ssn": "00000000-0000",
                "discord_webhook_url": "https://discord.invalid/secret",
            }
            store.save_config("user", value)
            self.assertEqual(value, store.load_config("user"))
            store.close()
            raw = path.read_bytes()
            self.assertNotIn(b"00000000-0000", raw)
            self.assertNotIn(b"discord.invalid", raw)


if __name__ == "__main__":
    unittest.main()
