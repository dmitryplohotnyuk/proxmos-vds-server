from __future__ import annotations

import ipaddress
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from domain_router.core import (
    RouteStore,
    RouterError,
    RouterSettings,
    normalize_domain,
    normalize_upstream,
)


class FakeRunner:
    def __init__(self, reload_codes: list[int] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.reload_codes = list(reload_codes or [])
        self.lock = threading.Lock()

    def __call__(self, command: list[str], *, capture_output: bool = False):
        with self.lock:
            self.commands.append(command)
            if command[:3] == ["systemctl", "reload", "caddy"] and self.reload_codes:
                code = self.reload_codes.pop(0)
                return subprocess.CompletedProcess(command, code, "", "reload failed" if code else "")
        if command[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(command, 0, "active\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def settings(tmp_path: Path) -> RouterSettings:
    return RouterSettings(
        routes_file=tmp_path / "routes.yml",
        caddy_file=tmp_path / "Caddyfile",
        lock_file=tmp_path / "domain-router.lock",
        allowed_domain="content-factory-vps.win",
        upstream_networks=(ipaddress.ip_network("10.77.0.0/24"),),
    )


def test_domain_and_upstream_policy() -> None:
    assert normalize_domain("App.Content-Factory-VPS.win.", "content-factory-vps.win") == (
        "app.content-factory-vps.win"
    )
    assert normalize_upstream(
        "http://10.77.0.10", (ipaddress.ip_network("10.77.0.0/24"),)
    ) == "http://10.77.0.10:80"
    with pytest.raises(RouterError, match="belong"):
        normalize_domain("app.example.com", "content-factory-vps.win")
    with pytest.raises(RouterError, match="outside"):
        normalize_upstream(
            "http://192.168.0.10:80", (ipaddress.ip_network("10.77.0.0/24"),)
        )
    with pytest.raises(RouterError, match="IP address"):
        normalize_upstream(
            "http://internal.local:80", (ipaddress.ip_network("10.77.0.0/24"),)
        )


def test_route_lifecycle_and_render(settings: RouterSettings) -> None:
    runner = FakeRunner()
    store = RouteStore(settings, runner)
    route = store.add("app.content-factory-vps.win", "http://10.77.0.10")
    assert route["upstream"] == "http://10.77.0.10:80"
    assert "host app.content-factory-vps.win" in settings.caddy_file.read_text()
    store.update(route["domain"], "https://10.77.0.11:8443")
    store.toggle(route["domain"], False)
    assert "host app.content-factory-vps.win" not in settings.caddy_file.read_text()
    store.toggle(route["domain"], True)
    store.remove(route["domain"])
    assert store.list_routes() == []


def test_reload_failure_restores_files(settings: RouterSettings) -> None:
    settings.routes_file.write_text("version: 1\nroutes: []\n")
    settings.caddy_file.write_text("old caddy\n")
    runner = FakeRunner(reload_codes=[1, 0])
    store = RouteStore(settings, runner)
    with pytest.raises(RouterError, match="reload failed"):
        store.add("app.content-factory-vps.win", "http://10.77.0.10:80")
    assert settings.routes_file.read_text() == "version: 1\nroutes: []\n"
    assert settings.caddy_file.read_text() == "old caddy\n"


def test_concurrent_additions_are_serialized(settings: RouterSettings) -> None:
    store = RouteStore(settings, FakeRunner())
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                store.add,
                f"app{index}.content-factory-vps.win",
                f"http://10.77.0.{10 + index}:80",
            )
            for index in range(2)
        ]
        for future in futures:
            future.result()
    assert [route["domain"] for route in store.list_routes()] == [
        "app0.content-factory-vps.win",
        "app1.content-factory-vps.win",
    ]
