from django import template
from django.utils.html import format_html

from core.branding import fit_within, resolve_organization

register = template.Library()


@register.simple_tag(takes_context=True)
def email_logo(context):
    """Render the organization's custom logo at the top of an HTML email.

    Renders nothing when no custom logo is set, so emails keep their default
    look. The organization is taken from `organization`, `reviewee`, `cycle`
    or `user` in the template context.
    """
    org = resolve_organization(
        context.get('organization'),
        context.get('reviewee'),
        context.get('cycle'),
        context.get('user'),
    )
    if org is None or not org.has_logo:
        return ''

    logo = org.brand_images.filter(kind='logo').only('width', 'height').first()
    width, height = fit_within(logo.width, logo.height) if logo else fit_within(0, 0)

    return format_html(
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr><td align="center" style="padding: 24px 20px 8px; text-align: center;">'
        '<img src="{}" alt="{}" width="{}" height="{}" '
        'style="display: inline-block; border: 0; outline: none; text-decoration: none; '
        'width: {}px; height: {}px; max-width: 100%;">'
        '</td></tr></table>',
        org.get_absolute_logo_url(), org.name, width, height, width, height,
    )
