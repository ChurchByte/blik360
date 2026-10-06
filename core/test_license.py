"""Lead360 product identity, footer attribution and the License & Credits page."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import UserProfile
from accounts.permissions import assign_organization_admin
from core.models import Organization

NO_MANIFEST = override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})


@NO_MANIFEST
class LicensePageTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Kings', email='org@kings.example')
        self.admin = User.objects.create_user(username='admin', email='a@kings.example', password='pw')
        UserProfile.objects.create(user=self.admin, organization=self.org)
        assign_organization_admin(self.admin)

    def test_license_page_is_public_and_credits_upstream(self):
        response = self.client.get(reverse('license'))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('Lead360', page)
        self.assertIn('ChurchByte', page)
        self.assertIn('modified version of', page)
        self.assertIn('https://github.com/thijsdezoete/blik', page)
        self.assertIn('Thijs de Zoete', page)
        self.assertIn('https://github.com/ChurchByte/blik360', page)
        # Full AGPL text from the LICENSE file is rendered
        self.assertIn('GNU AFFERO GENERAL PUBLIC LICENSE', page)
        self.assertIn('Remote Network Interaction', page)

    def test_license_page_reachable_before_setup(self):
        UserProfile.objects.all().delete()
        Organization.objects.all().delete()
        self.assertEqual(self.client.get(reverse('license')).status_code, 200)

    def test_footer_links_to_license_on_public_and_dashboard_pages(self):
        login = self.client.get(reverse('login')).content.decode()
        self.assertIn('class="site-footer', login)
        self.assertIn(f'href="{reverse("license")}"', login)
        self.assertIn('is a product of', login)

        self.client.force_login(self.admin)
        dashboard = self.client.get(reverse('settings')).content.decode()
        self.assertIn(f'href="{reverse("license")}"', dashboard)
        self.assertIn('https://github.com/ChurchByte/blik360', dashboard)
        self.assertNotIn('- Blik</title>', dashboard)
