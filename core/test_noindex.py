"""
The site must stay out of search engines, and the removed marketing pages
must report themselves as permanently gone.
"""
from django.test import TestCase, override_settings

from core.factories import OrganizationFactory, UserFactory
from accounts.factories import UserProfileFactory

NOINDEX = 'noindex, nofollow, noarchive, nosnippet'


class NoIndexTestCase(TestCase):
    def setUp(self):
        # Setup-wizard middleware redirects everything until a user exists
        UserProfileFactory(user=UserFactory(), organization=OrganizationFactory())

    def test_pages_carry_noindex_header(self):
        for url in ['/', '/accounts/login/', '/license/', '/health/', '/does-not-exist/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response['X-Robots-Tag'], NOINDEX)

    def test_login_page_has_robots_meta_tag(self):
        response = self.client.get('/accounts/login/')
        self.assertContains(response, '<meta name="robots" content="noindex')

    def test_removed_marketing_pages_return_410(self):
        for url in ['/landing', '/landing/', '/landing/pricing/', '/landing/vs-lattice/',
                    '/landing/dreyfus-assessment/submit/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response['X-Robots-Tag'], NOINDEX)

    def test_sitemap_removed(self):
        self.assertEqual(self.client.get('/sitemap.xml').status_code, 404)

    def test_robots_txt_allows_crawling_so_noindex_is_seen(self):
        response = self.client.get('/robots.txt')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Disallow:\n', body)
        self.assertNotIn('Disallow: /', body)
        self.assertNotIn('Sitemap', body)

    @override_settings(SEARCH_ENGINES_BLOCK_CRAWLING=True)
    def test_robots_txt_can_block_all_crawling(self):
        body = self.client.get('/robots.txt').content.decode()
        self.assertIn('Disallow: /\n', body)
