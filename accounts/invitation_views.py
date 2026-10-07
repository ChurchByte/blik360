"""
Views for organization invitations
"""
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.urls import reverse
from django.utils.html import escape
from django.views.decorators.http import require_POST
from accounts.models import OrganizationInvitation
from accounts.permissions import organization_admin_required
from core.email import send_email
from subscriptions.utils import check_user_limit


def _role_summary(roles, invitation=None):
    """'Organization Admin, Report Viewer (Northside, Southside)' or 'Member'."""
    from accounts.permissions import role_labels, REPORT_VIEWER, ROLE_LABELS
    labels = role_labels(roles)
    if invitation is not None and REPORT_VIEWER in roles:
        if invitation.report_all_campuses:
            scope = 'all campuses'
        else:
            scope = ', '.join(sorted(invitation.report_campuses.values_list('name', flat=True))) \
                or 'no campuses'
        labels = [f'{l} ({scope})' if l == ROLE_LABELS[REPORT_VIEWER] else l for l in labels]
    return ', '.join(labels) or 'Member'


@login_required
@organization_admin_required
def send_invitation(request):
    """
    Send invitation to join organization.

    Only organization administrators can invite new team members. Inviters who
    can manage roles can choose the roles (and Report Viewer campuses) the new
    member gets when they accept; only Owners can invite someone as an Owner.
    """
    from accounts.models import UserProfile
    from accounts.permissions import invitation_roles

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()

        # Get organization from request or user's profile
        org = request.organization
        if not org and hasattr(request.user, 'profile'):
            org = request.user.profile.organization

        if not org:
            messages.error(request, 'No organization found.')
            return redirect('admin_dashboard')

        if not email:
            messages.error(request, 'Email address is required.')
            return redirect('team_list')

        if UserProfile.objects.filter(organization=org, user__email__iexact=email).exists():
            messages.info(
                request,
                f'{email} is already on the team. Use "Manage roles" to change their roles.'
            )
            return redirect('team_list')

        # Check user limit
        allowed, error_message = check_user_limit(request)
        if not allowed:
            messages.error(request, error_message)
            return redirect('team_list')

        # One invitation per organization and email (unique_together)
        existing = OrganizationInvitation.objects.filter(organization=org, email=email).first()

        if existing and existing.is_valid():
            messages.info(
                request,
                f'Invitation already sent to {email}. It grants: '
                f'{_role_summary(invitation_roles(existing), existing)}. '
                'Use Edit or Resend under Pending Invitations to change it.'
            )
            return redirect('team_list')

        if existing:
            # Reuse the expired invitation for this email
            invitation = existing
            invitation.token = ''  # save() generates a fresh token
            invitation.accepted_at = None
        else:
            invitation = OrganizationInvitation(organization=org, email=email)
        invitation.invited_by = request.user
        invitation.expires_at = timezone.now() + timedelta(days=INVITATION_DAYS)
        _apply_role_choices(request, invitation)

        granted = invitation_roles(invitation)
        from core.audit import log_event, Actions
        log_event(request, Actions.MEMBER_INVITED, organization=org, target=invitation,
                  target_label=email, details=_role_details(invitation, granted))

        _send_and_report(request, invitation, f'Invitation sent to {email}')

    return redirect('team_list')


INVITATION_DAYS = 7


def _role_details(invitation, granted):
    """Audit-log details describing what an invitation grants."""
    from accounts.permissions import REPORT_VIEWER
    details = {'roles': sorted(granted)}
    if REPORT_VIEWER in granted:
        details['report_campuses'] = {
            'all_campuses': invitation.report_all_campuses,
            'campuses': sorted(invitation.report_campuses.values_list('name', flat=True)),
        }
    return details


