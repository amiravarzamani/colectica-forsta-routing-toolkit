import secrets

from django.conf import settings
from django.db import models


class McpAccessToken(models.Model):
    """
    A per-person credential for the standalone MCP server (see management/commands/
    runmcp.py). The token is embedded directly in that person's connector URL
    (https://<host>/t/<token>/mcp) rather than sent as a header, since MCP clients
    like Claude Desktop's "Add custom connector" UI only take a URL. Validated by
    mcp_server.auth_middleware.TokenAuthMiddleware on every MCP request.
    """

    label = models.CharField(
        max_length=200,
        help_text="Who this token is for, e.g. a colleague's name.",
    )
    token = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mcp_access_tokens_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.label} ({'active' if self.is_active else 'revoked'})"

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(32)

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = self.generate_token()
        super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def connector_url(self) -> str:
        return f"{settings.MCP_PUBLIC_BASE_URL}/t/{self.token}/mcp"
