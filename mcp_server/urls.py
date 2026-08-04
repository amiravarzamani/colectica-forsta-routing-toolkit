from django.urls import path

from mcp_server.views import (
    mcp_token_generate_view,
    mcp_token_list_view,
    mcp_token_revoke_view,
)

app_name = "mcp_server"

urlpatterns = [
    path("", mcp_token_list_view, name="token_list"),
    path("generate/", mcp_token_generate_view, name="token_generate"),
    path("<int:token_id>/revoke/", mcp_token_revoke_view, name="token_revoke"),
]
