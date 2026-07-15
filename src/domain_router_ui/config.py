from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppSettings:
    database_path: Path = Path("/var/lib/domain-router-ui/app.db")
    router_command: tuple[str, ...] = (
        "/usr/bin/sudo",
        "-n",
        "/usr/local/bin/domain-router",
    )
    session_hours: int = 12
    login_window_minutes: int = 15
    login_max_attempts: int = 5
    secure_cookies: bool = True


def default_settings() -> AppSettings:
    command = shlex.split(
        os.environ.get(
            "DOMAIN_ROUTER_UI_COMMAND",
            "/usr/bin/sudo -n /usr/local/bin/domain-router",
        )
    )
    return AppSettings(
        database_path=Path(
            os.environ.get("DOMAIN_ROUTER_UI_DATABASE", "/var/lib/domain-router-ui/app.db")
        ),
        router_command=tuple(command),
        session_hours=int(os.environ.get("DOMAIN_ROUTER_UI_SESSION_HOURS", "12")),
        login_window_minutes=int(
            os.environ.get("DOMAIN_ROUTER_UI_LOGIN_WINDOW_MINUTES", "15")
        ),
        login_max_attempts=int(os.environ.get("DOMAIN_ROUTER_UI_LOGIN_MAX_ATTEMPTS", "5")),
        secure_cookies=os.environ.get("DOMAIN_ROUTER_UI_SECURE_COOKIES", "true").lower()
        not in {"0", "false", "no"},
    )