def _apply_role_choices(request, invitation, keep_owner=False):
    """
    Set the invitation's roles and Report Viewer campuses from the dialog's
    POST data and save it. Anything the current user isn't allowed to grant is
    ignored. Without role management the organization's default applies
    (roles=None). keep_owner: an invitation that already grants Owner keeps it
    when edited by someone who can't manage Owners.
    """
    from accounts.models import Campus
    from accounts.permissions import (
        roles_grantable_by, normalize_roles, invitation_roles, OWNER, REPORT_VIEWER,
    )
    grantable = roles_grantable_by(request.user)
    if grantable:
        roles = {r for r in request.POST.getlist('roles') if r in grantable}
        if keep_owner and OWNER not in grantable:
            roles.add(OWNER)
        invitation.roles = sorted(normalize_roles(roles))
    elif invitation.pk is None:
        invitation.roles = None
    invitation.report_all_campuses = request.POST.get('report_all_campuses') == 'on'
    invitation.save()

    if REPORT_VIEWER in invitation_roles(invitation) and not invitation.report_all_campuses:
        campus_ids = [c for c in request.POST.getlist('report_campuses') if str(c).isdigit()]
        invitation.report_campuses.set(Campus.objects.filter(
            organization=invitation.organization, id__in=campus_ids
        ))
    else:
        invitation.report_campuses.clear()


def _send_and_report(request, invitation, success_prefix):
    """Email the invitation and flash the outcome."""
    from accounts.permissions import invitation_roles
    role_summary = _role_summary(invitation_roles(invitation), invitation)
    try:
        _send_invitation_email(request, invitation, role_summary)
        messages.success(request, f'{success_prefix} as: {role_summary}.')
        return True
    except Exception as e:
        messages.error(request, f'Failed to send invitation: {e}')
        return False


def _send_invitation_email(request, invitation, role_summary):
    from accounts.permissions import invitation_roles
    org = invitation.organization
    invite_url = request.build_absolute_uri(
        reverse('accept_invitation', kwargs={'token': invitation.token})
    )
    granted = invitation_roles(invitation)
    role_line = '' if not granted else f'You will join as: {role_summary}.\n\n'
    role_html = '' if not granted else (
        f'<p>You will join as: <strong>{escape(role_summary)}</strong>.</p>'
    )
    send_email(
        subject=f'Invitation to join {org.name} on Lead360',
        message=f'''
Hello,

You've been invited to join {org.name} on Lead360.

{role_line}Click the link below to accept this invitation and create your account:
{invite_url}

This invitation will expire in {INVITATION_DAYS} days.

Best regards,
{org.name} Team
        '''.strip(),
        recipient_list=[invitation.email],
        html_message=f'''
<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2>You're Invited!</h2>
        <p>You've been invited to join <strong>{escape(org.name)}</strong> on Lead360.</p>
        {role_html}
        <p style="margin: 30px 0;">
            <a href="{invite_url}" style="display: inline-block; padding: 12px 24px; background-color: #4f46e5; color: white; text-decoration: none; border-radius: 8px;">Accept Invitation</a>
        </p>
        <p style="color: #666; font-size: 14px;">This invitation will expire in {INVITATION_DAYS} days.</p>
        <p>Best regards,<br>{escape(org.name)} Team</p>
    </div>
</body>
</html>
        ''',
        from_email=org.from_email if org.from_email else None
    )


def _pending_invitation(request, pk):
    """A not-yet-accepted invitation in the current user's organization."""
    org = request.organization
    if not org and hasattr(request.user, 'profile'):
        org = request.user.profile.organization
    return get_object_or_404(
        OrganizationInvitation, pk=pk, organization=org, accepted_at__isnull=True
    )


def _grants_owner(invitation):
    from accounts.permissions import invitation_roles, OWNER
    return OWNER in invitation_roles(invitation)


