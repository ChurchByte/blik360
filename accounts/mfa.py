"""
Email-code multi-factor authentication for privileged accounts.

Anyone holding an admin or report-viewing permission (see
accounts.permissions.requires_mfa) must enter a 6-digit code emailed to them
after signing in, before they can use the app. Enforcement lives in
accounts.middleware.MFAMiddleware so every login path (password login,
invitation signup, setup wizard, Stripe auto-login, Django admin) is covered.

Codes are random, stored only as a keyed hash, expire after
MFA_CODE_TTL_MINUTES, allow MFA_MAX_ATTEMPTS guesses, and a new code
invalidates the previous one. Sending is throttled per user.
"""
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.crypto import salted_hmac

from accounts.models import EmailMFACode
from accounts.permissions import requires_mfa

logger = logging.getLogger(__name__)

SESSION_KEY = '_mfa_verified_uid'

CODE_TTL_MINUTES = getattr(settings, 'MFA_CODE_TTL_MINUTES', 10)
MAX_ATTEMPTS = getattr(settings, 'MFA_MAX_ATTEMPTS', 5)
RESEND_COOLDOWN_SECONDS = getattr(settings, 'MFA_RESEND_COOLDOWN_SECONDS', 30)
MAX_CODES_PER_HOUR = getattr(settings, 'MFA_MAX_CODES_PER_HOUR', 6)


def mfa_enabled():
    return getattr(settings, 'MFA_REQUIRED', True)


def user_needs_mfa(user):
    return mfa_enabled() and requires_mfa(user)


def is_verified(request):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return False
    return request.session.get(SESSION_KEY) == user.pk


def mark_verified(request):
    request.session.cycle_key()
    request.session[SESSION_KEY] = request.user.pk


def clear_verified(request):
    request.session.pop(SESSION_KEY, None)


def _hash(user, code):
    return salted_hmac('blik.accounts.mfa', f'{user.pk}:{code}').hexdigest()


def active_code(user):
    return (
        EmailMFACode.objects.filter(user=user, used_at__isnull=True, expires_at__gt=timezone.now())
        .order_by('-created_at')
        .first()
    )


def can_send_code(user):
    """Returns (allowed, seconds_to_wait)."""
    now = timezone.now()
    recent = EmailMFACode.objects.filter(user=user, created_at__gte=now - timedelta(hours=1))
    last = recent.order_by('-created_at').first()
    if last and (now - last.created_at).total_seconds() < RESEND_COOLDOWN_SECONDS:
        return False, int(RESEND_COOLDOWN_SECONDS - (now - last.created_at).total_seconds()) + 1
    if recent.count() >= MAX_CODES_PER_HOUR:
        oldest = recent.order_by('created_at').first()
        wait = 3600 - (now - oldest.created_at).total_seconds()
        return False, max(int(wait), 1)
    return True, 0


def issue_code(user):
    """
    Create a new code (invalidating earlier ones) and email it.
    Returns True if the email was handed to the mail backend.
    """
    now = timezone.now()
    EmailMFACode.objects.filter(user=user, used_at__isnull=True).update(used_at=now)
    code = f'{secrets.randbelow(1_000_000):06d}'
    EmailMFACode.objects.create(
        user=user,
        code_hash=_hash(user, code),
        expires_at=now + timedelta(minutes=CODE_TTL_MINUTES),
    )
    try:
        _send_code_email(user, code)
        return True
    except Exception:
        logger.exception('Failed to send MFA code email to user %s', user.pk)
        return False


def verify_code(user, code):
    """
    Check a submitted code. Returns (ok, error_message).
    Each wrong guess counts against the active code.
    """
    code = (code or '').strip().replace(' ', '')
    record = active_code(user)
    if record is None:
        return False, 'Your code has expired. Request a new one.'
    if record.attempts >= MAX_ATTEMPTS:
        return False, 'Too many incorrect attempts. Request a new code.'
    if not code.isdigit() or len(code) != 6 or not hmac.compare_digest(record.code_hash, _hash(user, code)):
        record.attempts += 1
        record.save(update_fields=['attempts'])
        remaining = MAX_ATTEMPTS - record.attempts
        if remaining <= 0:
            return False, 'Too many incorrect attempts. Request a new code.'
        return False, f'That code is not correct. {remaining} attempt{"s" if remaining != 1 else ""} left.'
    record.used_at = timezone.now()
    record.save(update_fields=['used_at'])
    return True, ''


def _send_code_email(user, code):
    from django.template.loader import render_to_string
    from core.email import send_email

    context = {
        'user': user,
        'code': code,
        'minutes': CODE_TTL_MINUTES,
        'site_name': settings.SITE_NAME,
    }
    send_email(
        subject=f'Your {settings.SITE_NAME} sign-in code: {code}',
        message=render_to_string('emails/mfa_code.txt', context),
        recipient_list=[user.email],
        html_message=render_to_string('emails/mfa_code.html', context),
    )
