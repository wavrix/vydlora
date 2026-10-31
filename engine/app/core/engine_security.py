"""Authenticated loopback guard and lifecycle hooks for Lora 1.

Vydlora forwards the engine token on HTTP and WebSocket requests. Historical
wire keys remain compatible with existing clients.
"""

from __future__ import annotations

import http.cookies
from typing import Awaitable, Callable
from .compat import LEGACY_TOKEN_HEADER, LEGACY_TOKEN_COOKIE

Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]

COOKIE_NAME = "lora1_token"
ENTER_PATH = "/__enter"
SHUTDOWN_PATH = "/__engine/shutdown"


def wrap(app, token: str, on_shutdown: Callable[[], None]):
    """Wrap ``app`` with the engine guard. ``token`` empty → guard disabled."""
    return _EngineGuard(app, token or "", on_shutdown)


class _EngineGuard:
    def __init__(self, app, token: str, on_shutdown: Callable[[], None]) -> None:
        self.app = app
        self.token = token
        self.on_shutdown = on_shutdown

    async def __call__(self, scope: dict, receive: Receive, send: Send) -> None:
        typ = scope["type"]
        if typ == "lifespan":
            # Pass startup/shutdown lifespan events straight through to FastAPI.
            return await self.app(scope, receive, send)

        path = scope.get("path", "")

        # Graceful shutdown - token via header, available in dev and prod.
        if typ == "http" and path == SHUTDOWN_PATH:
            if self.token and not self._header_ok(scope):
                return await _json(send, 403, b'{"detail":"forbidden"}')
            await _json(send, 200, b'{"ok":true}')
            try:
                self.on_shutdown()
            except Exception:
                pass
            return

        # No token configured (development) → behave like the bare app.
        if not self.token:
            return await self.app(scope, receive, send)

        # One-time cookie handshake initiated by the Vydlora supervisor.
        if typ == "http" and path == ENTER_PATH:
            return await self._enter(scope, send)

        # Everything else requires the cookie (browser) or the header (supervisor).
        if not (self._cookie_ok(scope) or self._header_ok(scope)):
            if typ == "websocket":
                await receive()  # consume the websocket.connect event
                return await send({"type": "websocket.close", "code": 4403})
            return await _json(send, 401, b'{"detail":"unauthorized"}')

        return await self.app(scope, receive, send)

    async def _enter(self, scope: dict, send: Send) -> None:
        from urllib.parse import parse_qs

        qs = parse_qs(scope.get("query_string", b"").decode("latin1"))
        supplied = (qs.get("token", [""])[0]).strip()
        if supplied != self.token:
            return await _json(send, 403, b'{"detail":"forbidden"}')

        cookie = f"{COOKIE_NAME}={self.token}; Path=/; HttpOnly; SameSite=Strict".encode("latin1")
        await send(
            {
                "type": "http.response.start",
                "status": 302,
                "headers": [(b"location", b"/"), (b"set-cookie", cookie)],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    def _header_ok(self, scope: dict) -> bool:
        for key, value in scope.get("headers", []):
            if key in (b"x-lora1-token", LEGACY_TOKEN_HEADER):
                return value.decode("latin1").strip() == self.token
            if key == b"authorization":
                raw = value.decode("latin1").strip()
                if raw.lower().startswith("bearer "):
                    return raw[7:].strip() == self.token
        return False

    def _cookie_ok(self, scope: dict) -> bool:
        for key, value in scope.get("headers", []):
            if key == b"cookie":
                jar = http.cookies.SimpleCookie()
                try:
                    jar.load(value.decode("latin1"))
                except Exception:
                    return False
                morsel = jar.get(COOKIE_NAME) or jar.get(LEGACY_TOKEN_COOKIE)
                return bool(morsel and morsel.value == self.token)
        return False


async def _json(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
