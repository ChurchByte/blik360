"""Custom organization branding: logo and favicon upload, serving, pages and emails."""
import io

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import UserProfile
from accounts.permissions import assign_organization_admin
from core.branding import (
    clear_organization_favicon, clear_organization_logo, fit_within, process_logo_upload,
    set_organization_favicon, set_organization_logo,
)
from core.models import Organization, OrganizationBrandImage

# Templates under test load css via {% static %}; don't require collectstatic.
NO_MANIFEST = override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})


def make_image(fmt='PNG', size=(400, 100), mode='RGBA', name='logo.png'):
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30, 255) if mode == 'RGBA' else (200, 30, 30)).save(buf, format=fmt)
    return SimpleUploadedFile(name, buf.getvalue(), content_type=f'image/{fmt.lower()}')


class ProcessLogoUploadTests(TestCase):
    def test_png_with_alpha_is_stored_as_png(self):
        data, content_type, w, h = process_logo_upload(make_image())
        self.assertEqual(content_type, 'image/png')
        self.assertEqual((w, h), (400, 100))
        self.assertTrue(data.startswith(b'\x89PNG'))

    def test_jpeg_stays_jpeg(self):
        _, content_type, _, _ = process_logo_upload(make_image('JPEG', mode='RGB', name='logo.jpg'))
        self.assertEqual(content_type, 'image/jpeg')

    def test_large_image_is_downscaled(self):
        _, _, w, h = process_logo_upload(make_image(size=(4000, 1000)))
        self.assertLessEqual(w, 1200)
        self.assertLessEqual(h, 400)

    def test_rejects_non_image(self):
        bad = SimpleUploadedFile('logo.png', b'<svg onload="alert(1)"></svg>', content_type='image/png')
        with self.assertRaises(ValidationError):
            process_logo_upload(bad)

    def test_rejects_oversized_file(self):
        big = SimpleUploadedFile('logo.png', b'0' * (2 * 1024 * 1024 + 1), content_type='image/png')
        with self.assertRaises(ValidationError):
            process_logo_upload(big)

    def test_favicon_is_padded_to_square_png(self):
        data, content_type, w, h = process_logo_upload(make_image(size=(400, 100)), kind='favicon')
        self.assertEqual(content_type, 'image/png')
        self.assertEqual((w, h), (256, 256))
        corner = Image.open(io.BytesIO(data)).getpixel((0, 0))
        self.assertEqual(corner[3], 0)  # padding is transparent, not cropped

    def test_fit_within_preserves_aspect_ratio(self):
        self.assertEqual(fit_within(1200, 300), (240, 60))
        self.assertEqual(fit_within(100, 50), (100, 50))  # never upscales


