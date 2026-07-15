from __future__ import annotations

import fcntl
import ipaddress
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


DOMAIN_RE = re.compile(
    r"^(?:\*\.)?(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class RouterError(RuntimeError):
    pass


@dataclass(frozen=True)
class RouterSettings:
    routes_file: Path = Path("/etc/domain-router/routes.yml")
    caddy_file: Path = Path("/etc/caddy/Caddyfile")
    lock_file: Path = Path("/run/lock/domain-router.lock")
    caddy_listen: str = ":8080"
    caddy_bind: str = "127.0.0.1"
    allowed_domain: str = "content-factory-vps.win"
    upstream_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
        ipaddress.ip_network("10.77.0.0/24"),
    )


def default_settings() -> RouterSettings:
    networks = tuple(
        ipaddress.ip_network(item.strip())
        for item in os.environ.get("DOMAIN_ROUTER_UPSTREAM_NETWORKS", "10.77.0.0/24").split(",")
        if item.strip()
    )
    return RouterSettings(
        routes_file=Path(os.environ.get("DOMAIN_ROUTER_ROUTES_FILE", "/etc/domain-router/routes.yml")),
        caddy_file=Path(os.environ.get("DOMAIN_ROUTER_CADDY_FILE", "/etc/caddy/Caddyfile")),
        lock_file=Path(os.environ.get("DOMAIN_ROUTER_LOCK_FILE", "/run/lock/domain-router.lock")),
        allowed_domain=os.environ.get("DOMAIN_ROUTER_ALLOWED_DOMAIN", "content-factory-vps.win"),
        upstream_networks=networks,
    )


def run_command(
    command: list[str], *, capture_output: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=capture_output,
    )


def normalize_domain(value: str, allowed_domain: str | None = None) -> str:
    domain = value.strip().lower().rstrip(".")
    if not DOMAIN_RE.fullmatch(domain):
        raise RouterError(f"Invalid domain: {value}")
    if allowed_domain:
        zone = allowed_domain.strip().lower().rstrip(".")
        comparable = domain.removeprefix("*.")
        if comparable != zone and not comparable.endswith(f".{zone}"):
            raise RouterError(f"Domain must belong to {zone}")
    return domain


