"""
Audit logging.

Call log_event() wherever something security- or privacy-relevant happens.
It never raises: a failure to write an audit row is logged and the request
carries on.

Do not put feedback content (answers, comments) in `details`.
"""
import logging

logger = logging.getLogger(__name__)


class Actions:
    # Authentication
    LOGIN = 'auth.login'
    LOGOUT = 'auth.logout'
    MFA_CODE_SENT = 'auth.mfa_code_sent'
    MFA_VERIFIED = 'auth.mfa_verified'
    MFA_FAILED = 'auth.mfa_failed'

    # Team and roles
    ROLES_CHANGED = 'team.roles_changed'
    OWNERSHIP_TRANSFERRED = 'team.ownership_transferred'
    MEMBER_INVITED = 'team.member_invited'
    INVITATION_ACCEPTED = 'team.invitation_accepted'

    # Reports and responses
    REPORT_VIEWED = 'report.viewed'
    REPORT_GENERATED = 'report.generated'
    REPORT_EMAILED = 'report.emailed'
    INVESTIGATION_ACCESS = 'report.reviewer_identities_viewed'
    INVESTIGATION_DENIED = 'report.reviewer_identities_denied'

    # Cycles
    CYCLE_CREATED = 'cycle.created'
    CYCLE_CLOSED = 'cycle.closed'
    CYCLE_ARCHIVED = 'cycle.archived'
    CYCLE_RESTORED = 'cycle.restored'
    REVIEWER_REMOVED = 'cycle.reviewer_removed'

    # Organization and data
    SETTINGS_CHANGED = 'org.settings_changed'
    DATA_EXPORTED = 'data.exported'
    DATA_IMPORTED = 'data.imported'
    USER_DATA_DELETED = 'data.user_deleted'
    REVIEWEE_DATA_DELETED = 'data.reviewee_deleted'
    ACCOUNT_DELETED = 'data.account_deleted'
    ORGANIZATION_DELETED = 'org.deleted'
    SUBSCRIPTION_CHANGED = 'billing.subscription_changed'
    API_TOKEN_CREATED = 'api.token_created'
    API_TOKEN_UPDATED = 'api.token_updated'
    API_TOKEN_DELETED = 'api.token_deleted'
    WEBHOOK_CHANGED = 'api.webhook_changed'


ACTION_LABELS = {
    Actions.LOGIN: 'Signed in',
    Actions.LOGOUT: 'Signed out',
    Actions.MFA_CODE_SENT: 'Sign-in code sent',
    Actions.MFA_VERIFIED: 'Sign-in code verified',
    Actions.MFA_FAILED: 'Sign-in code rejected',
    Actions.ROLES_CHANGED: 'Roles changed',
    Actions.OWNERSHIP_TRANSFERRED: 'Ownership transferred',
    Actions.MEMBER_INVITED: 'Member invited',
    Actions.INVITATION_ACCEPTED: 'Invitation accepted',
    Actions.REPORT_VIEWED: 'Report viewed',
    Actions.REPORT_GENERATED: 'Report generated',
    Actions.REPORT_EMAILED: 'Report emailed to reviewee',
    Actions.INVESTIGATION_ACCESS: 'Reviewer identities viewed (investigation)',
    Actions.INVESTIGATION_DENIED: 'Reviewer identity access refused',
    Actions.CYCLE_CREATED: 'Cycle created',
    Actions.CYCLE_CLOSED: 'Cycle closed',
    Actions.CYCLE_ARCHIVED: 'Cycle archived',
    Actions.CYCLE_RESTORED: 'Cycle restored',
    Actions.REVIEWER_REMOVED: 'Reviewer removed from cycle',
    Actions.SETTINGS_CHANGED: 'Organization settings changed',
    Actions.DATA_EXPORTED: 'Organization data exported',
    Actions.DATA_IMPORTED: 'Organization data imported',
    Actions.USER_DATA_DELETED: 'User data deleted (GDPR)',
    Actions.REVIEWEE_DATA_DELETED: 'Reviewee data deleted (GDPR)',
    Actions.ACCOUNT_DELETED: 'Account deleted',
    Actions.ORGANIZATION_DELETED: 'Organization deleted',
    Actions.SUBSCRIPTION_CHANGED: 'Subscription changed',
    Actions.API_TOKEN_CREATED: 'API token created',
    Actions.API_TOKEN_UPDATED: 'API token updated',
    Actions.API_TOKEN_DELETED: 'API token deleted',
    Actions.WEBHOOK_CHANGED: 'Webhook changed',
}


def client_ip(request):
    """Best-effort client IP (first X-Forwarded-For hop, else REMOTE_ADDR)."""
    if request is None:
        return None
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    ip = forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR')
    if not ip:
        return None
    from django.core.validators import validate_ipv46_address
    from django.core.exceptions import ValidationError
    try:
        validate_ipv46_address(ip)
    except ValidationError:
        return None
    return ip


def log_event(request, action, *, actor=None, organization=None, target=None,
              target_label='', details=None):
    """
    Record an audit event.

    Args:
        request: the HttpRequest (or None for background/system events)
        action: one of Actions.*
        actor: defaults to request.user when authenticated
        organization: defaults to request.organization, then the actor's org
        target: optional model instance the action was about
        target_label: human-readable description of the target
        details: small JSON-serialisable dict (never feedback content)
    """
    from core.models import AuditLog

    try:
        if actor is None and request is not None:
            user = getattr(request, 'user', None)
            if user is not None and getattr(user, 'is_authenticated', False):
                actor = user

        if organization is None and request is not None:
            organization = getattr(request, 'organization', None)
        if organization is None and actor is not None:
            profile = getattr(actor, 'profile', None)
            organization = getattr(profile, 'organization', None) if profile else None

        # Objects deleted in this request (e.g. the organization on
        # "delete organization") can't be referenced any more.
        if organization is not None and getattr(organization, 'pk', None) is None:
            organization = None
        if actor is not None and getattr(actor, 'pk', None) is None:
            actor = None

        target_type = target_id = ''
        if target is not None:
            target_type = target._meta.label
            target_id = str(getattr(target, 'uuid', None) or target.pk)
            if not target_label:
                target_label = str(target)

        user_agent = ''
        if request is not None:
            user_agent = request.META.get('HTTP_USER_AGENT', '')[:255]

        return AuditLog.objects.create(
            organization=organization,
            organization_name=getattr(organization, 'name', '')[:255] if organization else '',
            actor=actor,
            actor_email=(getattr(actor, 'email', '') or getattr(actor, 'username', ''))[:254] if actor else '',
            action=action,
            target_type=target_type,
            target_id=target_id[:64],
            target_label=(target_label or '')[:255],
            details=details or {},
            ip_address=client_ip(request),
            user_agent=user_agent,
        )
    except Exception:
        logger.exception('Failed to write audit log entry for %s', action)
        return None
