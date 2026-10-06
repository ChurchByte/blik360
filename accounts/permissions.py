"""
Organization roles and permission checks.

Roles are Django groups and can be combined (a person can be, say, an
Organization Admin and a Report Viewer). Every user in an organization is at
least a Member.

    Role               Permissions (accounts.<codename>)
    -----------------  ---------------------------------------------------------
    Owner              can_manage_billing, can_delete_organization,
                       can_manage_owners, can_view_audit_log
                       (an Owner is always also an Organization Admin;
                       an organization can have several Owners but never none)
    Organization Admin can_manage_organization, can_invite_members,
                       can_manage_cycles
    Cycle Manager      can_manage_cycles
    Report Viewer      can_view_all_reports, can_investigate_responses
    Member             (none) — own cycles and own reports only

Report content is only ever visible to Report Viewers and to the reviewee.
Organization Admins and Cycle Managers can see that cycles exist and how far
along they are, but not the results.
"""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.shortcuts import redirect

from accounts.models import UserProfile


# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------

OWNER = 'owner'
ORG_ADMIN = 'org_admin'
CYCLE_MANAGER = 'cycle_manager'
REPORT_VIEWER = 'report_viewer'

# Group names. ORG_ADMIN_GROUP / ORG_MEMBER_GROUP keep their historical names
# so existing databases keep working.
ORG_OWNER_GROUP = 'Organization Owner'
ORG_ADMIN_GROUP = 'Organization Admin'
CYCLE_MANAGER_GROUP = 'Cycle Manager'
REPORT_VIEWER_GROUP = 'Report Viewer'
ORG_MEMBER_GROUP = 'Organization Member'

ROLE_GROUPS = {
    OWNER: ORG_OWNER_GROUP,
    ORG_ADMIN: ORG_ADMIN_GROUP,
    CYCLE_MANAGER: CYCLE_MANAGER_GROUP,
    REPORT_VIEWER: REPORT_VIEWER_GROUP,
}
GROUP_ROLES = {group: role for role, group in ROLE_GROUPS.items()}

ROLE_LABELS = {
    OWNER: 'Owner',
    ORG_ADMIN: 'Organization Admin',
    CYCLE_MANAGER: 'Cycle Manager',
    REPORT_VIEWER: 'Report Viewer',
}

# Display order
ROLE_ORDER = [OWNER, ORG_ADMIN, CYCLE_MANAGER, REPORT_VIEWER]

# Roles an Organization Admin can grant or remove on the Team page
# (including on themselves). Only Owners can grant or remove Owner.
ASSIGNABLE_ROLES = [ORG_ADMIN, CYCLE_MANAGER, REPORT_VIEWER]
OWNER_ASSIGNABLE_ROLES = [OWNER] + ASSIGNABLE_ROLES

PERMISSION_NAMES = {
    'can_manage_billing': 'Can manage billing and subscription',
    'can_delete_organization': 'Can delete organization',
    'can_manage_owners': 'Can add and remove owners',
    'can_view_audit_log': 'Can view the audit log',
    'can_invite_members': 'Can invite team members',
    'can_manage_organization': 'Can manage organization settings',
    'can_manage_cycles': 'Can create and run review cycles for others',
    'can_view_all_reports': 'Can view all organization reports',
    'can_investigate_responses': 'Can view reviewer identities for investigations',
}

GROUP_PERMISSIONS = {
    ORG_OWNER_GROUP: [
        'can_manage_billing',
        'can_delete_organization',
        'can_manage_owners',
        'can_view_audit_log',
    ],
    ORG_ADMIN_GROUP: [
        'can_manage_organization',
        'can_invite_members',
        'can_manage_cycles',
    ],
    CYCLE_MANAGER_GROUP: ['can_manage_cycles'],
    REPORT_VIEWER_GROUP: ['can_view_all_reports', 'can_investigate_responses'],
    ORG_MEMBER_GROUP: [],
}

# Holding any of these means the account must pass email MFA (accounts/mfa.py).
PRIVILEGED_PERMISSIONS = [
    'accounts.can_manage_organization',
    'accounts.can_manage_billing',
    'accounts.can_view_all_reports',
    'accounts.can_investigate_responses',
]


