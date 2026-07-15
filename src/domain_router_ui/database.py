from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: int
    user_id: int
    username: str
    csrf_secret: str
    expires_at: datetime


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    password_changed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_secret TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_expires_at ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_name TEXT NOT NULL,
                    result TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_created_at ON audit_events(created_at DESC);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id INTEGER PRIMARY KEY,
                    username_hash TEXT NOT NULL,
                    attempted_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS login_attempt_lookup
                    ON login_attempts(username_hash, attempted_at);
                """
            )

    def create_user(self, username: str, password: str) -> int:
        normalized = username.strip().lower()
        if not normalized or len(normalized) > 64:
            raise ValueError("Username must contain between 1 and 64 characters")
        if len(password) < 12:
            raise ValueError("Password must contain at least 12 characters")
        now = timestamp()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users(username, password_hash, password_changed_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (normalized, PASSWORD_HASHER.hash(password), now, now),
            )
            return int(cursor.lastrowid)

    def user_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def verify_user(self, username: str, password: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE AND disabled = 0",
                (username.strip(),),
            ).fetchone()
            if row is None:
                PASSWORD_HASHER.hash(password)
                return None
            try:
                valid = PASSWORD_HASHER.verify(row["password_hash"], password)
            except (VerifyMismatchError, InvalidHashError):
                return None
            if valid and PASSWORD_HASHER.check_needs_rehash(row["password_hash"]):
                connection.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (PASSWORD_HASHER.hash(password), row["id"]),
                )
            return row if valid else None

    def change_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        if len(new_password) < 12:
            raise ValueError("New password must contain at least 12 characters")
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                return False
            try:
                PASSWORD_HASHER.verify(row["password_hash"], current_password)
            except (VerifyMismatchError, InvalidHashError):
                return False
            connection.execute(
                "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                (PASSWORD_HASHER.hash(new_password), timestamp(), user_id),
            )
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return True

    def reset_password(self, username: str, password: str) -> bool:
        if len(password) < 12:
            raise ValueError("Password must contain at least 12 characters")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE users SET password_hash = ?, password_changed_at = ?
                WHERE username = ? COLLATE NOCASE
                """,
                (PASSWORD_HASHER.hash(password), timestamp(), username.strip()),
            )
            if cursor.rowcount:
                connection.execute(
                    "DELETE FROM sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE username = ? COLLATE NOCASE)",
                    (username.strip(),),
                )
            return bool(cursor.rowcount)

    def create_session(self, user_id: int, hours: int) -> tuple[str, AuthenticatedSession]:
        token = secrets.token_urlsafe(32)
        csrf_secret = secrets.token_urlsafe(32)
        now = utc_now()
        expires = now + timedelta(hours=hours)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sessions(user_id, token_hash, csrf_secret, created_at, expires_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, token_hash(token), csrf_secret, timestamp(now), timestamp(expires), timestamp(now)),
            )
            user = connection.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        session = AuthenticatedSession(
            session_id=int(cursor.lastrowid),
            user_id=user_id,
            username=str(user["username"]),
            csrf_secret=csrf_secret,
            expires_at=expires,
        )
        return token, session

    def get_session(self, token: str | None) -> AuthenticatedSession | None:
        if not token:
            return None
        now = utc_now()
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (timestamp(now),))
            row = connection.execute(
                """
                SELECT s.id session_id, s.user_id, s.csrf_secret, s.expires_at, u.username
                FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0
                """,
                (token_hash(token), timestamp(now)),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                (timestamp(now), row["session_id"]),
            )
        return AuthenticatedSession(
            session_id=int(row["session_id"]),
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            csrf_secret=str(row["csrf_secret"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def record_login_failure(self, username: str) -> None:
        key = token_hash(username.strip().lower())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO login_attempts(username_hash, attempted_at) VALUES (?, ?)",
                (key, timestamp()),
            )

    def clear_login_failures(self, username: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM login_attempts WHERE username_hash = ?",
                (token_hash(username.strip().lower()),),
            )

    def login_is_limited(self, username: str, minutes: int, max_attempts: int) -> bool:
        cutoff = timestamp(utc_now() - timedelta(minutes=minutes))
        key = token_hash(username.strip().lower())
        with self.connect() as connection:
            connection.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
            count = connection.execute(
                "SELECT COUNT(*) FROM login_attempts WHERE username_hash = ? AND attempted_at >= ?",
                (key, cutoff),
            ).fetchone()[0]
            return int(count) >= max_attempts

    def audit(
        self,
        user_id: int | None,
        action: str,
        object_type: str,
        object_name: str,
        result: str,
        details: str = "",
    ) -> None:
        safe_details = details.replace("\n", " ")[:500]
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    user_id, action, object_type, object_name, result, details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, action, object_type, object_name[:253], result, safe_details, timestamp()),
            )

    def audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, COALESCE(u.username, 'system') username
                FROM audit_events a LEFT JOIN users u ON u.id = a.user_id
                ORDER BY a.id DESC LIMIT ?
                """,
                (min(max(limit, 1), 500),),
            ).fetchall()
        return [dict(row) for row in rows]
