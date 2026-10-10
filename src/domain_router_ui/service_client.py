from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess


class ServiceClientError(RuntimeError):
    pass


@dataclass
class ServiceClient:
    command: tuple[str, ...]

    def _request(self, payload: dict, *, timeout: int = 15) -> dict:
        if not self.command:
            raise ServiceClientError('Реестр сервисов не подключён.')
        try:
            result = subprocess.run(self.command, input=json.dumps(payload), text=True,
                                    capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceClientError('Сервер реестра недоступен. Изменения не подтверждены.') from exc
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ServiceClientError('Не удалось получить ответ сервера реестра.') from exc
        if result.returncode or response.get('error'):
            raise ServiceClientError(str(response.get('error', 'Операция не подтверждена.'))[:300])
        return response

    def inventory(self) -> dict:
        return self._request({'action': 'list'})

    def toggle(self, identifier: str, enabled: bool, expected_enabled: bool) -> dict:
        return self._request({'action': 'toggle', 'id': identifier, 'enabled': enabled,
                              'expected_enabled': expected_enabled})

    def integrations(self) -> dict:
        return self._request({'action': 'integrations-status'}, timeout=25)

    def integration_action(self, target: str, operation: dict) -> dict:
        return self._request({'action': 'integration-action', 'target': target,
                              'operation': operation}, timeout=80)
