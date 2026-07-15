from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from domain_router_ui.app import create_app
from domain_router_ui.config import AppSettings
from domain_router_ui.database import Database
from domain_router_ui.router_client import RouterClientError


class FakeRouterClient:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def routes(self):
        return sorted(self.items, key=lambda item: item["domain"])

    def status(self):
        return {
            "services": {"caddy": "active", "cloudflared": "active", "tailscaled": "active"},
            "routes": len(self.items),
        }

    def add(self, domain: str, upstream: str):
        if not domain.endswith("content-factory-vps.win"):
            raise RouterClientError("Domain must belong to content-factory-vps.win")
        route = {"domain": domain, "upstream": upstream, "enabled": True}
        self.items.append(route)
        return route

    def update(self, domain: str, upstream: str):
        route = next(item for item in self.items if item["domain"] == domain)
        route["upstream"] = upstream
        return route

    def toggle(self, domain: str, enabled: bool):
        route = next(item for item in self.items if item["domain"] == domain)
        route["enabled"] = enabled
        return route

    def remove(self, domain: str):
        self.items = [item for item in self.items if item["domain"] != domain]

    def probe(self, upstream: str):
        return {"upstream": upstream, "status_code": 200, "elapsed": 0.012}


def hidden_token(html: str) -> str:
    matches = re.findall(r'name="csrf_token" value="([^"]+)"', html)
    assert matches
    return matches[-1]


@pytest.fixture
def client(tmp_path: Path):
    database_path = tmp_path / "app.db"
    database = Database(database_path)
    database.initialize()
    database.create_user("admin", "correct horse battery staple")
    fake_router = FakeRouterClient()
    app = create_app(
        AppSettings(database_path=database_path, router_command=("unused",)),
        fake_router,
    )
    with TestClient(app, base_url="https://domain-router.test") as test_client:
        yield test_client, fake_router, database


def login(test_client: TestClient) -> None:
    page = test_client.get("/login")
    assert page.status_code == 200
    response = test_client.post(
        "/login",
        data={
            "csrf_token": hidden_token(page.text),
            "username": "admin",
            "password": "correct horse battery staple",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_authentication_and_security_headers(client) -> None:
    test_client, _, _ = client
    assert test_client.get("/", follow_redirects=False).status_code == 303
    page = test_client.get("/login")
    assert page.headers["x-frame-options"] == "DENY"
    response = test_client.post(
        "/login",
        data={"csrf_token": hidden_token(page.text), "username": "admin", "password": "wrong"},
    )
    assert response.status_code == 401
    assert "Неверный логин или пароль" in response.text
    login(test_client)
    dashboard = test_client.get("/")
    assert dashboard.status_code == 200
    assert "Маршруты" in dashboard.text


def test_route_crud_and_audit(client) -> None:
    test_client, fake_router, database = client
    login(test_client)
    page = test_client.get("/routes/new")
    response = test_client.post(
        "/routes/new",
        data={
            "csrf_token": hidden_token(page.text),
            "domain": "app.content-factory-vps.win",
            "upstream": "http://10.77.0.10:80",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(fake_router.items) == 1
    dashboard = test_client.get("/")
    assert "app.content-factory-vps.win" in dashboard.text
    assert any(event["action"] == "add" for event in database.audit_events())


def test_csrf_rejects_route_change(client) -> None:
    test_client, fake_router, _ = client
    login(test_client)
    response = test_client.post(
        "/routes/new",
        data={
            "csrf_token": "invalid",
            "domain": "app.content-factory-vps.win",
            "upstream": "http://10.77.0.10:80",
        },
    )
    assert response.status_code == 400
    assert fake_router.items == []


def test_password_change_revokes_old_session(client) -> None:
    test_client, _, database = client
    login(test_client)
    page = test_client.get("/account")
    response = test_client.post(
        "/account",
        data={
            "csrf_token": hidden_token(page.text),
            "current_password": "correct horse battery staple",
            "new_password": "another secure password 123",
            "confirm_password": "another secure password 123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert database.verify_user("admin", "another secure password 123") is not None
