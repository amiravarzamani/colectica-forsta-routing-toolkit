from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.middleware.csrf import rotate_token
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST


@require_http_methods(["GET", "POST"])
def questionnaire_login_view(request):
    if request.user.is_authenticated:
        return redirect("flowise_questionnaire:module_list")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        user = authenticate(request, username=username, password=password)

        if user is None:
            messages.error(request, "Invalid username or password.")
        else:
            login(request, user)
            rotate_token(request)

            next_url = request.GET.get("next")
            if next_url:
                return redirect(next_url)

            return redirect("flowise_questionnaire:module_list")

    return render(request, "flowise_questionnaire/login.html")


@login_required
@require_POST
def questionnaire_logout_view(request):
    logout(request)
    rotate_token(request)
    return redirect("flowise_questionnaire:login")