@NO_MANIFEST
class LogoSettingsAndServingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Kings', email='org@kings.example')
        self.admin = User.objects.create_user(username='admin', email='a@kings.example', password='pw')
        UserProfile.objects.create(user=self.admin, organization=self.org)
        assign_organization_admin(self.admin)
        self.client.force_login(self.admin)

    def test_admin_can_upload_and_logo_is_served(self):
        response = self.client.post(reverse('update_logo'), {'logo': make_image()})
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertTrue(self.org.has_logo)

        served = self.client.get(self.org.get_logo_url())
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served['Content-Type'], 'image/png')
        self.assertEqual(served['X-Content-Type-Options'], 'nosniff')

    def test_logo_replaces_wordmark_in_dashboard_header(self):
        page = self.client.get(reverse('settings')).content.decode()
        self.assertIn('class="navbar-brand">Lead360</a>', page)

        set_organization_logo(self.org, make_image())
        self.org.refresh_from_db()
        page = self.client.get(reverse('settings')).content.decode()
        self.assertIn('class="navbar-logo"', page)
        self.assertIn(self.org.get_logo_url(), page)

    def test_logo_shows_on_public_login_page(self):
        set_organization_logo(self.org, make_image())
        self.client.logout()
        page = self.client.get(reverse('login')).content.decode()
        self.assertIn('public-brand-logo', page)

    def test_remove_logo(self):
        set_organization_logo(self.org, make_image())
        self.client.post(reverse('remove_logo'))
        self.org.refresh_from_db()
        self.assertFalse(self.org.has_logo)
        self.assertFalse(OrganizationBrandImage.objects.filter(kind='logo').exists())
        self.assertEqual(self.client.get(reverse('organization_brand_image', args=[self.org.pk, 'logo'])).status_code, 404)

    def test_favicon_upload_replaces_default_icon_on_all_pages(self):
        page = self.client.get(reverse('settings')).content.decode()
        self.assertIn('image/svg+xml', page)  # default Lead360 icon

        response = self.client.post(reverse('update_favicon'), {'favicon': make_image(size=(64, 64))})
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertTrue(self.org.has_favicon)
        self.assertFalse(self.org.has_logo)  # independent of the logo

        url = self.org.get_favicon_url()
        page = self.client.get(reverse('settings')).content.decode()
        self.assertIn(f'<link rel="icon" type="image/png" href="{url}">', page)
        self.client.logout()
        self.assertContains(self.client.get(reverse('login')), url)

        served = self.client.get(url)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served['Content-Type'], 'image/png')

    def test_remove_favicon_keeps_logo(self):
        set_organization_logo(self.org, make_image())
        set_organization_favicon(self.org, make_image())
        self.client.post(reverse('remove_favicon'))
        self.org.refresh_from_db()
        self.assertFalse(self.org.has_favicon)
        self.assertTrue(self.org.has_logo)
        self.assertEqual(self.client.get(reverse('organization_brand_image', args=[self.org.pk, 'favicon'])).status_code, 404)

    def test_unknown_image_kind_is_404(self):
        set_organization_logo(self.org, make_image())
        self.assertEqual(self.client.get(f'/branding/{self.org.pk}/other/').status_code, 404)

    def test_non_admin_cannot_upload(self):
        member = User.objects.create_user(username='m', email='m@kings.example', password='pw')
        UserProfile.objects.create(user=member, organization=self.org)
        self.client.force_login(member)
        self.client.post(reverse('update_logo'), {'logo': make_image()})
        self.client.post(reverse('update_favicon'), {'favicon': make_image()})
        self.org.refresh_from_db()
        self.assertFalse(self.org.has_logo)
        self.assertFalse(self.org.has_favicon)

    def test_invalid_upload_shows_error_and_keeps_existing_logo(self):
        set_organization_logo(self.org, make_image())
        bad = SimpleUploadedFile('logo.png', b'not an image', content_type='image/png')
        response = self.client.post(reverse('update_logo'), {'logo': bad}, follow=True)
        self.assertContains(response, 'not a valid image')
        self.org.refresh_from_db()
        self.assertTrue(self.org.has_logo)


@override_settings(SITE_PROTOCOL='https', SITE_DOMAIN='360.example.org')
class EmailLogoTagTests(TestCase):
    template = Template('{% load branding_tags %}{% email_logo %}')

    def setUp(self):
        self.org = Organization.objects.create(name='Kings', email='org@kings.example')

    def test_renders_nothing_without_logo(self):
        self.assertEqual(self.template.render(Context({'organization': self.org})).strip(), '')

    def test_renders_absolute_url_with_dimensions(self):
        set_organization_logo(self.org, make_image(size=(1200, 300)))
        self.org.refresh_from_db()
        html = self.template.render(Context({'organization': self.org}))
        self.assertIn('src="https://360.example.org/branding/%d/logo/' % self.org.pk, html)
        self.assertIn('width="240" height="60"', html)
        self.assertIn('alt="Kings"', html)

    def test_falls_back_to_single_active_org(self):
        set_organization_logo(self.org, make_image())
        self.assertIn('<img', self.template.render(Context({})))

    def test_no_fallback_when_multiple_orgs(self):
        """Never show one tenant's logo to another tenant's recipients."""
        set_organization_logo(self.org, make_image())
        Organization.objects.create(name='Other', email='o@other.example')
        self.assertEqual(self.template.render(Context({})).strip(), '')

    def test_real_email_template_includes_logo(self):
        from django.template.loader import render_to_string
        set_organization_logo(self.org, make_image())
        self.org.refresh_from_db()
        html = render_to_string('emails/reviewer_invitation.html', {
            'organization': self.org, 'category': 'peer', 'reviewee_name': 'Sam',
            'feedback_url': 'https://x/feedback/1/', 'questionnaire_name': 'Q',
        })
        self.assertIn('/branding/%d/logo/' % self.org.pk, html)

    def test_clear_logo(self):
        set_organization_logo(self.org, make_image())
        clear_organization_logo(self.org)
        self.assertEqual(self.template.render(Context({'organization': self.org})).strip(), '')
