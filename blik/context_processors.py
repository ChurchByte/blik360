"""
Context processors for making settings available in templates.
"""
from django.conf import settings


def stripe_settings(request):
    """
    Make Stripe configuration available in templates.
    Used to conditionally show/hide features based on Stripe setup.
    """
    return {
        'STRIPE_PRICE_ID_SAAS': settings.STRIPE_PRICE_ID_SAAS,
        'STRIPE_PRICE_ID_ENTERPRISE': settings.STRIPE_PRICE_ID_ENTERPRISE,
        'HAS_STRIPE_CONFIGURED': bool(settings.STRIPE_PRICE_ID_SAAS or settings.STRIPE_PRICE_ID_ENTERPRISE),
    }


def branding(request):
    """
    Expose the organization's custom branding to templates:
    `brand_logo_url`, `brand_favicon_url` and `brand_name`. Templates fall
    back to the default Blik branding when a URL is empty.
    """
    from core.branding import resolve_organization

    org = resolve_organization(getattr(request, 'organization', None))
    if org is None:
        return {'brand_logo_url': '', 'brand_favicon_url': '', 'brand_name': ''}
    return {
        'brand_logo_url': org.get_logo_url(),
        'brand_favicon_url': org.get_favicon_url(),
        'brand_name': org.name,
    }