def normalize_upstream(
    value: str,
    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str:
    upstream = value.strip().rstrip("/")
    parsed = urlparse(upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RouterError("Upstream must be an http:// or https:// URL")
    if parsed.username or parsed.password:
        raise RouterError("Upstream credentials are not allowed")
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        raise RouterError("Upstream must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RouterError(f"Invalid upstream port: {exc}") from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise RouterError("Upstream port must be between 1 and 65535")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise RouterError("Upstream host must be an IP address") from exc
    if not any(address in network for network in allowed_networks):
        raise RouterError("Upstream address is outside the allowed networks")
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"{parsed.scheme}://{host}:{port}"


def dump_routes(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=False)


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class RouteStore:
    def __init__(
        self,
        settings: RouterSettings | None = None,
        runner: CommandRunner = run_command,
    ) -> None:
        self.settings = settings or default_settings()
        self.runner = runner

    def _normalize_route(self, route: Any) -> dict[str, Any]:
        if not isinstance(route, dict):
            raise RouterError("Each route must be an object")
        if not isinstance(route.get("enabled", True), bool):
            raise RouterError("Route enabled state must be boolean")
        return {
            "domain": normalize_domain(str(route.get("domain", "")), self.settings.allowed_domain),
            "upstream": normalize_upstream(
                str(route.get("upstream", "")), self.settings.upstream_networks
            ),
            "enabled": route.get("enabled", True),
        }

    def load(self) -> dict[str, Any]:
        if not self.settings.routes_file.exists():
            return {"version": 1, "routes": []}
        try:
            raw = yaml.safe_load(self.settings.routes_file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RouterError(f"Invalid routes file: {exc}") from exc
        if raw.get("version") != 1 or not isinstance(raw.get("routes"), list):
            raise RouterError(f"Invalid routes file: {self.settings.routes_file}")
        routes = [self._normalize_route(route) for route in raw["routes"]]
        domains = [route["domain"] for route in routes]
        if len(domains) != len(set(domains)):
            raise RouterError("Routes file contains duplicate domains")
        return {"version": 1, "routes": routes}

    def list_routes(self) -> list[dict[str, Any]]:
        return sorted(self.load()["routes"], key=lambda item: item["domain"])

    def render_caddy(self, data: dict[str, Any]) -> str:
        lines = [
            "{",
            "\tauto_https off",
            "}",
            "",
            f"{self.settings.caddy_listen} {{",
            f"\tbind {self.settings.caddy_bind}",
            "",
        ]
        enabled = [route for route in data["routes"] if route.get("enabled", True)]
        for index, route in enumerate(enabled):
            lines.extend(
                [
                    f"\t@route_{index} host {route['domain']}",
                    f"\thandle @route_{index} {{",
                    f"\t\treverse_proxy {route['upstream']}",
                    "\t}",
                    "",
                ]
            )
        lines.extend(
            ["\thandle {", '\t\trespond "Unknown domain" 404', "\t}", "}", ""]
        )
        return "\n".join(lines)

    def _validate_caddy(self, content: str) -> None:
        fd, temp_name = tempfile.mkstemp(prefix="domain-router-", suffix=".Caddyfile")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            result = self.runner(
                ["caddy", "validate", "--config", temp_name, "--adapter", "caddyfile"],
                capture_output=True,
            )
            if result.returncode:
                raise RouterError((result.stderr or result.stdout).strip())
        finally:
            os.unlink(temp_name)

    def _reload_caddy(self) -> subprocess.CompletedProcess[str]:
        return self.runner(["systemctl", "reload", "caddy"], capture_output=True)

    def _find(self, data: dict[str, Any], domain: str) -> dict[str, Any]:
        for route in data["routes"]:
            if route["domain"] == domain:
                return route
        raise RouterError(f"Route not found: {domain}")

    def _save_and_reload(self, data: dict[str, Any]) -> None:
        caddy_content = self.render_caddy(data)
        self._validate_caddy(caddy_content)
        old_routes = self.settings.routes_file.read_bytes() if self.settings.routes_file.exists() else None
        old_caddy = self.settings.caddy_file.read_bytes() if self.settings.caddy_file.exists() else None
        atomic_write(self.settings.routes_file, dump_routes(data))
        atomic_write(self.settings.caddy_file, caddy_content)
        result = self._reload_caddy()
        if not result.returncode:
            return
        if old_routes is None:
            self.settings.routes_file.unlink(missing_ok=True)
        else:
            atomic_write(self.settings.routes_file, old_routes.decode("utf-8"))
        if old_caddy is None:
            self.settings.caddy_file.unlink(missing_ok=True)
        else:
            atomic_write(self.settings.caddy_file, old_caddy.decode("utf-8"))
        rollback = self._reload_caddy()
        message = (result.stderr or result.stdout).strip()
        if rollback.returncode:
            message = f"{message}; rollback reload also failed"
        raise RouterError(f"Caddy reload failed: {message}")

    def _mutate(self, operation: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        self.settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.lock_file.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = self.load()
            operation(data)
            self._save_and_reload(data)
            return data

    def add(self, domain: str, upstream: str) -> dict[str, Any]:
        normalized_domain = normalize_domain(domain, self.settings.allowed_domain)
        normalized_upstream = normalize_upstream(upstream, self.settings.upstream_networks)

        def operation(data: dict[str, Any]) -> None:
            if any(route["domain"] == normalized_domain for route in data["routes"]):
                raise RouterError(f"Route already exists: {normalized_domain}")
            data["routes"].append(
                {"domain": normalized_domain, "upstream": normalized_upstream, "enabled": True}
            )

        self._mutate(operation)
        return {"domain": normalized_domain, "upstream": normalized_upstream, "enabled": True}

    def update(self, domain: str, upstream: str) -> dict[str, Any]:
        normalized_domain = normalize_domain(domain, self.settings.allowed_domain)
        normalized_upstream = normalize_upstream(upstream, self.settings.upstream_networks)

        def operation(data: dict[str, Any]) -> None:
            self._find(data, normalized_domain)["upstream"] = normalized_upstream

        data = self._mutate(operation)
        return self._find(data, normalized_domain)

    def remove(self, domain: str) -> None:
        normalized_domain = normalize_domain(domain, self.settings.allowed_domain)

        def operation(data: dict[str, Any]) -> None:
            self._find(data, normalized_domain)
            data["routes"] = [
                route for route in data["routes"] if route["domain"] != normalized_domain
            ]

        self._mutate(operation)

    def toggle(self, domain: str, enabled: bool) -> dict[str, Any]:
        normalized_domain = normalize_domain(domain, self.settings.allowed_domain)

        def operation(data: dict[str, Any]) -> None:
            self._find(data, normalized_domain)["enabled"] = enabled

        data = self._mutate(operation)
        return self._find(data, normalized_domain)

    def render(self) -> None:
        content = self.render_caddy(self.load())
        self._validate_caddy(content)
        atomic_write(self.settings.caddy_file, content)

    def reload(self) -> None:
        content = self.settings.caddy_file.read_text(encoding="utf-8")
        self._validate_caddy(content)
        result = self._reload_caddy()
        if result.returncode:
            raise RouterError((result.stderr or result.stdout).strip() or "Caddy reload failed")

    def status(self) -> dict[str, Any]:
        services: dict[str, str] = {}
        for service in ("caddy", "cloudflared", "tailscaled"):
            result = self.runner(["systemctl", "is-active", service], capture_output=True)
            services[service] = (result.stdout or "").strip() or "not-installed"
        return {"services": services, "routes": len(self.load()["routes"])}

    def test_domain(self, domain: str) -> dict[str, Any]:
        normalized = normalize_domain(domain, self.settings.allowed_domain)
        result = self.runner(
            [
                "curl",
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                "--header",
                f"Host: {normalized}",
                "http://127.0.0.1:8080/",
            ],
            capture_output=True,
        )
        if result.returncode:
            raise RouterError((result.stderr or "Request failed").strip())
        return {"domain": normalized, "status_code": int(result.stdout)}

    def probe_upstream(self, upstream: str) -> dict[str, Any]:
        normalized = normalize_upstream(upstream, self.settings.upstream_networks)
        result = self.runner(
            [
                "curl",
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--connect-timeout",
                "3",
                "--max-time",
                "5",
                "--write-out",
                "%{http_code} %{time_total}",
                normalized,
            ],
            capture_output=True,
        )
        if result.returncode:
            raise RouterError((result.stderr or "Upstream is unavailable").strip())
        status, elapsed = result.stdout.strip().split(maxsplit=1)
        return {"upstream": normalized, "status_code": int(status), "elapsed": float(elapsed)}
