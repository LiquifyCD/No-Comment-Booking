from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass

from .settings import AppSettings


def hash_password(password: str, *, iterations: int = 600_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            base64.urlsafe_b64decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(base64.urlsafe_b64encode(digest).decode(), expected)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class UserSession:
    session_id: str
    user_id: str
    csrf_token: str
    expires_at: int


class AuthManager:
    def __init__(self, settings: AppSettings):
        self.settings = settings
        self._sessions: dict[str, UserSession] = {}
        self._lock = threading.RLock()
        self._login_attempts: dict[str, list[float]] = {}
        self._local_token_consumed = False

    def authenticate(self, username: str, password: str, remote: str) -> UserSession | None:
        now = time.time()
        with self._lock:
            attempts = [
                stamp for stamp in self._login_attempts.get(remote, []) if now - stamp < 300
            ]
            if len(attempts) >= 8:
                return None
            attempts.append(now)
            self._login_attempts[remote] = attempts
        encoded = self.settings.server_users.get(username)
        if not encoded or not verify_password(password, encoded):
            return None
        with self._lock:
            self._login_attempts.pop(remote, None)
        return self.create_session(username)

    def authenticate_local_token(self, token: str) -> UserSession | None:
        with self._lock:
            if (
                self.settings.is_server
                or self._local_token_consumed
                or not secrets.compare_digest(token, self.settings.local_launch_token)
            ):
                return None
            self._local_token_consumed = True
            return self.create_session("local")

    def create_session(self, user_id: str) -> UserSession:
        session = UserSession(
            session_id=secrets.token_urlsafe(24),
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(24),
            expires_at=int(time.time()) + 12 * 60 * 60,
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def encode(self, session: UserSession) -> str:
        payload = json.dumps(
            {"sid": session.session_id, "exp": session.expires_at},
            separators=(",", ":"),
        ).encode()
        body = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self.settings.secret_key.encode(), body, hashlib.sha256).digest()
        return f"{body.decode()}.{base64.urlsafe_b64encode(signature).decode()}"

    def decode(self, token: str | None) -> UserSession | None:
        if not token or "." not in token:
            return None
        body_text, signature_text = token.split(".", 1)
        body = body_text.encode()
        expected = hmac.new(self.settings.secret_key.encode(), body, hashlib.sha256).digest()
        try:
            signature = base64.urlsafe_b64decode(signature_text)
            payload = json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        if not hmac.compare_digest(signature, expected) or int(payload.get("exp", 0)) < time.time():
            return None
        with self._lock:
            session = self._sessions.get(str(payload.get("sid", "")))
            if session and session.expires_at >= time.time():
                return session
        return None

    def revoke(self, session: UserSession) -> None:
        with self._lock:
            self._sessions.pop(session.session_id, None)
