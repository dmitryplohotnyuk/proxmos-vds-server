from __future__ import annotations

import hashlib
import hmac
import secrets


SESSION_COOKIE = "dr_session"
LOGIN_CSRF_COOKIE = "dr_login_csrf"


def csrf_token(secret: str, action: str) -> str:
    return hmac.new(secret.encode("utf-8"), action.encode("utf-8"), hashlib.sha256).hexdigest()


def valid_csrf(secret: str, action: str, supplied: str) -> bool:
    return bool(supplied) and hmac.compare_digest(csrf_token(secret, action), supplied)


def new_login_csrf() -> str:
    return secrets.token_urlsafe(24)
