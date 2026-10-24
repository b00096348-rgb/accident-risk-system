"""Phone-number authentication with single-use codes and bearer tokens (FR1, FR2).

Standard library only. Codes and tokens are stored as SHA-256 digests, never in
clear text, and compared with a constant-time function. There is no SMS gateway
in the research build: `request_code` returns the code when `expose_codes` is
set, which is how the pilot signs in. Wire a real gateway before any deployment
outside the pilot.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

from accident_risk.api.db import Database, normalise_phone

CODE_TTL_SECONDS = 300.0
TOKEN_TTL_SECONDS = 12 * 3600.0
MAX_CODE_ATTEMPTS = 5


def _digest(value: str) -> str:
    """Hash a secret for storage; never persist the clear-text value."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """The authenticated caller behind a request."""

    user_id: int
    phone: str
    name: str
    role: str


class AuthError(Exception):
    """Authentication failed. Carries the HTTP status the route should return."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class AuthService:
    """Issues login codes and bearer tokens against the SQLite store."""

    def __init__(self, database: Database, expose_codes: bool = True) -> None:
        self.db = database
        self.expose_codes = expose_codes

    def request_code(self, phone: str) -> dict[str, Any]:
        """Create a single-use login code for a registered, active user.

        Returns the code itself only when `expose_codes` is set, which the
        research build does because no SMS gateway is configured.
        """
        phone = normalise_phone(phone)
        user = self.db.get_user_by_phone(phone)
        if user is None or not user["active"]:
            # Do not reveal whether the number is registered.
            return {"sent": True, "delivery": "none"}
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO auth_codes(phone, code_hash, expires_at, attempts) "
                "VALUES (?, ?, ?, 0) ON CONFLICT(phone) DO UPDATE SET "
                "code_hash = excluded.code_hash, expires_at = excluded.expires_at, attempts = 0",
                (phone, _digest(code), time.time() + CODE_TTL_SECONDS),
            )
        result: dict[str, Any] = {
            "sent": True,
            "delivery": "development" if self.expose_codes else "sms",
            "expires_in": int(CODE_TTL_SECONDS),
        }
        if self.expose_codes:
            result["code"] = code
            result["warning"] = "Development login: no SMS gateway is configured."
        return result

    def verify_code(self, phone: str, code: str) -> dict[str, Any]:
        """Exchange a valid code for a bearer token. Raises AuthError otherwise."""
        phone = normalise_phone(phone)
        now = time.time()
        # Decide first, mutate second, raise last. A write and a raise must never
        # share a transaction here: connect() rolls back on exception, which would
        # silently undo the failed-attempt counter and defeat the lockout entirely.
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM auth_codes WHERE phone = ?", (phone,)
            ).fetchone()
            state = dict(row) if row else None

        if state is None:
            raise AuthError("Request a login code first")

        failure: AuthError | None = None
        if state["expires_at"] < now:
            failure = AuthError("That code has expired; request a new one")
        elif state["attempts"] >= MAX_CODE_ATTEMPTS:
            failure = AuthError("Too many attempts; request a new code", status=429)
        elif not hmac.compare_digest(state["code_hash"], _digest(code.strip())):
            failure = AuthError("That code is not correct")

        with self.db.connect() as connection:
            if failure is None or failure.status == 429 or state["expires_at"] < now:
                # Success, lockout and expiry all retire the code.
                connection.execute("DELETE FROM auth_codes WHERE phone = ?", (phone,))
            else:
                connection.execute(
                    "UPDATE auth_codes SET attempts = attempts + 1 WHERE phone = ?", (phone,)
                )

        if failure is not None:
            raise failure

        user = self.db.get_user_by_phone(phone)
        if user is None or not user["active"]:
            raise AuthError("This account is not active", status=403)
        token = secrets.token_urlsafe(32)
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO tokens(token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (_digest(token), user["id"], now, now + TOKEN_TTL_SECONDS),
            )
        return {
            "token": token,
            "expires_in": int(TOKEN_TTL_SECONDS),
            "user": {
                "id": user["id"],
                "name": user["name"],
                "phone": user["phone"],
                "role": user["role"],
            },
        }

    def resolve(self, token: str | None) -> Principal:
        """Resolve a bearer token to its principal. Raises AuthError if invalid."""
        if not token:
            raise AuthError("Sign in to continue")
        now = time.time()
        with self.db.connect() as connection:
            connection.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
            row = connection.execute(
                "SELECT t.user_id, u.phone, u.name, u.role, u.active FROM tokens t "
                "JOIN users u ON u.id = t.user_id WHERE t.token_hash = ? AND t.expires_at >= ?",
                (_digest(token), now),
            ).fetchone()
        if row is None:
            raise AuthError("Your session has expired; sign in again")
        if not row["active"]:
            raise AuthError("This account is not active", status=403)
        return Principal(
            user_id=int(row["user_id"]), phone=row["phone"], name=row["name"], role=row["role"]
        )

    def revoke(self, token: str) -> None:
        """Sign a user out by deleting their token."""
        with self.db.connect() as connection:
            connection.execute("DELETE FROM tokens WHERE token_hash = ?", (_digest(token),))


def bearer_from_header(header: str | None) -> str | None:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()
