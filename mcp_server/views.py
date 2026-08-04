from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from mcp_server.models import McpAccessToken


def _is_staff(user) -> bool:
    return user.is_staff


@login_required
@user_passes_test(_is_staff)
def mcp_token_list_view(request):
    tokens = McpAccessToken.objects.select_related("created_by").all()
    return render(request, "mcp_server/token_list.html", {"tokens": tokens})


@login_required
@user_passes_test(_is_staff)
@require_POST
def mcp_token_generate_view(request):
    label = request.POST.get("label", "").strip()
    if not label:
        messages.error(request, "Label is required to generate a token.")
        return redirect("mcp_server:token_list")

    McpAccessToken.objects.create(label=label, created_by=request.user)
    messages.success(
        request,
        f'Token generated for "{label}". Copy the URL below and send it to them.',
    )
    return redirect("mcp_server:token_list")


@login_required
@user_passes_test(_is_staff)
@require_POST
def mcp_token_revoke_view(request, token_id):
    token = get_object_or_404(McpAccessToken, id=token_id)
    if token.is_active:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        messages.success(request, f'Revoked token for "{token.label}".')
    return redirect("mcp_server:token_list")
