"""
Search-engine endpoints.

Lead360 is a private application and must not appear in search results.
Pages are kept out of the index by the `X-Robots-Tag: noindex` header added
by core.middleware.NoIndexMiddleware (plus a robots meta tag in the page
templates). robots.txt deliberately does NOT block crawling by default:
a crawler that is blocked by robots.txt never sees the noindex instruction,
so pages that were already indexed would linger. Once the site has dropped
out of the index, set SEARCH_ENGINES_BLOCK_CRAWLING=True to block crawling too.
"""
from django.conf import settings
from django.http import HttpResponse, HttpResponseGone


def robots(request):
    """robots.txt - no sitemap; optionally block all crawling."""
    if getattr(settings, 'SEARCH_ENGINES_BLOCK_CRAWLING', False):
        body = 'User-agent: *\nDisallow: /\n'
    else:
        body = (
            '# This site must not be indexed. Every page is served with\n'
            '# "X-Robots-Tag: noindex"; crawling is allowed so that search\n'
            '# engines can see that instruction and remove existing entries.\n'
            'User-agent: *\n'
            'Disallow:\n'
        )
    return HttpResponse(body, content_type='text/plain')


def gone(request, *args, **kwargs):
    """410 Gone for the removed marketing pages."""
    return HttpResponseGone(
        'This page has been permanently removed.',
        content_type='text/plain',
    )