def ensure_permission_groups():
    """
    Create the role permissions and groups, and make each group's permissions
    match GROUP_PERMISSIONS. Safe to call repeatedly.

    Returns (admin_group, member_group) for backward compatibility.
    """
    from django.apps import apps
    if not apps.ready:
        return None, None

    content_type = ContentType.objects.get_for_model(UserProfile)
    perms = {}
    for codename, name in PERMISSION_NAMES.items():
        perm, _ = Permission.objects.get_or_create(
            codename=codename,
            content_type=content_type,
            defaults={'name': name},
        )
        perms[codename] = perm

    groups = {}
    for group_name, codenames in GROUP_PERMISSIONS.items():
        group, _ = Group.objects.get_or_create(name=group_name)
        wanted = {perms[c].pk for c in codenames}
        current = set(group.permissions.values_list('pk', flat=True))
        if wanted != current:
            group.permissions.set([perms[c] for c in codenames])
        groups[group_name] = group

    return groups[ORG_ADMIN_GROUP], groups[ORG_MEMBER_GROUP]


def _clear_perm_cache(user):
    for attr in ('_perm_cache', '_user_perm_cache', '_group_perm_cache'):
        if hasattr(user, attr):
            delattr(user, attr)


# ---------------------------------------------------------------------------
# Reading and changing roles
# ---------------------------------------------------------------------------

def get_user_roles(user):
    """Return the set of role keys (OWNER, ORG_ADMIN, ...) the user holds."""
    if not user or not getattr(user, 'pk', None):
        return set()
    names = user.groups.filter(name__in=GROUP_ROLES.keys()).values_list('name', flat=True)
    return {GROUP_ROLES[n] for n in names}


def role_labels(roles):
    """Ordered human-readable labels for a set of role keys."""
    return [ROLE_LABELS[r] for r in ROLE_ORDER if r in roles]


def set_user_roles(user, roles):
    """
    Make `roles` the user's exact set of roles.

    - Owner always brings Organization Admin with it.
    - Every user stays in the Member group.
    - is_staff mirrors Organization Admin (historical behaviour).
    - profile.can_create_cycles_for_others mirrors the ability to manage cycles.

    Returns (old_roles, new_roles).
    """
    ensure_permission_groups()
    roles = set(roles)
    unknown = roles - set(ROLE_GROUPS)
    if unknown:
        raise ValueError(f'Unknown role(s): {", ".join(sorted(unknown))}')
    if OWNER in roles:
        roles.add(ORG_ADMIN)

    with transaction.atomic():
        old_roles = get_user_roles(user)
        role_groups = Group.objects.filter(name__in=ROLE_GROUPS.values())
        user.groups.remove(*role_groups)
        user.groups.add(*Group.objects.filter(name__in=[ROLE_GROUPS[r] for r in roles]))
        user.groups.add(Group.objects.get(name=ORG_MEMBER_GROUP))

        is_admin = ORG_ADMIN in roles
        if user.is_staff != is_admin:
            user.is_staff = is_admin
            user.save(update_fields=['is_staff'])

        try:
            profile = UserProfile.objects.get(user=user)
        except UserProfile.DoesNotExist:
            profile = None
        if profile is not None:
            can_cycles = bool(roles & {ORG_ADMIN, CYCLE_MANAGER})
            if profile.can_create_cycles_for_others != can_cycles:
                profile.can_create_cycles_for_others = can_cycles
                profile.save(update_fields=['can_create_cycles_for_others'])

    _clear_perm_cache(user)
    return old_roles, roles


def add_user_roles(user, *roles):
    return set_user_roles(user, get_user_roles(user) | set(roles))


def assign_organization_owner(user):
    """Make the user an Owner (and therefore Organization Admin), keeping other roles."""
    return add_user_roles(user, OWNER, ORG_ADMIN)


def assign_organization_admin(user):
    """Add the Organization Admin role, keeping any other roles."""
    return add_user_roles(user, ORG_ADMIN)


def assign_organization_member(user, can_create_cycles_for_others=False):
    """
    Reset the user to a plain Member, optionally with the Cycle Manager role.

    Used when someone joins from an invitation. Owners keep ownership.
    """
    roles = {CYCLE_MANAGER} if can_create_cycles_for_others else set()
    if OWNER in get_user_roles(user):
        roles |= {OWNER, ORG_ADMIN}
    return set_user_roles(user, roles)


def remove_from_all_org_groups(user):
    """Remove user from all role and member groups (without deleting the groups)."""
    user.groups.remove(*Group.objects.filter(
        name__in=list(ROLE_GROUPS.values()) + [ORG_MEMBER_GROUP]
    ))
    _clear_perm_cache(user)


def get_organization_owners(organization):
    """Users who own the organization, earliest first."""
    return [
        p.user for p in UserProfile.objects.filter(
            organization=organization, user__groups__name=ORG_OWNER_GROUP
        ).select_related('user').order_by('user__date_joined', 'user__id')
    ]


