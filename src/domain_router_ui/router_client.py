from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


class RouterClientError(RuntimeError):
    pass


@dataclass
class RouterClient:
    command: tuple[str, ...]

    def _run(self, *arguments: str, json_output: bool = False) -> Any:
        command = [*self.command, *arguments]
        if json_output:
            command.append("--json")
        result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=15)
        if result.returncode:
            message = (result.stderr or result.stdout or "Route operation failed").strip()
            message = message.removeprefix("Error: ")
            raise RouterClientError(message[:500])
        if not json_output:
            return result.stdout.strip()
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RouterClientError("Invalid response from domain-router CLI") from exc

    def routes(self) -> list[dict[str, Any]]:
        return self._run("list", json_output=True)

    def status(self) -> dict[str, Any]:
        return self._run("status", json_output=True)

    def add(self, domain: str, upstream: str) -> dict[str, Any]:
        return self._run("add", domain, upstream, json_output=True)

    def update(self, domain: str, upstream: str) -> dict[str, Any]:
        return self._run("set", domain, upstream, json_output=True)

    def toggle(self, domain: str, enabled: bool) -> dict[str, Any]:
        return self._run("enable" if enabled else "disable", domain, json_output=True)

    def remove(self, domain: str) -> None:
        self._run("remove", domain, json_output=True)

    def probe(self, upstream: str) -> dict[str, Any]:
        return self._run("probe", upstream, json_output=True)
