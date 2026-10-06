"""
Custom organization branding: logo and favicon upload, storage and lookup.

The logo replaces the "Blik" wordmark in the dashboard header, appears at the
top of public pages (login, feedback forms, reports) and at the top of
outgoing HTML emails. The favicon replaces the default browser-tab icon.
"""
import io
import logging

from django.core.exceptions import ValidationError
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from .models import Organization, OrganizationBrandImage

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB
ALLOWED_FORMATS = {'PNG', 'JPEG', 'GIF', 'WEBP'}
# Stored at up to 2x the largest display size so it stays sharp on retina.
MAX_STORED_SIZE = (1200, 400)
# Favicons are padded to a square and stored at this size; browsers scale
# down for tabs, and it is large enough for home-screen / pinned-tab icons.
FAVICON_SIZE = 256

# Display box for emails (CSS px). Outlook ignores max-width/max-height, so the
# email tag emits explicit width/height attributes that fit inside this box.
EMAIL_LOGO_BOX = (240, 64)


def process_logo_upload(uploaded_file, kind=OrganizationBrandImage.LOGO):
    """Validate an uploaded image and normalise it for storage.

    Re-encoding through Pillow strips metadata and anything that isn't pixel
    data. SVG is deliberately not accepted: most email clients won't render
    it, and serving user-supplied SVG from our origin is a script-injection
    risk.

    Returns (bytes, content_type, width, height). Raises ValidationError.
    """
    if uploaded_file is None:
        raise ValidationError('Please choose an image file to upload.')
    if uploaded_file.size > MAX_UPLOAD_BYTES:
        raise ValidationError('Logo must be 2 MB or smaller.')

    try:
        image = Image.open(uploaded_file)
        source_format = image.format
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValidationError('That file is not a valid image. Please upload a PNG, JPG, GIF or WebP.')

    if source_format not in ALLOWED_FORMATS:
        raise ValidationError('Unsupported image type. Please upload a PNG, JPG, GIF or WebP.')

    has_alpha = (
        image.mode in ('RGBA', 'LA', 'PA')
        or (image.mode == 'P' and 'transparency' in image.info)
    )
    if kind == OrganizationBrandImage.FAVICON:
        image = _square_icon(image.convert('RGBA'))
        has_alpha = True
    else:
        image = image.convert('RGBA' if has_alpha else 'RGB')
        image.thumbnail(MAX_STORED_SIZE, Image.LANCZOS)

    out = io.BytesIO()
    if source_format == 'JPEG' and not has_alpha:
        image.save(out, format='JPEG', quality=90, optimize=True)
        content_type = 'image/jpeg'
    else:
        # PNG keeps transparency and is supported by every email client.
        image.save(out, format='PNG', optimize=True)
        content_type = 'image/png'

    return out.getvalue(), content_type, image.width, image.height


def _square_icon(image):
    """Fit `image` inside a transparent FAVICON_SIZE square without cropping."""
    image.thumbnail((FAVICON_SIZE, FAVICON_SIZE), Image.LANCZOS)
    canvas = Image.new('RGBA', (FAVICON_SIZE, FAVICON_SIZE), (0, 0, 0, 0))
    canvas.paste(image, ((FAVICON_SIZE - image.width) // 2, (FAVICON_SIZE - image.height) // 2))
    return canvas


def _timestamp_field(kind):
    return f'{kind}_updated_at'


def set_brand_image(organization, kind, uploaded_file):
    """Process and save `uploaded_file` as the organization's logo or favicon."""
    data, content_type, width, height = process_logo_upload(uploaded_file, kind)
    OrganizationBrandImage.objects.update_or_create(
        organization=organization,
        kind=kind,
        defaults={
            'data': data,
            'content_type': content_type,
            'width': width,
            'height': height,
        },
    )
    setattr(organization, _timestamp_field(kind), timezone.now())
    organization.save(update_fields=[_timestamp_field(kind), 'updated_at'])


def clear_brand_image(organization, kind):
    """Remove the organization's logo or favicon, reverting to the default."""
    OrganizationBrandImage.objects.filter(organization=organization, kind=kind).delete()
    setattr(organization, _timestamp_field(kind), None)
    organization.save(update_fields=[_timestamp_field(kind), 'updated_at'])


def set_organization_logo(organization, uploaded_file):
    set_brand_image(organization, OrganizationBrandImage.LOGO, uploaded_file)


def clear_organization_logo(organization):
    clear_brand_image(organization, OrganizationBrandImage.LOGO)


def set_organization_favicon(organization, uploaded_file):
    set_brand_image(organization, OrganizationBrandImage.FAVICON, uploaded_file)


def clear_organization_favicon(organization):
    clear_brand_image(organization, OrganizationBrandImage.FAVICON)


def fit_within(width, height, box=EMAIL_LOGO_BOX):
    """Scale (width, height) down to fit inside `box`, preserving aspect ratio."""
    max_w, max_h = box
    if not width or not height:
        return max_w, max_h
    scale = min(max_w / width, max_h / height, 1)
    return max(1, round(width * scale)), max(1, round(height * scale))


def get_default_organization():
    """The organization to brand pages/emails that have no org context.

    Only returned when the instance has exactly one active organization, so a
    multi-tenant deployment never shows one organization's logo to another's
    users.
    """
    try:
        orgs = list(Organization.objects.filter(is_active=True)[:2])
    except Exception:
        logger.exception('Error loading organization for branding')
        return None
    return orgs[0] if len(orgs) == 1 else None


def resolve_organization(*candidates):
    """Return the first Organization found among `candidates`.

    Each candidate may be an Organization, or an object that leads to one via
    `.organization`, `.reviewee.organization` or `.profile.organization`.
    Falls back to get_default_organization().
    """
    for obj in candidates:
        if obj is None:
            continue
        if isinstance(obj, Organization):
            return obj
        for path in ('organization', 'reviewee.organization', 'profile.organization'):
            target = obj
            try:
                for attr in path.split('.'):
                    target = getattr(target, attr, None)
                    if target is None:
                        break
            except Exception:
                target = None
            if isinstance(target, Organization):
                return target
    return get_default_organization()
