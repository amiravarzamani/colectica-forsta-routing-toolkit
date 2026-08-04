from __future__ import annotations

import re

from asgiref.sync import sync_to_async
from django.utils import timezone
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

TOKEN_PATH_RE = re.compile(r"^/t/(?P<token>[^/]+)(?P<rest>/.*)?$")


class TokenAuthMiddleware:
    """
    Rewrites /t/<token>/mcp -> /mcp after validating <token> against
    McpAccessToken, so each colleague gets their own connector URL (see
    mcp_server.models.McpAccessToken) rather than one shared secret. Requests
    to any other path (including a bare /mcp) are rejected -- there is no
    unauthenticated way to reach the underlying MCP app.

    A raw ASGI middleware rather than starlette.middleware.base.BaseHTTPMiddleware,
    since the latter buffers the response body and breaks the streamable-http
    transport's long-lived SSE responses.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        match = TOKEN_PATH_RE.match(scope["path"])
        if not match:
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return

        token = match.group("token")
        rest = match.group("rest") or "/"

        if not await self._consume_valid_token(token):
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            return

        scope = dict(scope)
        scope["path"] = rest
        scope["raw_path"] = rest.encode("utf-8")
        await self.app(scope, receive, send)

    @staticmethod
    @sync_to_async(thread_sensitive=True)
    def _consume_valid_token(token: str) -> bool:
        # mcp_server.models imported lazily so importing this module doesn't
        # require Django apps to already be loaded (matters for management
        # command import ordering).
        from mcp_server.models import McpAccessToken

        updated = McpAccessToken.objects.filter(
            token=token,
            revoked_at__isnull=True,
        ).update(last_used_at=timezone.now())
        return updated > 0
