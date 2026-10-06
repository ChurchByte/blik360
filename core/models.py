from django.db import models
from django.conf import settings
from cryptography.fernet import Fernet


class TimeStampedModel(models.Model):
    """Abstract base model with created and updated timestamps"""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Organization(TimeStampedModel):
    """Organization model for multi-tenant support"""
    name = models.CharField(max_length=255)
    email = models.EmailField()

    # Email settings
    smtp_host = models.CharField(max_length=255, blank=True)
    smtp_port = models.IntegerField(default=587)
    smtp_username = models.CharField(max_length=255, blank=True)
    smtp_password_encrypted = models.BinaryField(blank=True, null=True)
    smtp_use_tls = models.BooleanField(default=True)
    from_email = models.EmailField(blank=True)

    # Report settings
    min_responses_for_anonymity = models.IntegerField(
        default=3,
        help_text='Minimum number of responses required to show results (for anonymity). Set to 1 for small teams.'
    )
    auto_send_report_email = models.BooleanField(
        default=True,
        help_text='Automatically send email to reviewee when their report is generated'
    )

    # Registration settings
    allow_registration = models.BooleanField(
        default=False,
        help_text='Allow new users to register for this organization'
    )
    default_users_can_create_cycles = models.BooleanField(
        default=False,
        help_text='By default, new users can create cycles for others (not just themselves)'
    )

    # Branding — the image bytes live in OrganizationBrandImage so they are
    # not loaded on every Organization query; these timestamps double as the
    # "has an image" flags and the cache-busting versions for the image URLs.
    logo_updated_at = models.DateTimeField(blank=True, null=True)
    favicon_updated_at = models.DateTimeField(blank=True, null=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'organizations'
        ordering = ['name']

    def __str__(self):
        return self.name

    def set_smtp_password(self, raw_password):
        """
        Encrypt and store SMTP password.

        Args:
            raw_password (str): The plaintext SMTP password

        Note: Requires ENCRYPTION_KEY to be set in settings
        """
        if raw_password:
            encryption_key = getattr(settings, 'ENCRYPTION_KEY', None)
            if not encryption_key:
                raise ValueError(
                    'ENCRYPTION_KEY not configured in settings. '
                    'Generate with: from cryptography.fernet import Fernet; Fernet.generate_key()'
                )
            f = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
            self.smtp_password_encrypted = f.encrypt(raw_password.encode())
        else:
            self.smtp_password_encrypted = None

    def get_smtp_password(self):
        """
        Decrypt and return SMTP password.

        Returns:
            str or None: The decrypted SMTP password, or None if not set
        """
        if self.smtp_password_encrypted:
            encryption_key = getattr(settings, 'ENCRYPTION_KEY', None)
            if not encryption_key:
                raise ValueError('ENCRYPTION_KEY not configured in settings')
            f = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
            return f.decrypt(self.smtp_password_encrypted).decode()
        return None

    @property
    def has_logo(self):
        return self.logo_updated_at is not None

    @property
    def has_favicon(self):
        return self.favicon_updated_at is not None

    def _brand_image_url(self, kind, updated_at):
        if updated_at is None:
            return ''
        from django.urls import reverse
        return f"{reverse('organization_brand_image', args=[self.pk, kind])}?v={int(updated_at.timestamp())}"

    def get_logo_url(self):
        """Site-relative URL of the custom logo, or '' if none is set."""
        return self._brand_image_url('logo', self.logo_updated_at)

    def get_favicon_url(self):
        """Site-relative URL of the custom favicon, or '' if none is set."""
        return self._brand_image_url('favicon', self.favicon_updated_at)

    def get_absolute_logo_url(self):
        """Absolute logo URL for use in emails, or '' if none is set."""
        url = self.get_logo_url()
        if not url:
            return ''
        return f'{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}{url}'

    @property
    def smtp_password(self):
        """Backward compatibility property"""
        return self.get_smtp_password()

    @smtp_password.setter
    def smtp_password(self, value):
        """Backward compatibility setter"""
        self.set_smtp_password(value)


class OrganizationBrandImage(models.Model):
    """Custom branding image (logo or favicon) for an organization.

    Stored in the database rather than MEDIA_ROOT so it survives container
    rebuilds without needing a media volume, and can be served at a stable
    public URL (emails need an absolute, publicly reachable image URL).
    """
    LOGO = 'logo'
    FAVICON = 'favicon'
    KIND_CHOICES = [(LOGO, 'Logo'), (FAVICON, 'Favicon')]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='brand_images',
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    data = models.BinaryField()
    content_type = models.CharField(max_length=50)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'organization_brand_images'
        constraints = [
            models.UniqueConstraint(fields=['organization', 'kind'], name='unique_brand_image_per_kind'),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} for {self.organization}'


class WelcomeEmailFact(TimeStampedModel):
    """
    Educational facts about 360 feedback, psychology, and development.
    These are randomly selected and shown in welcome emails.
    """
    title = models.CharField(
        max_length=255,
        help_text='Short title for the fact (e.g., "The Power of 360 Feedback")'
    )
    content = models.TextField(
        help_text='The fact content. Can include HTML tags like <strong> for emphasis.'
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Only active facts will be shown in emails'
    )
    display_order = models.IntegerField(
        default=0,
        help_text='Optional ordering (lower numbers shown first when not randomizing)'
    )

    class Meta:
        db_table = 'welcome_email_facts'
        ordering = ['display_order', 'created_at']
        verbose_name = 'Welcome Email Fact'
        verbose_name_plural = 'Welcome Email Facts'

    def __str__(self):
        return self.title


class UpgradeStep(models.Model):
    """Tracks one-time data upgrade steps that run on deploy."""
    name = models.CharField(max_length=255, unique=True)
    applied_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=False)
    error = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'upgrade_steps'

    def __str__(self):
        status = 'OK' if self.success else 'FAILED'
        return f'{self.name} [{status}]'


class AuditLog(models.Model):
    """
    Append-only record of security- and privacy-relevant actions: role
    changes, report access, investigations into reviewer identities, exports,
    deletions, settings changes and sign-in events.

    Never store feedback content here. Rows are written through
    core.audit.log_event() and are not editable once saved.
    """
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs',
    )
    organization_name = models.CharField(max_length=255, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs',
    )
    # Snapshot so the record still says who acted after the account is gone.
    actor_email = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=64, db_index=True)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    target_label = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'audit_logs'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['organization', '-created_at']),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None and not kwargs.pop('_allow_update', False):
            raise ValueError('Audit log entries cannot be modified.')
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.actor_email or "system"} {self.action}'