@login_required
@organization_admin_required
@require_POST
def update_invitation(request, pk):
    """
    Change the roles (and Report Viewer campuses) a pending invitation grants.
    The email and link stay the same; the new roles apply when it's accepted.
    """
    from accounts.permissions import invitation_roles, roles_grantable_by
    invitation = _pending_invitation(request, pk)
    if not roles_grantable_by(request.user):
        messages.error(request, 'You do not have permission to manage user roles.')
        return redirect('team_list')

    before = _role_details(invitation, invitation_roles(invitation))
    _apply_role_choices(request, invitation, keep_owner=_grants_owner(invitation))
    after = _role_details(invitation, invitation_roles(invitation))

    if before == after:
        messages.info(request, 'No changes made.')
        return redirect('team_list')

    from core.audit import log_event, Actions
    log_event(request, Actions.INVITATION_UPDATED, organization=invitation.organization,
              target=invitation, target_label=invitation.email,
              details={'from': before, 'to': after})
    summary = _role_summary(invitation_roles(invitation), invitation)
    messages.success(
        request,
        f'Invitation for {invitation.email} now grants: {summary}. '
        'Use Resend if you\'d like them to get an updated email.'
    )
    return redirect('team_list')


@login_required
@organization_admin_required
@require_POST
def resend_invitation(request, pk):
    """Email the invitation again and give it a fresh 7-day expiry."""
    from core.audit import log_event, Actions
    invitation = _pending_invitation(request, pk)
    was_expired = not invitation.is_valid()
    invitation.expires_at = timezone.now() + timedelta(days=INVITATION_DAYS)
    invitation.save(update_fields=['expires_at', 'updated_at'])
    log_event(request, Actions.INVITATION_RESENT, organization=invitation.organization,
              target=invitation, target_label=invitation.email,
              details={'was_expired': was_expired})
    _send_and_report(request, invitation, f'Invitation resent to {invitation.email}')
    return redirect('team_list')


@login_required
@organization_admin_required
@require_POST
def revoke_invitation(request, pk):
    """
    Cancel a pending invitation: its link stops working straight away.
    Only Owners can revoke an invitation that would make someone an Owner.
    """
    from accounts.permissions import can_manage_owners, invitation_roles
    from core.audit import log_event, Actions
    invitation = _pending_invitation(request, pk)
    if _grants_owner(invitation) and not can_manage_owners(request.user):
        messages.error(request, 'Only an Owner can revoke an invitation that makes someone an Owner.')
        return redirect('team_list')
    email = invitation.email
    org = invitation.organization
    details = _role_details(invitation, invitation_roles(invitation))
    invitation.delete()
    log_event(request, Actions.INVITATION_REVOKED, organization=org,
              target_label=email, details=details)
    messages.success(request, f'Invitation for {email} revoked. Their link no longer works.')
    return redirect('team_list')


def accept_invitation(request, token):
    """Accept invitation - redirects to login if user exists, or shows signup form"""
    invitation = get_object_or_404(OrganizationInvitation, token=token)

    if not invitation.is_valid():
        messages.error(request, 'This invitation has expired or been used.')
        return redirect('login')

    # Check if user already exists with this email
    from django.contrib.auth.models import User
    existing_user = User.objects.filter(email=invitation.email).first()

    if existing_user:
        # User exists - create profile and mark invitation accepted
        from accounts.models import UserProfile
        from accounts.permissions import apply_invitation_roles

        profile, created = UserProfile.objects.get_or_create(
            user=existing_user,
            defaults={
                'organization': invitation.organization,
                'can_create_cycles_for_others': invitation.organization.default_users_can_create_cycles
            }
        )

        if not created and profile.organization != invitation.organization:
            messages.error(
                request,
                f'Your account is already linked to {profile.organization.name}. '
                'Please contact support for multi-organization access.'
            )
            return redirect('login')

        # Give them the roles chosen when they were invited
        if created:
            apply_invitation_roles(existing_user, invitation)

        # Mark invitation accepted
        invitation.accepted_at = timezone.now()
        invitation.save()
        from core.audit import log_event, Actions
        log_event(request, Actions.INVITATION_ACCEPTED, actor=existing_user,
                  organization=invitation.organization, target_label=invitation.email)

        messages.success(
            request,
            f'Welcome to {invitation.organization.name}! Please log in.'
        )
        return redirect('login')

    # New user - show signup form
    # Store invitation token in session for signup
    request.session['invitation_token'] = token
    request.session['invitation_email'] = invitation.email

    messages.info(
        request,
        f'Welcome! Create your account to join {invitation.organization.name}'
    )
    return redirect('signup_from_invitation')
