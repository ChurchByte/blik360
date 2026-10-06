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
    back to the default Lead360 branding when a URL is empty.
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


def product_info(request):
    """
    Expose Lead360 product identity and upstream attribution to templates
    (used by the site footer and the License & Credits page).
    """
    return {
        'PRODUCT_NAME': settings.PRODUCT_NAME,
        'PRODUCT_COMPANY': settings.PRODUCT_COMPANY,
        'PRODUCT_COMPANY_URL': settings.PRODUCT_COMPANY_URL,
        'PRODUCT_SOURCE_URL': settings.PRODUCT_SOURCE_URL,
        'UPSTREAM_NAME': settings.UPSTREAM_NAME,
        'UPSTREAM_AUTHOR': settings.UPSTREAM_AUTHOR,
        'UPSTREAM_URL': settings.UPSTREAM_URL,
        'LICENSE_NAME': settings.LICENSE_NAME,
        'LICENSE_SPDX': settings.LICENSE_SPDX,
    }
