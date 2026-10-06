from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from accounts import mfa


class MFAMiddleware:
    """
    Hold privileged users at the email-code step until they verify.

    Runs after AuthenticationMiddleware. Requests authenticated by API token
    are not affected (DRF authenticates those inside the view, so request.user
    is anonymous here); API tokens can only be created by verified admins.
    """

    EXEMPT_PREFIXES = (
        '/accounts/mfa/',
        '/accounts/logout/',
        '/static/',
        '/media/',
        '/health/',
        '/feedback/',
        '/favicon',
        '/branding/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if (
            user is not None
            and user.is_authenticated
            and not request.path.startswith(self.EXEMPT_PREFIXES)
            and not mfa.is_verified(request)
            and mfa.user_needs_mfa(user)
        ):
            if request.path.startswith('/api/'):
                return JsonResponse(
                    {'detail': 'Multi-factor verification required. Sign in through the web app first.'},
                    status=403,
                )
            url = reverse('mfa_verify')
            if request.method == 'GET' and request.path != reverse('admin_dashboard'):
                url += '?' + urlencode({'next': request.get_full_path()})
            return redirect(url)
        return self.get_response(request)
