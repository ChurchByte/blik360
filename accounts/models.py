import uuid
from django.contrib.auth.models import User
from django.db import models
from django.utils.crypto import get_random_string
from core.models import TimeStampedModel, Organization
from core.managers import OrganizationManager


class UserProfile(TimeStampedModel):
    """Extended user profile with organization relationship"""
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='profile'
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='users'
    )
    can_create_cycles_for_others = models.BooleanField(
        default=False,
        help_text='Mirrors the Cycle Manager role (kept in sync by accounts.permissions.set_user_roles)'
    )
    has_seen_welcome = models.BooleanField(
        default=False,
        help_text='Whether user has seen the welcome modal'
    )
    # Campus scope for the Report Viewer role (ignored for everyone else).
    report_all_campuses = models.BooleanField(
        default=True,
        help_text='Report Viewers: may read reports for every reviewee, including '
                  'reviewees with no campus and campuses added later'
    )
    report_campuses = models.ManyToManyField(
        'Campus',
        blank=True,
        related_name='report_viewers',
        help_text='Report Viewers without "all campuses": the campuses whose '
                  'reviewees\' reports they may read'
    )

    objects = OrganizationManager()

    class Meta:
        db_table = 'user_profiles'
        ordering = ['user__username']
        # Granted through role groups — see accounts/permissions.py.
        permissions = [
            # Owner
            ('can_manage_billing', 'Can manage billing and subscription'),
            ('can_delete_organization', 'Can delete organization'),
            ('can_manage_owners', 'Can add and remove owners'),
            ('can_view_audit_log', 'Can view the audit log'),
            # Organization Admin
            ('can_invite_members', 'Can invite team members'),
            ('can_manage_organization', 'Can manage organization settings'),
            # Cycle Manager (also held by Organization Admin)
            ('can_manage_cycles', 'Can create and run review cycles for others'),
            # Report Viewer
            ('can_view_all_reports', 'Can view all organization reports'),
            ('can_investigate_responses', 'Can view reviewer identities for investigations'),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.organization.name}"


class EmailMFACode(models.Model):
    """
    One-time code emailed to a user as the second login factor.

    Only a keyed hash of the code is stored. A new code supersedes any earlier
    unused one; each code expires after a few minutes and allows a limited
    number of attempts (see accounts/mfa.py).
    """
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='mfa_codes'
    )
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'email_mfa_codes'
        ordering = ['-created_at']

    def __str__(self):
        return f"MFA code for {self.user_id} ({self.created_at:%Y-%m-%d %H:%M})"


class TrustedDevice(models.Model):
    """
    A browser the user chose to remember after passing email MFA, so they
    aren't asked for a code there again until it expires.

    The browser holds a random token in a signed cookie; only a hash of the
    token is stored here. Deleting the row revokes the device.
    """
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='trusted_devices'
    )
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = 'mfa_trusted_devices'
        ordering = ['-created_at']

    def __str__(self):
        return f"Trusted device for {self.user_id} until {self.expires_at:%Y-%m-%d}"


class OrganizationInvitation(TimeStampedModel):
    """Invitation to join an organization"""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='invitations'
    )
    email = models.EmailField()
    token = models.CharField(max_length=64, unique=True, db_index=True)
    invited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='sent_invitations'
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    objects = OrganizationManager()

    class Meta:
        db_table = 'organization_invitations'
        ordering = ['-created_at']
        unique_together = ['organization', 'email']

    def __str__(self):
        return f"Invite {self.email} to {self.organization.name}"

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = get_random_string(64)
        super().save(*args, **kwargs)

    def is_valid(self):
        """Check if invitation is still valid"""
        from django.utils import timezone
        return (
            self.accepted_at is None and
            self.expires_at > timezone.now()
        )


class PasswordResetToken(TimeStampedModel):
    """Token for password reset requests"""
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='password_reset_tokens'
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'password_reset_tokens'
        ordering = ['-created_at']

    def __str__(self):
        return f"Password reset for {self.user.email}"

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = get_random_string(64)
        if not self.expires_at:
            from django.utils import timezone
            from datetime import timedelta
            self.expires_at = timezone.now() + timedelta(hours=1)
        super().save(*args, **kwargs)

    def is_valid(self):
        """Check if token is still valid (not expired and not used)"""
        from django.utils import timezone
        return self.used_at is None and self.expires_at > timezone.now()


class Campus(TimeStampedModel):
    """A campus (site/location) of an organization. Reviewees can belong to
    several campuses, and Report Viewers can be limited to some campuses."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='campuses'
    )
    name = models.CharField(max_length=100)

    class Meta:
        db_table = 'campuses'
        ordering = ['name']
        unique_together = ['organization', 'name']
        verbose_name_plural = 'campuses'

    def __str__(self):
        return self.name


class Reviewee(TimeStampedModel):
    """Person being reviewed in 360 feedback"""
    # Public UUID for external references (API, URLs)
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
        help_text="Public identifier for API and URL usage (non-enumerable)"
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='reviewees'
    )
    name = models.CharField(max_length=255)
    email = models.EmailField()
    department = models.CharField(max_length=255, blank=True)
    campuses = models.ManyToManyField(
        Campus,
        blank=True,
        related_name='reviewees',
        help_text='Report Viewers limited to campuses only see reports for '
                  'reviewees in one of their campuses'
    )
    is_active = models.BooleanField(default=True)

    objects = OrganizationManager()

    class Meta:
        db_table = 'reviewees'
        ordering = ['name']
        unique_together = ['organization', 'email']

    def __str__(self):
        return f"{self.name} ({self.email})"
