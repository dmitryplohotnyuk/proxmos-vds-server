from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import AppSettings, default_settings
from .database import AuthenticatedSession, Database
from .router_client import RouterClient, RouterClientError
from .security import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    csrf_token,
    new_login_csrf,
    valid_csrf,
)


PACKAGE_DIR = Path(__file__).resolve().parent
NOTICE_MESSAGES = {
    "route-added": "Маршрут добавлен и конфигурация Caddy применена.",
    "route-updated": "Маршрут обновлен.",
    "route-enabled": "Маршрут включен.",
    "route-disabled": "Маршрут отключен.",
    "route-removed": "Маршрут удален.",
    "password-changed": "Пароль изменен, остальные сессии завершены.",
}


async def form_data(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" not in content_type:
        return {}
    body = await request.body()
    if len(body) > 16384:
        return {}
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {}
    parsed = parse_qs(decoded, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def create_app(
    settings: AppSettings | None = None,
    router_client: RouterClient | None = None,
) -> FastAPI:
    config = settings or default_settings()
    database = Database(config.database_path)
    router = router_client or RouterClient(config.router_command)
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        yield

    app = FastAPI(title="Domain Router", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.state.database = database
    app.state.router = router
    app.state.settings = config

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; img-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path != "/healthz":
            response.headers["Cache-Control"] = "no-store"
        return response

    def session_for(request: Request) -> AuthenticatedSession | None:
        return database.get_session(request.cookies.get(SESSION_COOKIE))

    def login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    def template(
        request: Request,
        name: str,
        *,
        session: AuthenticatedSession | None = None,
        status_code: int = 200,
        **context,
    ) -> HTMLResponse:
        values = {
            "session": session,
            "notice": NOTICE_MESSAGES.get(request.query_params.get("notice", "")),
            **context,
        }
        if session and not values.get("logout_csrf"):
            values["logout_csrf"] = csrf_token(session.csrf_secret, "logout")
        return templates.TemplateResponse(
            request=request,
            name=name,
            context=values,
            status_code=status_code,
        )

    def csrf_or_error(
        session: AuthenticatedSession, action: str, supplied: str
    ) -> str | None:
        if not valid_csrf(session.csrf_secret, action, supplied):
            return "Сессия формы устарела. Обновите страницу и повторите действие."
        return None

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if session_for(request):
            return RedirectResponse("/", status_code=303)
        login_csrf = new_login_csrf()
        response = template(request, "login.html", login_csrf=login_csrf, error=None)
        response.set_cookie(
            LOGIN_CSRF_COOKIE,
            login_csrf,
            secure=config.secure_cookies,
            httponly=True,
            samesite="strict",
            max_age=600,
            path="/login",
        )
        return response

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        form = await form_data(request)
        username = form.get("username", "")[:64]
        supplied_csrf = form.get("csrf_token", "")
        cookie_csrf = request.cookies.get(LOGIN_CSRF_COOKIE, "")
        if not supplied_csrf or not hmac.compare_digest(supplied_csrf, cookie_csrf):
            replacement_csrf = new_login_csrf()
            response = template(
                request,
                "login.html",
                login_csrf=replacement_csrf,
                error="Форма входа устарела. Обновите страницу.",
                status_code=400,
            )
            response.set_cookie(
                LOGIN_CSRF_COOKIE,
                replacement_csrf,
                secure=config.secure_cookies,
                httponly=True,
                samesite="strict",
                max_age=600,
                path="/login",
            )
            return response
        if database.login_is_limited(
            username, config.login_window_minutes, config.login_max_attempts
        ):
            database.audit(None, "login", "session", username, "limited")
            return template(
                request,
                "login.html",
                login_csrf=cookie_csrf,
                error="Вход временно ограничен. Повторите попытку позже.",
                status_code=429,
            )
        user = database.verify_user(username, form.get("password", ""))
        if user is None:
            database.record_login_failure(username)
            database.audit(None, "login", "session", username, "failed")
            return template(
                request,
                "login.html",
                login_csrf=cookie_csrf,
                error="Неверный логин или пароль.",
                status_code=401,
            )
        database.clear_login_failures(username)
        token, session = database.create_session(int(user["id"]), config.session_hours)
        database.audit(session.user_id, "login", "session", username, "success")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            secure=config.secure_cookies,
            httponly=True,
            samesite="strict",
            max_age=config.session_hours * 3600,
            path="/",
        )
        response.delete_cookie(LOGIN_CSRF_COOKIE, path="/login")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        if csrf_or_error(session, "logout", form.get("csrf_token", "")):
            return login_redirect()
        database.audit(session.user_id, "logout", "session", session.username, "success")
        database.delete_session(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        error = None
        try:
            routes = router.routes()
            status = router.status()
        except RouterClientError as exc:
            routes = []
            status = {"services": {}, "routes": 0}
            error = str(exc)
        route_actions = {
            route["domain"]: {
                "toggle": csrf_token(session.csrf_secret, f"route:toggle:{route['domain']}"),
                "probe": csrf_token(session.csrf_secret, f"route:probe:{route['domain']}"),
            }
            for route in routes
        }
        return template(
            request,
            "dashboard.html",
            session=session,
            routes=routes,
            service_status=status.get("services", {}),
            route_actions=route_actions,
            logout_csrf=csrf_token(session.csrf_secret, "logout"),
            error=error,
        )

    @app.get("/routes/new", response_class=HTMLResponse)
    async def route_new(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        return template(
            request,
            "route_form.html",
            session=session,
            heading="Новый маршрут",
            route=None,
            action="/routes/new",
            csrf=csrf_token(session.csrf_secret, "route:add"),
            error=None,
        )

    @app.post("/routes/new", response_class=HTMLResponse)
    async def route_create(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        error = csrf_or_error(session, "route:add", form.get("csrf_token", ""))
        domain = form.get("domain", "")
        upstream = form.get("upstream", "")
        if error is None:
            try:
                router.add(domain, upstream)
                database.audit(session.user_id, "add", "route", domain, "success", upstream)
                return RedirectResponse("/?notice=route-added", status_code=303)
            except RouterClientError as exc:
                error = str(exc)
                database.audit(session.user_id, "add", "route", domain, "failed", error)
        return template(
            request,
            "route_form.html",
            session=session,
            heading="Новый маршрут",
            route={"domain": domain, "upstream": upstream},
            action="/routes/new",
            csrf=csrf_token(session.csrf_secret, "route:add"),
            error=error,
            status_code=400,
        )

    @app.get("/routes/edit", response_class=HTMLResponse)
    async def route_edit(request: Request, domain: str = ""):
        session = session_for(request)
        if session is None:
            return login_redirect()
        try:
            route = next(item for item in router.routes() if item["domain"] == domain)
        except (RouterClientError, StopIteration):
            return RedirectResponse("/", status_code=303)
        return template(
            request,
            "route_form.html",
            session=session,
            heading="Изменить маршрут",
            route=route,
            action="/routes/edit",
            csrf=csrf_token(session.csrf_secret, f"route:set:{domain}"),
            error=None,
        )

    @app.post("/routes/edit", response_class=HTMLResponse)
    async def route_update(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        domain = form.get("domain", "")
        upstream = form.get("upstream", "")
        error = csrf_or_error(
            session, f"route:set:{domain}", form.get("csrf_token", "")
        )
        if error is None:
            try:
                router.update(domain, upstream)
                database.audit(session.user_id, "set", "route", domain, "success", upstream)
                return RedirectResponse("/?notice=route-updated", status_code=303)
            except RouterClientError as exc:
                error = str(exc)
                database.audit(session.user_id, "set", "route", domain, "failed", error)
        return template(
            request,
            "route_form.html",
            session=session,
            heading="Изменить маршрут",
            route={"domain": domain, "upstream": upstream},
            action="/routes/edit",
            csrf=csrf_token(session.csrf_secret, f"route:set:{domain}"),
            error=error,
            status_code=400,
        )

    @app.post("/routes/toggle")
    async def route_toggle(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        domain = form.get("domain", "")
        action = f"route:toggle:{domain}"
        if csrf_or_error(session, action, form.get("csrf_token", "")):
            return RedirectResponse("/", status_code=303)
        enabled = form.get("enabled") == "true"
        try:
            router.toggle(domain, enabled)
            database.audit(
                session.user_id, "enable" if enabled else "disable", "route", domain, "success"
            )
            notice = "route-enabled" if enabled else "route-disabled"
            return RedirectResponse(f"/?notice={notice}", status_code=303)
        except RouterClientError as exc:
            database.audit(session.user_id, "toggle", "route", domain, "failed", str(exc))
            return RedirectResponse("/", status_code=303)

    @app.get("/routes/delete", response_class=HTMLResponse)
    async def route_delete_page(request: Request, domain: str = ""):
        session = session_for(request)
        if session is None:
            return login_redirect()
        try:
            route = next(item for item in router.routes() if item["domain"] == domain)
        except (RouterClientError, StopIteration):
            return RedirectResponse("/", status_code=303)
        return template(
            request,
            "route_delete.html",
            session=session,
            route=route,
            csrf=csrf_token(session.csrf_secret, f"route:delete:{domain}"),
            error=None,
        )

    @app.post("/routes/delete", response_class=HTMLResponse)
    async def route_delete(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        domain = form.get("domain", "")
        error = csrf_or_error(
            session, f"route:delete:{domain}", form.get("csrf_token", "")
        )
        if error is None:
            try:
                router.remove(domain)
                database.audit(session.user_id, "remove", "route", domain, "success")
                return RedirectResponse("/?notice=route-removed", status_code=303)
            except RouterClientError as exc:
                error = str(exc)
                database.audit(session.user_id, "remove", "route", domain, "failed", error)
        return template(
            request,
            "route_delete.html",
            session=session,
            route={"domain": domain, "upstream": form.get("upstream", "")},
            csrf=csrf_token(session.csrf_secret, f"route:delete:{domain}"),
            error=error,
            status_code=400,
        )

    @app.post("/routes/probe", response_class=HTMLResponse)
    async def route_probe(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        domain = form.get("domain", "")
        if csrf_or_error(
            session, f"route:probe:{domain}", form.get("csrf_token", "")
        ):
            return RedirectResponse("/", status_code=303)
        try:
            result = router.probe(form.get("upstream", ""))
            database.audit(
                session.user_id,
                "probe",
                "route",
                domain,
                "success",
                f"HTTP {result['status_code']} in {result['elapsed']:.3f}s",
            )
            message = f"HTTP {result['status_code']}, {result['elapsed']:.3f} сек."
            result_kind = "success" if int(result["status_code"]) < 500 else "warning"
        except RouterClientError as exc:
            message = str(exc)
            result_kind = "error"
            database.audit(session.user_id, "probe", "route", domain, "failed", message)
        routes = router.routes()
        status = router.status()
        route_actions = {
            route["domain"]: {
                "toggle": csrf_token(session.csrf_secret, f"route:toggle:{route['domain']}"),
                "probe": csrf_token(session.csrf_secret, f"route:probe:{route['domain']}"),
            }
            for route in routes
        }
        return template(
            request,
            "dashboard.html",
            session=session,
            routes=routes,
            service_status=status.get("services", {}),
            route_actions=route_actions,
            logout_csrf=csrf_token(session.csrf_secret, "logout"),
            probe_result={"domain": domain, "message": message, "kind": result_kind},
            error=None,
        )

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        return template(
            request,
            "audit.html",
            session=session,
            events=database.audit_events(),
            logout_csrf=csrf_token(session.csrf_secret, "logout"),
        )

    @app.get("/account", response_class=HTMLResponse)
    async def account_page(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        return template(
            request,
            "account.html",
            session=session,
            csrf=csrf_token(session.csrf_secret, "account:password"),
            logout_csrf=csrf_token(session.csrf_secret, "logout"),
            error=None,
        )

    @app.post("/account", response_class=HTMLResponse)
    async def account_password(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        error = csrf_or_error(session, "account:password", form.get("csrf_token", ""))
        if error is None and form.get("new_password") != form.get("confirm_password"):
            error = "Новый пароль и подтверждение не совпадают."
        if error is None:
            try:
                changed = database.change_password(
                    session.user_id,
                    form.get("current_password", ""),
                    form.get("new_password", ""),
                )
                if not changed:
                    error = "Текущий пароль указан неверно."
            except ValueError as exc:
                error = str(exc)
        if error is None:
            token, _ = database.create_session(session.user_id, config.session_hours)
            database.audit(session.user_id, "password-change", "user", session.username, "success")
            response = RedirectResponse("/?notice=password-changed", status_code=303)
            response.set_cookie(
                SESSION_COOKIE,
                token,
                secure=config.secure_cookies,
                httponly=True,
                samesite="strict",
                max_age=config.session_hours * 3600,
                path="/",
            )
            return response
        database.audit(session.user_id, "password-change", "user", session.username, "failed")
        return template(
            request,
            "account.html",
            session=session,
            csrf=csrf_token(session.csrf_secret, "account:password"),
            logout_csrf=csrf_token(session.csrf_secret, "logout"),
            error=error,
            status_code=400,
        )

    return app


app = create_app()
