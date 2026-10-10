from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from domain_router_ui.app import create_app
from domain_router_ui.config import AppSettings
from domain_router_ui.database import Database
from domain_router_services.agent import merge_snapshot
from domain_router_ui.service_client import ServiceClientError


class DemoRouterClient:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = [
            {
                "domain": "app.content-factory-vps.win",
                "upstream": "http://10.77.0.10:80",
                "enabled": True,
            },
            {
                "domain": "api.content-factory-vps.win",
                "upstream": "http://10.77.0.12:8080",
                "enabled": True,
            },
            {
                "domain": "old.content-factory-vps.win",
                "upstream": "http://10.77.0.13:80",
                "enabled": False,
            },
        ]

    def routes(self):
        return sorted(self.items, key=lambda route: route["domain"])

    def status(self):
        return {
            "services": {"caddy": "active", "cloudflared": "active", "tailscaled": "active"},
            "routes": len(self.items),
        }

    def add(self, domain: str, upstream: str):
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
        return {"upstream": upstream, "status_code": 200, "elapsed": 0.018}


database_path = Path(os.environ.get("DOMAIN_ROUTER_DEMO_DB", "/tmp/domain-router-ui-demo.db"))
database = Database(database_path)
database.initialize()
if database.user_count() == 0:
    database.create_user("admin", "domain router demo password")

class DemoServices:
    def __init__(self):
        self.state = merge_snapshot(dict(services=[], aliases={}, revision=0, updated_at=None),
            [dict(kind='lxc', vmid=vmid, identity='demo', name=name, status='running')
             for vmid, name in [(104, 'media'), (106, 'adguard-home'), (110, 'new-service')]],
            {'lxc:104': (['10.77.0.14', '192.168.0.156'], [('tcp', 8096, '*'), ('tcp', 445, '*')]),
             'lxc:106': (['10.77.0.53', '192.168.0.5'], [('tcp', 3000, '*'), ('tcp', 53, '*'), ('udp', 53, '*')]),
             'lxc:110': (['192.168.0.190'], [('tcp', 8080, '*')])})
        for item in self.state['services']:
            item['reachable'] = True

    def inventory(self):
        return self.state

    def integrations(self):
        from test_integrations import FakeIntegrations
        return FakeIntegrations().integrations()

    def integration_action(self, target, operation):
        return {}

    def toggle(self, identifier, enabled, expected_enabled):
        item = next(item for item in self.state['services'] if item['id'] == identifier)
        if item['enabled'] != expected_enabled:
            raise ServiceClientError('State conflict')
        item['enabled'] = enabled


app = create_app(
    AppSettings(database_path=database_path, router_command=("unused",), secure_cookies=False),
    DemoRouterClient(),
    DemoServices(),
)
