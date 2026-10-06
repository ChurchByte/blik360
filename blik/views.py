"""
Core views for Lead360 application
"""
from functools import lru_cache

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render, redirect


def health_check(request):
    """Health check endpoint for monitoring"""
    return JsonResponse({
        'status': 'healthy',
        'service': 'blik',
        'version': '0.1.0'
    })


def home(request):
    """Home page - redirect authenticated users to dashboard, others to login"""
    if request.user.is_authenticated:
        return redirect('admin_dashboard')
    return redirect('login')


def handler404(request, exception):
    """Custom 404 error handler"""
    return render(request, 'landing/404.html', status=404)


def handler500(request):
    """Custom 500 error handler"""
    return render(request, 'landing/500.html', status=500)


@lru_cache(maxsize=1)
def _license_text():
    """Full AGPL-3.0 text from the LICENSE file shipped with the source."""
    try:
        return (settings.BASE_DIR / 'LICENSE').read_text(encoding='utf-8')
    except OSError:
        return ''


def license_page(request):
    """
    Public License & Credits page (linked from every page footer).

    Credits the original Blik authors, states that Lead360 is a modified
    version of Blik, links to the Corresponding Source (AGPL-3.0 s.13) and
    displays the full license text (AGPL-3.0 "Appropriate Legal Notices").
    """
    return render(request, 'license.html', {'license_text': _license_text()})
