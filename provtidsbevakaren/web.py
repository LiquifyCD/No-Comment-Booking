from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, engine
from .auth import AuthManager, UserSession
from .runtime import RuntimeConflict, RuntimeRegistry
from .settings import AppSettings
from .storage import EncryptedSqliteStateStore, VolatileStateStore

COOKIE_NAME = "ptb_session"
STATIC_DIR = Path(__file__).with_name("static")


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class MonitorConfigPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    ssn: str = Field(min_length=12, max_length=13)
    licence_id: int = Field(gt=0)
    examination_type_id: int = Field(gt=0)
    location_id: int = Field(gt=0)
    nearby_location_ids: list[int] = Field(default_factory=list)
    vehicle_type_id: int = Field(default=1, gt=0)
    tachograph_type_id: int = Field(default=1, gt=0)
    occasion_choice_id: int = Field(default=1, gt=0)
    language_id: int = Field(default=13, gt=0)
    date_from: str | None = None
    date_to: str | None = None
    earliest_time: str | None = None
    latest_time: str | None = None
    allowed_weekdays: list[int] | None = None
    poll_interval_seconds: float = Field(default=60, ge=10)
    discord_webhook_url: str = ""
    auto_reserve: bool = False
    auto_book: bool = False
    timezone: str = Field(default="Europe/Stockholm", min_length=1, max_length=80)


class DiscordPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    discord_webhook_url: str = Field(min_length=1, max_length=300)


class CatalogPayload(BaseModel):
    ssn: str = Field(min_length=12, max_length=13)
    licence_id: int = Field(default=0, ge=0)


def create_app(settings: AppSettings, shutdown_callback: Any | None = None) -> FastAPI:
    auth = AuthManager(settings)
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
        remote = request.client.host if request.client else "unknown"
        session = await asyncio.to_thread(
            auth.authenticate, payload.username, payload.password, remote
        )
        if not session:
            raise HTTPException(status_code=401, detail="Invalid credentials or too many attempts")
        response = Response(status_code=204)
        set_session_cookie(response, session)
        return response

    @app.post("/api/auth/logout")
    async def logout(session: UserSession = Depends(csrf_session)) -> Response:
        registry.remove_user(session.user_id)
        auth.revoke(session)
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/api/bootstrap")
    async def bootstrap(session: UserSession = Depends(current_session)) -> dict[str, Any]:
        job = registry.for_user(session.user_id)
        return {
            "mode": settings.mode,
            "version": __version__,
            "user": session.user_id,
            "csrfToken": session.csrf_token,
            "config": store.load_config(session.user_id),
            **job.snapshot(),
        }

    @app.get("/api/events")
    async def events(
        after: int = 0, session: UserSession = Depends(current_session)
    ) -> dict[str, Any]:
        return registry.for_user(session.user_id).snapshot(max(0, after))

    @app.post("/api/bankid/start")
    async def start_bankid(session: UserSession = Depends(csrf_session)) -> dict[str, str]:
        try:
            registry.for_user(session.user_id).start_authentication()
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "starting"}

    @app.post("/api/bankid/cancel")
    async def cancel_bankid(session: UserSession = Depends(csrf_session)) -> dict[str, str]:
        registry.for_user(session.user_id).cancel_authentication()
        return {"status": "cancelled"}

    @app.post("/api/bankid/retry")
    async def retry_bankid(session: UserSession = Depends(csrf_session)) -> dict[str, str]:
        try:
            registry.for_user(session.user_id).retry_authentication()
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "starting"}

    @app.post("/api/bankid/browser-fallback")
    async def browser_fallback(session: UserSession = Depends(csrf_session)) -> dict[str, str]:
        registry.for_user(session.user_id).use_browser_fallback()
        return {"status": "starting"}

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

    @app.post("/api/reservation/book")
    async def book_reservation(session: UserSession = Depends(csrf_session)) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                registry.for_user(session.user_id).complete_pending_booking
            )
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except engine.BotError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/monitor/start")
    async def start_monitor(
        payload: MonitorConfigPayload,
        session: UserSession = Depends(csrf_session),
    ) -> dict[str, str]:
        try:
            registry.for_user(session.user_id).start(payload.model_dump())
        except engine.BotError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "starting"}

    @app.post("/api/monitor/stop")
    async def stop_monitor(session: UserSession = Depends(csrf_session)) -> dict[str, str]:
        await asyncio.to_thread(registry.for_user(session.user_id).stop)
        return {"status": "stopped"}

    @app.post("/api/discord/test")
    async def discord_test(
        payload: DiscordPayload,
        session: UserSession = Depends(csrf_session),
    ) -> dict[str, bool]:
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
