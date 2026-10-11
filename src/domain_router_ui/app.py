from __future__ import annotations

import asyncio

import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from starlette.concurrency import run_in_threadpool
from domain_router_services.integrations import IntegrationError, valid_credentials

from .config import AppSettings, default_settings
from .access import METHODS, access_groups, access_rows, validate_link, vault_origin
from .database import AuthenticatedSession, Database
from .router_client import RouterClient, RouterClientError
from .service_client import ServiceClient, ServiceClientError
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
    "service-enabled": "Доступ к сервису через Tailscale включён.",
    "service-disabled": "Доступ к сервису через Tailscale выключен.",
    "integration-updated": "Операция выполнена. Проверьте состояние подключения ниже.",
    "access-saved": "Привязка сохранена. Пароль в сервисе не изменялся.",
    "access-removed": "Привязка удалена. Запись в хранилище и сам сервис не изменены.",
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
    service_client: ServiceClient | None = None,
) -> FastAPI:
    config = settings or default_settings()
    vault_url = vault_origin(config.vault_url)
    database = Database(config.database_path)
    router = router_client or RouterClient(config.router_command)
    services = service_client or ServiceClient(config.service_command)
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    guide_html = MarkdownIt("default", {"html": False}).render(
        (PACKAGE_DIR / "help" / "cloudflare.md").read_text(encoding="utf-8")
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        yield

    app = FastAPI(title="Domain Router", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.state.database = database
    app.state.router = router
    app.state.settings = config
    app.state.services = services

    # Argon2 uses 64 MiB per operation: keep hashing off the event loop and bounded.
    password_slots = asyncio.Semaphore(2)

    async def password_operation(operation, *args):
        async with password_slots:
            return await run_in_threadpool(operation, *args)

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

    @app.get("/docs", response_class=HTMLResponse)
    @app.get("/docs/access", response_class=HTMLResponse)
    async def documentation(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        access_guide = request.url.path == '/docs/access'
        rendered = MarkdownIt('default', {'html': False}).render(
            (PACKAGE_DIR / 'help' / 'access.md').read_text(encoding='utf-8')
        ) if access_guide else guide_html
        return template(request, "docs.html", session=session, guide_html=rendered,
                        access_guide=access_guide, vault_url=vault_url)

    async def integrations_page(request, session, *, error=None, status_code=200):
        try:
            state = await run_in_threadpool(services.integrations)
        except ServiceClientError:
            state = {'errors': ['panel', 'gateway']}
            error = error or "Агент подключений недоступен. Действующие подключения не изменены."
        return template(request, "integrations.html", session=session, state=state,
                        error=error, status_code=status_code,
                        integration_csrf=csrf_token(session.csrf_secret, "integrations"))

    @app.get("/integrations", response_class=HTMLResponse)
    async def integrations_get(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        return await integrations_page(request, session)

    @app.post("/integrations", response_class=HTMLResponse)
    async def integrations_post(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        error = csrf_or_error(session, "integrations", form.get("csrf_token", ""))
        if error:
            return await integrations_page(request, session, error=error, status_code=400)
        action, target = form.get('operation', ''), form.get('target', '')
        allowed = {'tailscale-connect', 'panel-serve', 'cloudflare-apply', 'cloudflare-rollback'}
        if action not in allowed or target not in {'panel', 'gateway'} or (
            target == 'gateway' and action != 'tailscale-connect') or form.get('confirm') != 'yes':
            return await integrations_page(request, session, error="Подтвердите выбранную операцию.", status_code=400)
        if database.login_is_limited(session.username, config.login_window_minutes, config.login_max_attempts):
            return await integrations_page(request, session, error="Проверка пароля временно ограничена.", status_code=429)
        user = await password_operation(database.verify_user, session.username, form.get('current_password', ''))
        if user is None:
            database.record_login_failure(session.username)
            database.audit(session.user_id, action, "integration", target, "denied")
            return await integrations_page(request, session, error="Неверный текущий пароль.", status_code=403)
        database.clear_login_failures(session.username)
        operation = {'action': action}
        if action in {'cloudflare-apply', 'cloudflare-rollback'}:
            operation['expected_revision'] = form.get('revision', '')
        if action == 'cloudflare-apply':
            operation.update(mode=form.get('mode', ''), secret=form.get('secret', ''))
            try:
                valid_credentials(operation['mode'], operation['secret'])
            except IntegrationError as exc:
                return await integrations_page(request, session, error=str(exc), status_code=400)
        try:
            await run_in_threadpool(services.integration_action, target, operation)
        except ServiceClientError:
            database.audit(session.user_id, action, "integration", target, "failed")
            return await integrations_page(request, session,
                error="Операция не подтверждена. Проверьте состояние ниже. При неудачной замене Tunnel восстанавливается прежняя конфигурация; при обрыве связи дождитесь автоматического отката (90 секунд).",
                status_code=409)
        database.audit(session.user_id, action, "integration", target, "success")
        return RedirectResponse('/integrations?notice=integration-updated', status_code=303)

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
        user = await password_operation(database.verify_user, username, form.get("password", ""))
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

    async def internal_page(request: Request, session: AuthenticatedSession,
                            error: str | None = None, status_code: int = 200):
        try:
            inventory = await run_in_threadpool(services.inventory)
        except ServiceClientError as exc:
            inventory = {"services": [], "updated_at": None, "errors": []}
            error = error or str(exc)
        actions = {item['id']: csrf_token(session.csrf_secret, f"service:toggle:{item['id']}")
                   for item in inventory['services']}
        grouped = {}
        for item in inventory['services']:
            key = item['guest_key'] + ':' + item['guest_identity'] + ':' + item['address']
            group = grouped.setdefault(key, {**item, 'key': key, 'services': [], 'enabled_count': 0})
            group['services'].append(item)
            group['enabled_count'] += int(item['enabled'] and not item['stale'])
        groups = sorted(grouped.values(), key=lambda group: group['guest_name'].lower())
        for group in groups:
            group['services'].sort(key=lambda item: (not item['enabled'], item['port']))
        return template(request, 'internal.html', session=session, inventory=inventory,
                        groups=groups, service_actions=actions, error=error, status_code=status_code)

    @app.get('/internal', response_class=HTMLResponse)
    async def internal_services(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        return await internal_page(request, session)

    async def access_inventory():
        try:
            return await run_in_threadpool(services.inventory)
        except ServiceClientError:
            return {'services': [], 'errors': ['Инвентаризация недоступна. Сохранённые привязки доступны.']}

    @app.get('/access', response_class=HTMLResponse)
    async def access_get(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        inventory = await access_inventory()
        links = database.access_links()
        rows = access_rows(inventory, links, vault_url)
        query = request.query_params.get('q', '').strip()[:120]
        scope = request.query_params.get('scope', 'accounts')
        if scope not in ('accounts', 'unlinked', 'ssh', 'all'):
            scope = 'accounts'
        groups = access_groups(rows)
        for group in groups:
            for row in group['rows']:
                row['visible'] = (scope == 'all' or (scope == 'accounts' and bool(row['id']))
                    or (scope == 'unlinked' and row['method'] != 'none' and not row['vault_item_id'])
                    or (scope == 'ssh' and row['target'] and row['target']['name'] == 'SSH'))
                row['visible'] = bool(row['visible'] and query.casefold() in row['search_text'])
            group['visible'] = any(row['visible'] for row in group['rows'])
        return template(request, 'access.html', session=session, rows=rows, query=query,
                        groups=groups, scope=scope, visible_count=sum(row['visible'] for row in rows),
                        inventory=inventory, vault_url=vault_url, links_count=len(links),
                        linked_count=sum(bool(link['vault_item_id']) for link in links))

    async def access_form(request, session, identifier='', values=None, error=None, status_code=200):
        existing = database.access_link(identifier) if identifier else None
        if identifier and not existing:
            return HTMLResponse('Привязка не найдена.', status_code=404)
        inventory = await access_inventory()
        values = values if values is not None else existing or {
            'target_id': request.query_params.get('target_id', ''), 'method': 'password',
            'title': '', 'username': '', 'vault_item_id': '',
        }
        targets = {item['id']: item for item in inventory['services']}
        if not existing and not values.get('title') and values.get('target_id') in targets:
            target = targets[values['target_id']]
            values['title'] = target['guest_name'] + ' / ' + target['name']
        return template(request, 'access_form.html', session=session, values=values,
                        existing=existing, identifier=identifier, targets=targets, methods=METHODS,
                        vault_url=vault_url, inventory=inventory, error=error, status_code=status_code,
                        csrf=csrf_token(session.csrf_secret, 'access:save:' + identifier),
                        remove_csrf=csrf_token(session.csrf_secret, 'access:remove:' + identifier))

    @app.get('/access/new', response_class=HTMLResponse)
    @app.get('/access/edit/{identifier}', response_class=HTMLResponse)
    async def access_edit_get(request: Request, identifier: str = ''):
        session = session_for(request)
        if session is None:
            return login_redirect()
        return await access_form(request, session, identifier)

    @app.post('/access/new', response_class=HTMLResponse)
    @app.post('/access/edit/{identifier}', response_class=HTMLResponse)
    async def access_edit_post(request: Request, identifier: str = ''):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        error = csrf_or_error(session, 'access:save:' + identifier, form.get('csrf_token', ''))
        if error:
            return await access_form(request, session, identifier, error=error, status_code=400)
        existing = database.access_link(identifier) if identifier else None
        if identifier and not existing:
            return HTMLResponse('Привязка не найдена.', status_code=404)
        inventory = await access_inventory()
        try:
            values = validate_link(form, {item['id']: item for item in inventory['services']}, existing)
            if values['vault_item_id'] and not vault_url:
                raise ValueError('Адрес хранилища ещё не настроен администратором.')
        except ValueError as exc:
            # Never echo submitted fields: they may contain an accidentally pasted password.
            return await access_form(request, session, identifier, error=str(exc), status_code=400)
        saved_id = identifier or str(uuid4())
        if not database.save_access_link(saved_id, values, form.get('expected', '') if existing else None):
            return await access_form(request, session, identifier,
                                     error='Привязка уже изменена. Проверьте актуальные данные.', status_code=409)
        database.audit(session.user_id, 'link', 'access', saved_id, 'success')
        return RedirectResponse('/access?notice=access-saved', status_code=303)

    @app.post('/access/remove/{identifier}')
    async def access_remove(request: Request, identifier: str):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        if (not valid_csrf(session.csrf_secret, 'access:remove:' + identifier, form.get('csrf_token', ''))
                or form.get('confirm') != 'yes'):
            return await access_form(request, session, identifier,
                                     error='Подтвердите удаление привязки.', status_code=400)
        if not database.remove_access_link(identifier, form.get('expected', '')):
            return await access_form(request, session, identifier,
                                     error='Привязка уже изменена. Обновите страницу.', status_code=409)
        database.audit(session.user_id, 'unlink', 'access', identifier, 'success')
        return RedirectResponse('/access?notice=access-removed', status_code=303)

    @app.post('/internal/toggle', response_class=HTMLResponse)
    async def internal_toggle(request: Request):
        session = session_for(request)
        if session is None:
            return login_redirect()
        form = await form_data(request)
        identifier = form.get('id', '')
        error = csrf_or_error(session, f'service:toggle:{identifier}', form.get('csrf_token', ''))
        if error:
            return await internal_page(request, session, error, 400)
        if form.get('enabled') not in {'true', 'false'} or form.get('expected_enabled') not in {'true', 'false'}:
            return await internal_page(request, session, 'Некорректное состояние переключателя.', 400)
        enabled = form['enabled'] == 'true'
        try:
            await run_in_threadpool(services.toggle, identifier, enabled,
                                   form['expected_enabled'] == 'true')
        except ServiceClientError as exc:
            database.audit(session.user_id, 'toggle', 'private-service', identifier, 'failed', str(exc))
            return await internal_page(request, session, str(exc), 409)
        database.audit(session.user_id, 'enable' if enabled else 'disable',
                       'private-service', identifier, 'success', 'Tailscale gateway')
        notice = 'service-enabled' if enabled else 'service-disabled'
        return RedirectResponse('/internal?notice=' + notice, status_code=303)

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
                changed = await password_operation(database.change_password,
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
