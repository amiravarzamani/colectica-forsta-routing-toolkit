import asyncio
import os
from pathlib import Path

from django.core.management.base import BaseCommand

from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware

from mcp_server.auth_middleware import TokenAuthMiddleware
from mcp_server.server import mcp

# The SDK auto-enables DNS-rebinding protection whenever host is 127.0.0.1/localhost,
# allowlisting only Origin: http://127.0.0.1:*  /  http://localhost:* (see
# mcp.server.lowlevel.server.Server.streamable_http_app). Claude Desktop's connector
# validation request carries its own app origin, which never matches that allowlist,
# so every "Add custom connector" attempt was silently rejected with 403 before this
# reached application code. This is a single-user local server already gated by the
# self-signed cert + loopback binding, so disabling the check is the correct fix here
# rather than trying to guess/allowlist Electron's exact origin string.
NO_DNS_REBINDING_PROTECTION = TransportSecuritySettings(enable_dns_rebinding_protection=False)

CERTS_DIR = Path(__file__).resolve().parent.parent.parent / "certs"

DEFAULT_HOST = os.getenv("MCP_SERVER_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("MCP_SERVER_PORT", "8765"))
DEFAULT_CERTFILE = os.getenv("MCP_SERVER_SSL_CERTFILE", str(CERTS_DIR / "cert.pem"))
DEFAULT_KEYFILE = os.getenv("MCP_SERVER_SSL_KEYFILE", str(CERTS_DIR / "key.pem"))


class Command(BaseCommand):
    help = (
        "Run the MCP server exposing read-only Colectica questionnaire tools "
        "over streamable-HTTP only, as its own standalone process on its own port -- "
        "does not touch config/asgi.py, config/wsgi.py, or manage.py runserver. "
        "Serves HTTPS using a local self-signed cert (mcp_server/certs/) if present, "
        "since Claude Desktop's custom connector UI requires an https:// URL."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--host",
            default=DEFAULT_HOST,
            help=f"Host to bind (default: {DEFAULT_HOST}, or $MCP_SERVER_HOST).",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=DEFAULT_PORT,
            help=f"Port to bind (default: {DEFAULT_PORT}, or $MCP_SERVER_PORT).",
        )
        parser.add_argument(
            "--certfile",
            default=DEFAULT_CERTFILE,
            help="TLS certificate PEM file (default: mcp_server/certs/cert.pem).",
        )
        parser.add_argument(
            "--keyfile",
            default=DEFAULT_KEYFILE,
            help="TLS private key PEM file (default: mcp_server/certs/key.pem).",
        )
        parser.add_argument(
            "--no-tls",
            action="store_true",
            help="Force plain HTTP even if a cert is present (e.g. for scripted tests).",
        )

    def handle(self, *args, **options):
        host = options["host"]
        port = options["port"]
        certfile = options["certfile"]
        keyfile = options["keyfile"]

        use_tls = (
            not options["no_tls"]
            and os.path.exists(certfile)
            and os.path.exists(keyfile)
        )

        scheme = "https" if use_tls else "http"
        self.stdout.write(
            f"Starting MCP server on {scheme}://{host}:{port}/t/<token>/mcp "
            "(streamable-http, Colectica-only tools, per-person tokens -- "
            "generate one at /questionnaires/mcp-tokens/)..."
        )

        if not use_tls:
            self.stdout.write(
                self.style.WARNING(
                    "No TLS certificate found -- serving plain HTTP. Claude "
                    "Desktop's custom connector UI requires https://; see "
                    "mcp_server/certs/ for how the dev certificate was generated."
                )
            )

        asyncio.run(
            _serve_streamable_http(
                host=host,
                port=port,
                certfile=certfile if use_tls else None,
                keyfile=keyfile if use_tls else None,
            )
        )


async def _serve_streamable_http(
    *,
    host: str,
    port: int,
    certfile: str | None,
    keyfile: str | None,
) -> None:
    # MCPServer.run()/.run_streamable_http_async() don't expose ssl_keyfile/
    # ssl_certfile or a way to attach extra ASGI middleware, so this replicates
    # its own implementation (streamable_http_app() + uvicorn.Config/Server)
    # with TLS and CORS added.
    import uvicorn

    starlette_app = mcp.streamable_http_app(
        host=host,
        transport_security=NO_DNS_REBINDING_PROTECTION,
    )

    # Claude Desktop's "Add custom connector" flow validates the server from a
    # web view via fetch(), which enforces standard browser CORS -- separate
    # from (and in addition to) the SDK's own Origin-allowlist check disabled
    # above. Without this, the browser silently blocks the preflight/response
    # before our application code ever sees it: "Add" does nothing, no error.
    # allow_origins="*" is fine here (no cookies/credentials involved in MCP).
    starlette_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )

    # Outermost layer: no request reaches CORS/routing/tools without a valid
    # per-person token in the URL path (/t/<token>/mcp) -- see
    # mcp_server.models.McpAccessToken and mcp_server.auth_middleware.
    app = TokenAuthMiddleware(starlette_app)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        log_level=mcp.settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()
