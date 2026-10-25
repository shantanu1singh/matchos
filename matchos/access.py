"""Optional shared HTTP Basic gate, including browser WebSocket/audio access."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

COOKIE = "matchos_access"
MAX_AGE = 12 * 60 * 60


@dataclass(frozen=True)
class AccessConfig:
    enabled: bool = False
    username: str = ""
    password: str = field(default="", repr=False)
    secure_cookie: bool = True

    @classmethod
    def from_env(cls):
        enabled = os.getenv("MATCHOS_BASIC_AUTH_ENABLED", "0").lower() in {"1", "true"}
        username = os.getenv("MATCHOS_BASIC_AUTH_USERNAME", "").strip()
        password = os.getenv("MATCHOS_BASIC_AUTH_PASSWORD", "")
        if enabled and (not username or ":" in username or len(password) < 16):
            raise ValueError(
                "Basic auth requires a username without a colon and a password of at least 16 characters"
            )
        return cls(enabled, username, password, os.getenv("MATCHOS_BASIC_AUTH_SECURE_COOKIE", "1") != "0")

    def basic_valid(self, authorization):
        try:
            scheme, encoded = authorization.split(" ", 1)
            if scheme.lower() != "basic":
                return False
            username, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeError, binascii.Error):
            return False
        user_ok = secrets.compare_digest(username.encode(), self.username.encode())
        password_ok = secrets.compare_digest(password.encode(), self.password.encode())
        return user_ok and password_ok

    def signature(self, stamp):
        return hmac.new(
            self.password.encode(), f"matchos-access-v1:{self.username}:{stamp}".encode(), hashlib.sha256
        ).hexdigest()

    def ticket(self):
        stamp = str(int(time.time()))
        return f"{stamp}.{self.signature(stamp)}"

    def cookie_valid(self, cookie):
        try:
            jar = SimpleCookie()
            jar.load(cookie)
            value = jar[COOKIE].value
            if len(value) > 128:
                return False
            stamp, signature = value.split(".", 1)
            age = time.time() - int(stamp)
            return 0 <= age <= MAX_AGE and secrets.compare_digest(signature, self.signature(stamp))
        except (CookieError, KeyError, ValueError, TypeError):
            return False


class AccessMiddleware:
    """Pure ASGI middleware preserves agent tracing context and gates WebSockets."""

    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        if scope["type"] == "http" and scope["path"] == "/api/health":
            return await self.app(scope, receive, send)
        config = getattr(self.state, "access", None)
        if config is not None and not config.enabled:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        basic = bool(config and config.basic_valid(headers.get("authorization", "")))
        allowed = bool(config and (basic or config.cookie_valid(headers.get("cookie", ""))))
        if scope["type"] == "websocket":
            origin = headers.get("origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    allowed = (
                        allowed
                        and parsed.scheme in {"http", "https"}
                        and parsed.netloc.lower() == headers.get("host", "").lower()
                    )
                except ValueError:
                    allowed = False
            if not allowed:
                await send({"type": "websocket.close", "code": 1008, "reason": "Authentication required"})
                return
            return await self.app(scope, receive, send)
        if not allowed:
            response = PlainTextResponse(
                "Authentication required",
                status_code=401 if config else 503,
                headers={
                    "WWW-Authenticate": 'Basic realm="MatchOS", charset="UTF-8"',
                    "Cache-Control": "no-store",
                },
            )
            return await response(scope, receive, send)

        async def authenticated_send(message):
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        (key, value)
                        for key, value in message.get("headers", [])
                        if key.lower() != b"cache-control"
                    ],
                }
                message["headers"].append((b"cache-control", b"private, no-store"))
                if basic:
                    # Browsers cannot set a WebSocket Authorization header. A signed,
                    # host-only HttpOnly cookie carries the successful page login.
                    cookie = (
                        f"{COOKIE}={config.ticket()}; Path=/; Max-Age={MAX_AGE}; HttpOnly; SameSite=Strict"
                    )
                    if config.secure_cookie:
                        cookie += "; Secure"
                    message["headers"].append((b"set-cookie", cookie.encode("ascii")))
            await send(message)

        await self.app(scope, receive, authenticated_send)
