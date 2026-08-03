from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import __version__, engine
from .accounts import (
    AccountChangeRejected,
    EmailUnavailable,
    PendingAccountLimit,
    SqliteAccountStore,
)
from .auth import AccountAccessDenied, AuthManager, RegistrationRateLimited, UserSession
from .runtime import RuntimeConflict, RuntimeRegistry
from .settings import AppSettings
from .storage import EncryptedSqliteStateStore, VolatileStateStore

COOKIE_NAME = "ptb_session"
STATIC_DIR = Path(__file__).with_name("static")


class LoginPayload(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class RegisterPayload(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)


class PasswordResetPayload(BaseModel):
    token: str = Field(min_length=32, max_length=256)
    password: str = Field(min_length=12, max_length=256)


class AdminCreateAccountPayload(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    display_name: str = Field(default="", max_length=120)


class AdminUpdateAccountPayload(BaseModel):
    email: str | None = Field(default=None, min_length=3, max_length=254)
    display_name: str | None = Field(default=None, max_length=120)
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    status: str | None = Field(default=None, pattern="^(pending|active|disabled)$")
    paid: bool | None = None
    discord_allowed: bool | None = None


class DiscordPolicyPayload(BaseModel):
    enabled_for_new_users: bool
    apply_to_existing_users: bool = False


class MonitorConfigPayload(BaseModel):
    name: str = Field(default="", max_length=80)
    ssn: str = Field(default="", max_length=13)
    licence_id: int = Field(gt=0)
    examination_type_id: int = Field(gt=0)
    location_id: int = Field(gt=0)
    nearby_location_ids: list[int] = Field(default_factory=list, max_length=3)
    vehicle_type_id: int = Field(default=1, gt=0)
    tachograph_type_id: int = Field(default=1, gt=0)
    occasion_choice_id: int = Field(default=1, gt=0)
    language_id: int = Field(default=13, gt=0)
    date_from: str | None = None
    date_to: str | None = None
    earliest_time: str | None = None
    latest_time: str | None = None
    allowed_weekdays: list[int] | None = None
    discord_webhook_url: str = ""
    auto_book: bool = False
    timezone: str = Field(default="Europe/Stockholm", min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_locations(self) -> MonitorConfigPayload:
        selected = [self.location_id, *self.nearby_location_ids]
        if len(selected) != len(set(selected)):
            raise ValueError("Varje provort får bara väljas en gång.")
        return self


class DiscordPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    discord_webhook_url: str = Field(min_length=1, max_length=300)


class CatalogPayload(BaseModel):
    ssn: str = Field(default="", max_length=13)
    licence_id: int = Field(default=0, ge=0)


def create_app(settings: AppSettings, shutdown_callback: Any | None = None) -> FastAPI:
    accounts = (
        SqliteAccountStore(settings.database_path, settings.legacy_email_map)
        if settings.is_server
        else None
    )
    if accounts:
        accounts.seed(settings.server_accounts, settings.admin_emails)
    auth = AuthManager(settings, accounts)
    store = (
        EncryptedSqliteStateStore(settings.database_path, settings.data_encryption_key)
        if settings.is_server
        else VolatileStateStore()
    )
    registry = RuntimeRegistry(settings, store, Path("data/runtime"))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            registry.shutdown()
            auth.close()

    app = FastAPI(
        title="No-Comment-Booking",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.state.auth = auth
    app.state.registry = registry
    app.state.store = store
    app.state.shutdown_callback = shutdown_callback
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        if settings.is_server:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    def set_session_cookie(response: Response, session: UserSession) -> None:
        response.set_cookie(
            COOKIE_NAME,
            auth.encode(session),
            httponly=True,
            secure=settings.is_server,
            samesite="strict",
            max_age=12 * 60 * 60,
            path="/",
        )

    def current_session(request: Request) -> UserSession:
        session = auth.decode(request.cookies.get(COOKIE_NAME))
        if not session:
            raise HTTPException(status_code=401, detail="Authentication required")
        return session

    def csrf_session(
        request: Request, session: UserSession = Depends(current_session)
    ) -> UserSession:
        token = request.headers.get("X-CSRF-Token", "")
        if not token or not secrets.compare_digest(token, session.csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        return session

    def require_admin(session: UserSession) -> UserSession:
        account = auth.account(session.user_id)
        if not account or not account.is_admin:
            raise HTTPException(status_code=403, detail="Administrator access required")
        return session

    def current_admin(session: UserSession = Depends(current_session)) -> UserSession:
        return require_admin(session)

    def csrf_admin(session: UserSession = Depends(csrf_session)) -> UserSession:
        return require_admin(session)

    def remote_identity(request: Request) -> str:
        return request.headers.get("CF-Connecting-IP") or (
            request.client.host if request.client else "unknown"
        )

    @app.get("/", include_in_schema=False)
    async def index(request: Request, token: str = "") -> Response:
        if token and not settings.is_server:
            session = auth.authenticate_local_token(token)
            if not session:
                raise HTTPException(status_code=401, detail="Invalid launch token")
            response = RedirectResponse("/", status_code=303)
            set_session_cookie(response, session)
            return response
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": settings.mode, "version": __version__}

    @app.post("/api/auth/login")
    async def login(payload: LoginPayload, request: Request) -> Response:
        if not settings.is_server:
            raise HTTPException(status_code=404, detail="Not available in local mode")
        remote = remote_identity(request)
        try:
            session = await asyncio.to_thread(
                auth.authenticate, payload.email, payload.password, remote
            )
        except AccountAccessDenied as exc:
            detail = (
                "Kontot väntar på betalning eller administratörsgodkännande."
                if exc.status == "pending"
                else "Kontot är avstängt."
            )
            raise HTTPException(status_code=403, detail=detail) from exc
        if not session:
            raise HTTPException(
                status_code=401,
                detail="Fel e-postadress eller lösenord, eller för många försök.",
            )
        response = Response(status_code=204)
        set_session_cookie(response, session)
        return response

    @app.post("/api/auth/register", status_code=201)
    async def register(payload: RegisterPayload, request: Request) -> dict[str, str]:
        if not settings.is_server:
            raise HTTPException(status_code=404, detail="Not available in local mode")
        try:
            account = await asyncio.to_thread(
                auth.register, payload.email, payload.password, remote_identity(request)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except EmailUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PendingAccountLimit as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RegistrationRateLimited as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return {"id": account.id, "email": account.email, "status": account.status}

    @app.post("/api/auth/reset-password")
    async def reset_password(payload: PasswordResetPayload) -> Response:
        try:
            account = await asyncio.to_thread(auth.reset_password, payload.token, payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if account is None:
            raise HTTPException(
                status_code=400, detail="Återställningslänken är ogiltig eller har gått ut."
            )
        return Response(status_code=204)

    @app.post("/api/auth/logout")
    async def logout(session: UserSession = Depends(csrf_session)) -> Response:
        auth.revoke(session)
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/api/bootstrap")
    async def bootstrap(session: UserSession = Depends(current_session)) -> dict[str, Any]:
        job = registry.for_user(session.user_id)
        account = auth.account(session.user_id)
        saved_config = store.load_config(session.user_id)
        public_config = (
            {
                key: value
                for key, value in saved_config.items()
                if key not in {"name", "ssn", "discord_webhook_url"}
            }
            if saved_config
            else None
        )
        live_state = job.snapshot() if account and account.is_admin else job.status_snapshot()
        return {
            "mode": settings.mode,
            "version": __version__,
            "user": session.user_id,
            "account": account.public_dict() if account else None,
            "isAdmin": bool(account and account.is_admin),
            "discordAllowed": bool(account and (account.is_admin or account.discord_allowed)),
            "discordDefaultForNewUsers": (
                accounts.discord_default_for_new_users()
                if accounts and account and account.is_admin
                else False
            ),
            "csrfToken": session.csrf_token,
            "browserFallbackAvailable": not settings.is_server or settings.has_remote_browser,
            "config": public_config,
            **live_state,
        }

    @app.get("/api/admin/users")
    async def admin_users(
        q: str = "",
        status: str = "",
        role: str = "",
        limit: int = 50,
        offset: int = 0,
        _session: UserSession = Depends(current_admin),
    ) -> dict[str, Any]:
        if status not in {"", "pending", "active", "disabled"} or role not in {
            "",
            "admin",
            "user",
        }:
            raise HTTPException(status_code=422, detail="Ogiltigt sökfilter.")
        users, total = await asyncio.to_thread(
            auth.search_accounts,
            q[:120],
            status=status,
            role=role,
            limit=max(1, min(limit, 100)),
            offset=max(0, offset),
        )
        return {
            "users": [account.public_dict() for account in users],
            "total": total,
            "limit": max(1, min(limit, 100)),
            "offset": max(0, offset),
        }

    @app.post("/api/admin/users", status_code=201)
    async def admin_create_user(
        payload: AdminCreateAccountPayload,
        _session: UserSession = Depends(csrf_admin),
    ) -> dict[str, Any]:
        try:
            account, reset_token = await asyncio.to_thread(
                auth.create_invited, payload.email, payload.display_name
            )
        except (ValueError, EmailUnavailable) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "account": account.public_dict(),
            "resetToken": reset_token,
            "resetExpiresIn": 30 * 60,
        }

    @app.patch("/api/admin/users/{account_id}")
    async def admin_update_user(
        account_id: str,
        payload: AdminUpdateAccountPayload,
        session: UserSession = Depends(csrf_admin),
    ) -> dict[str, Any]:
        changes = {
            field: value
            for field, value in payload.model_dump().items()
            if field in payload.model_fields_set
        }
        try:
            account = await asyncio.to_thread(
                auth.update_account,
                account_id,
                acting_id=session.user_id,
                **changes,
            )
        except (ValueError, EmailUnavailable, AccountChangeRejected) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if account.status != "active":
            await asyncio.to_thread(registry.remove_user, account.id)
        return account.public_dict()

    @app.post("/api/admin/users/{account_id}/password-reset")
    async def admin_reset_user_password(
        account_id: str,
        _session: UserSession = Depends(csrf_admin),
    ) -> dict[str, Any]:
        try:
            reset_token = await asyncio.to_thread(auth.initiate_password_reset, account_id)
        except AccountChangeRejected as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"resetToken": reset_token, "resetExpiresIn": 30 * 60}

    @app.delete("/api/admin/users/{account_id}")
    async def admin_delete_user(
        account_id: str,
        session: UserSession = Depends(csrf_admin),
    ) -> Response:
        try:
            account = await asyncio.to_thread(
                auth.delete_account, account_id, acting_id=session.user_id
            )
        except AccountChangeRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await asyncio.to_thread(registry.remove_user, account.id, delete_config=True)
        return Response(status_code=204)

    @app.put("/api/admin/discord-policy")
    async def admin_discord_policy(
        payload: DiscordPolicyPayload,
        _session: UserSession = Depends(csrf_admin),
    ) -> dict[str, bool]:
        if accounts is None:
            raise HTTPException(status_code=404, detail="Not available in local mode")
        await asyncio.to_thread(
            accounts.set_discord_policy,
            payload.enabled_for_new_users,
            apply_existing=payload.apply_to_existing_users,
        )
        return {"enabledForNewUsers": accounts.discord_default_for_new_users()}

    @app.get("/api/events", include_in_schema=False)
    @app.get("/api/live")
    async def events(
        after: int = 0, session: UserSession = Depends(current_admin)
    ) -> dict[str, Any]:
        return registry.for_user(session.user_id).snapshot(max(0, after))

    @app.get("/api/events/stream", include_in_schema=False)
    @app.get("/api/live/stream")
    async def event_stream(
        request: Request,
        after: int = 0,
        session: UserSession = Depends(current_admin),
    ) -> StreamingResponse:
        job = registry.for_user(session.user_id)
        header_cursor = request.headers.get("Last-Event-ID", "")
        cursor = max(0, after, int(header_cursor) if header_cursor.isdigit() else 0)

        async def snapshots():
            nonlocal cursor
            previous_state = ""
            last_send = time.monotonic()
            while not await request.is_disconnected():
                account = auth.account(session.user_id) if settings.is_server else None
                if session.expires_at < time.time() or (
                    settings.is_server and (not account or not account.is_active)
                ):
                    yield 'event: auth\ndata: {"status":401}\n\n'
                    return

                snapshot = job.snapshot(cursor)
                events = snapshot["events"]
                if events:
                    cursor = max(cursor, *(int(event["id"]) for event in events))
                state_signature = json.dumps(
                    {**snapshot, "events": []},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if events or state_signature != previous_state:
                    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\ndata: {payload}\n\n"
                    previous_state = state_signature
                    last_send = time.monotonic()
                elif time.monotonic() - last_send >= 15:
                    yield ": keepalive\n\n"
                    last_send = time.monotonic()
                await asyncio.sleep(1 if snapshot["state"].startswith("bankid_") else 2)

        return StreamingResponse(
            snapshots(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/status")
    async def monitor_status(
        session: UserSession = Depends(current_session),
    ) -> dict[str, Any]:
        return registry.for_user(session.user_id).status_snapshot()

    @app.get("/api/status/stream")
    async def monitor_status_stream(
        request: Request,
        session: UserSession = Depends(current_session),
    ) -> StreamingResponse:
        job = registry.for_user(session.user_id)

        async def snapshots():
            previous = ""
            last_send = time.monotonic()
            while not await request.is_disconnected():
                account = auth.account(session.user_id) if settings.is_server else None
                if session.expires_at < time.time() or (
                    settings.is_server and (not account or not account.is_active)
                ):
                    yield 'event: auth\ndata: {"status":401}\n\n'
                    return
                snapshot = job.status_snapshot()
                payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                if payload != previous:
                    yield f"data: {payload}\n\n"
                    previous = payload
                    last_send = time.monotonic()
                elif time.monotonic() - last_send >= 15:
                    yield ": keepalive\n\n"
                    last_send = time.monotonic()
                await asyncio.sleep(1 if snapshot["state"].startswith("bankid_") else 3)

        return StreamingResponse(
            snapshots(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/bankid/start")
    async def start_bankid(session: UserSession = Depends(csrf_session)) -> dict[str, Any]:
        job = registry.for_user(session.user_id)
        try:
            job.start_authentication()
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.status_snapshot()

    @app.post("/api/bankid/cancel")
    async def cancel_bankid(session: UserSession = Depends(csrf_session)) -> dict[str, Any]:
        job = registry.for_user(session.user_id)
        job.cancel_authentication()
        return job.status_snapshot()

    @app.post("/api/bankid/retry")
    async def retry_bankid(session: UserSession = Depends(csrf_session)) -> dict[str, Any]:
        job = registry.for_user(session.user_id)
        try:
            job.retry_authentication()
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.status_snapshot()

    @app.post("/api/bankid/browser-fallback")
    async def browser_fallback(session: UserSession = Depends(csrf_session)) -> dict[str, Any]:
        if settings.is_server and not settings.has_remote_browser:
            raise HTTPException(
                status_code=503,
                detail="Browser fallback is not available on this server",
            )
        job = registry.for_user(session.user_id)
        job.use_browser_fallback()
        return job.status_snapshot()

    @app.get("/api/bankid/qr.svg")
    async def bankid_qr(session: UserSession = Depends(current_session)) -> Response:
        try:
            image = registry.for_user(session.user_id).bankid_qr_svg()
        except engine.BotError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(content=image, media_type="image/svg+xml")

    @app.get("/api/bankid/open")
    async def open_bankid(session: UserSession = Depends(current_session)) -> Response:
        try:
            uri = registry.for_user(session.user_id).bankid_uri()
        except engine.BotError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(uri, status_code=307)

    @app.get("/api/catalog")
    async def cached_catalog(session: UserSession = Depends(current_session)) -> dict[str, Any]:
        value = registry.for_user(session.user_id).cached_catalog()
        if value is None:
            raise HTTPException(status_code=404, detail="Ingen katalog har hämtats ännu")
        return value

    @app.post("/api/catalog/refresh")
    async def refresh_catalog(
        payload: CatalogPayload,
        session: UserSession = Depends(csrf_session),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                registry.for_user(session.user_id).refresh_catalog,
                payload.ssn,
                payload.licence_id,
            )
        except engine.AuthenticationRequiredError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except engine.BotError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/monitor/start")
    async def start_monitor(
        payload: MonitorConfigPayload,
        session: UserSession = Depends(csrf_session),
    ) -> dict[str, Any]:
        account = auth.account(session.user_id)
        raw_config = payload.model_dump()
        raw_config["poll_interval_seconds"] = 15
        raw_config["auto_reserve"] = False
        if raw_config.get("discord_webhook_url") and not (
            account and (account.is_admin or account.discord_allowed)
        ):
            raise HTTPException(status_code=403, detail="Discord-notiser är inte aktiverade för kontot.")
        try:
            job = registry.for_user(session.user_id)
            job.start(raw_config)
        except engine.BotError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.status_snapshot()

    @app.post("/api/monitor/stop")
    async def stop_monitor(session: UserSession = Depends(csrf_session)) -> dict[str, Any]:
        job = registry.for_user(session.user_id)
        await asyncio.to_thread(job.stop)
        return job.status_snapshot()

    @app.post("/api/discord/test")
    async def discord_test(
        payload: DiscordPayload,
        session: UserSession = Depends(csrf_session),
    ) -> dict[str, bool]:
        account = auth.account(session.user_id)
        if settings.is_server and not (
            account and (account.is_admin or account.discord_allowed)
        ):
            raise HTTPException(status_code=403, detail="Discord-notiser är inte aktiverade för kontot.")
        try:
            engine.Config.from_dict(
                {
                    "name": payload.name,
                    "ssn": "00000000-0000",
                    "licence_id": 1,
                    "examination_type_id": 1,
                    "location_id": 1,
                    "discord_webhook_url": payload.discord_webhook_url,
                }
            )
        except engine.BotError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        sent = await asyncio.to_thread(
            engine.notify_discord,
            payload.discord_webhook_url,
            f"✅ [{payload.name}] Test notification from No-Comment-Booking",
        )
        if not sent:
            raise HTTPException(status_code=502, detail="Discord-testet misslyckades")
        return {"sent": True}

    @app.post("/api/app/exit")
    async def exit_local(session: UserSession = Depends(csrf_session)) -> dict[str, str]:
        if settings.is_server:
            raise HTTPException(status_code=404, detail="Not available in server mode")
        registry.remove_user(session.user_id, delete_config=True)
        callback = app.state.shutdown_callback
        if callback:
            callback()
        return {"status": "closing"}

    return app