def count_owners(organization):
    return UserProfile.objects.filter(
        organization=organization, user__groups__name=ORG_OWNER_GROUP
    ).count()


def is_last_owner(user):
    """True if the user is an Owner and nobody else in their organization is."""
    if OWNER not in get_user_roles(user):
        return False
    profile = UserProfile.objects.filter(user=user).select_related('organization').first()
    if profile is None:
        return False
    return count_owners(profile.organization) <= 1


# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------

def is_owner(user):
    return OWNER in get_user_roles(user)


def can_manage_owners(user):
    """Owners (and superusers) can make other people Owners or remove them."""
    return user.has_perm('accounts.can_manage_owners')


def is_organization_admin(user):
    """Organization Admin (settings and team management)."""
    return user.has_perm('accounts.can_manage_organization')


def can_invite_members(user):
    return user.has_perm('accounts.can_invite_members')


def can_manage_organization(user):
    return user.has_perm('accounts.can_manage_organization')


def can_delete_organization(user):
    return user.has_perm('accounts.can_delete_organization')


def can_manage_billing(user):
    return user.has_perm('accounts.can_manage_billing')


def can_manage_cycles(user):
    """Create and run review cycles for anyone (Cycle Manager or Org Admin)."""
    return user.has_perm('accounts.can_manage_cycles')


def can_view_all_reports(user):
    """Report Viewer: may read the report content for any reviewee."""
    return user.has_perm('accounts.can_view_all_reports')


def can_investigate_responses(user):
    """Report Viewer: may see which reviewer gave which answers."""
    return user.has_perm('accounts.can_investigate_responses')


def can_view_audit_log(user):
    return user.has_perm('accounts.can_view_audit_log')


def requires_mfa(user):
    """Accounts holding admin or report-viewing permissions must use MFA."""
    if not getattr(user, 'is_authenticated', False):
        return False
    return any(user.has_perm(p) for p in PRIVILEGED_PERMISSIONS)


def _own_filter(user, queryset, email_field):
    if not user.email:
        return queryset.none()
    return queryset.filter(**{f'{email_field}__iexact': user.email})


def visible_cycles(user, queryset, email_field='reviewee__email'):
    """
    Restrict a ReviewCycle queryset to the cycles this user may see and run
    (status, progress, invitations) — NOT the report content; use
    visible_reports() / can_view_cycle_report() for that.

    Cycle Managers, Organization Admins and Report Viewers see every cycle in
    their organization; everyone else only sees cycles where they are the
    reviewee (matched by email — there is no FK from Reviewee to User).

    Organization scoping is a separate concern and must already be applied.
    """
    if not getattr(user, 'is_authenticated', False):
        return queryset.none()
    if can_manage_cycles(user) or can_view_all_reports(user):
        return queryset
    return _own_filter(user, queryset, email_field)


def visible_reports(user, queryset, email_field='cycle__reviewee__email'):
    """
    Restrict a Report queryset (or any queryset whose rows expose report
    content) to what this user may read: everything for Report Viewers, their
    own reports for everyone else.
    """
    if not getattr(user, 'is_authenticated', False):
        return queryset.none()
    if can_view_all_reports(user):
        return queryset
    return _own_filter(user, queryset, email_field)


def is_own_cycle(user, cycle):
    """Reviewees are matched by email — there is no FK from Reviewee to User."""
    return bool(user.email) and cycle.reviewee.email.lower() == user.email.lower()


def can_view_cycle_report(user, cycle):
    return can_view_all_reports(user) or is_own_cycle(user, cycle)


# ---------------------------------------------------------------------------
# View decorators
# ---------------------------------------------------------------------------

def _permission_required(perm, message, default_redirect='admin_dashboard'):
    def factory(view_func=None, redirect_url=default_redirect):
        def decorator(func):
            @wraps(func)
            def wrapper(request, *args, **kwargs):
                if not request.user.has_perm(perm):
                    messages.error(request, message)
                    return redirect(redirect_url)
                return func(request, *args, **kwargs)
            return wrapper
        if view_func is None:
            return decorator
        return decorator(view_func)
    return factory


organization_admin_required = _permission_required(
    'accounts.can_invite_members',
    'You do not have permission to perform this action. '
    'Only organization administrators can access this feature.',
)
can_manage_organization_required = _permission_required(
    'accounts.can_manage_organization',
    'You do not have permission to manage organization settings.',
)
can_delete_organization_required = _permission_required(
    'accounts.can_delete_organization',
    'Only the organization owner can delete the organization.',
)
can_manage_billing_required = _permission_required(
    'accounts.can_manage_billing',
    'Only the organization owner can manage billing.',
)
