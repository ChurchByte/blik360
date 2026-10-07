"""
Tests for choosing a new team member's roles (and Report Viewer campuses)
in the Invite User dialog, and applying them when the invitation is accepted.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Campus, OrganizationInvitation, UserProfile
from accounts.permissions import (
    CYCLE_MANAGER, ORG_ADMIN, OWNER, REPORT_VIEWER, get_user_roles, set_user_roles,
)
from core.audit import Actions
from core.models import AuditLog, Organization

User = get_user_model()

PASSWORD = 'Tr1cky-Passw0rd!x'


def fresh(user):
    return User.objects.get(pk=user.pk)


class InvitationFixture(TestCase):

    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name='Kings Test', email='hr@kings.test')
        self.north = Campus.objects.create(organization=self.org, name='Northside')
        self.south = Campus.objects.create(organization=self.org, name='Southside')
        self.owner = self.make_user('owner', {OWNER})
        self.admin = self.make_user('admin', {ORG_ADMIN})

    def make_user(self, name, roles):
        user = User.objects.create_user(
            username=f'{name}@kings.test', email=f'{name}@kings.test', password='pw-123456!'
        )
        UserProfile.objects.create(user=user, organization=self.org)
        set_user_roles(user, roles)
        return fresh(user)

    def invite(self, by, email='new@kings.test', **data):
        self.client.force_login(by)
        data = {'email': email, **data}
        return self.client.post(reverse('send_invitation'), data)

    def accept_as_new_user(self, invitation):
        self.client.logout()
        self.client.get(reverse('accept_invitation', kwargs={'token': invitation.token}))
        self.client.post(reverse('signup_from_invitation'), {
            'email': invitation.email, 'password1': PASSWORD, 'password2': PASSWORD,
        })
        return User.objects.get(email=invitation.email)



class InvitationRoleTests(InvitationFixture):

    # -- Sending -----------------------------------------------------------

    def test_roles_are_stored_on_the_invitation_and_audited(self):
        self.invite(self.admin, roles=['cycle_manager', 'report_viewer'],
                    report_campuses=[self.north.id])
        inv = OrganizationInvitation.objects.get(email='new@kings.test')
        self.assertEqual(inv.roles, ['cycle_manager', 'report_viewer'])
        self.assertFalse(inv.report_all_campuses)
        self.assertEqual(list(inv.report_campuses.all()), [self.north])
        entry = AuditLog.objects.get(action=Actions.MEMBER_INVITED)
        self.assertEqual(entry.details['roles'], ['cycle_manager', 'report_viewer'])
        self.assertEqual(entry.details['report_campuses']['campuses'], ['Northside'])
        self.assertIn('Cycle Manager', mail.outbox[0].body)

    def test_no_roles_ticked_means_plain_member(self):
        self.invite(self.admin)
        inv = OrganizationInvitation.objects.get(email='new@kings.test')
        self.assertEqual(inv.roles, [])
        user = self.accept_as_new_user(inv)
        self.assertEqual(get_user_roles(user), set())

    def test_admin_cannot_invite_an_owner(self):
        self.invite(self.admin, roles=['owner', 'report_viewer'])
        inv = OrganizationInvitation.objects.get(email='new@kings.test')
        self.assertEqual(inv.roles, ['report_viewer'])

    def test_owner_can_invite_an_owner(self):
        self.invite(self.owner, roles=['owner'])
        inv = OrganizationInvitation.objects.get(email='new@kings.test')
        self.assertEqual(set(inv.roles), {OWNER, ORG_ADMIN})
        user = self.accept_as_new_user(inv)
        self.assertEqual(get_user_roles(user), {OWNER, ORG_ADMIN})

    def test_role_rules_applied(self):
        self.invite(self.owner, roles=['org_admin', 'cycle_manager', 'bogus'])
        inv = OrganizationInvitation.objects.get(email='new@kings.test')
        self.assertEqual(inv.roles, ['org_admin'])

    def test_campuses_from_other_orgs_are_ignored(self):
        other = Organization.objects.create(name='Other', email='x@other.test')
        foreign = Campus.objects.create(organization=other, name='Elsewhere')
        self.invite(self.admin, roles=['report_viewer'],
                    report_campuses=[foreign.id, self.south.id])
        inv = OrganizationInvitation.objects.get(email='new@kings.test')
        self.assertEqual(list(inv.report_campuses.all()), [self.south])

    def test_existing_team_member_is_not_reinvited(self):
        response = self.invite(self.admin, email='owner@kings.test', roles=['org_admin'])
        self.assertFalse(OrganizationInvitation.objects.filter(email='owner@kings.test').exists())
        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('already on the team' in m for m in msgs))

    def test_expired_invitation_can_be_resent_with_new_roles(self):
        old = OrganizationInvitation.objects.create(
            organization=self.org, email='new@kings.test', invited_by=self.admin,
            expires_at=timezone.now() - timedelta(days=1), roles=[],
        )
        old_token = old.token
        self.invite(self.admin, roles=['cycle_manager'])
        inv = OrganizationInvitation.objects.get(email='new@kings.test')
        self.assertEqual(inv.pk, old.pk)
        self.assertNotEqual(inv.token, old_token)
        self.assertTrue(inv.is_valid())
        self.assertEqual(inv.roles, ['cycle_manager'])

    # -- Accepting ---------------------------------------------------------

    def test_new_user_gets_invited_roles_and_campus_scope(self):
        self.invite(self.admin, roles=['report_viewer'],
                    report_campuses=[self.north.id, self.south.id])
        inv = OrganizationInvitation.objects.get(email='new@kings.test')
        user = self.accept_as_new_user(inv)
        self.assertEqual(get_user_roles(user), {REPORT_VIEWER})
        profile = user.profile
        self.assertFalse(profile.report_all_campuses)
        self.assertEqual(set(profile.report_campuses.all()), {self.north, self.south})

    def test_report_viewer_all_campuses(self):
        self.invite(self.admin, roles=['report_viewer'], report_all_campuses='on')
        inv = OrganizationInvitation.objects.get(email='new@kings.test')
        user = self.accept_as_new_user(inv)
        self.assertTrue(user.profile.report_all_campuses)
        self.assertFalse(user.profile.report_campuses.exists())

    def test_existing_account_gets_invited_roles(self):
        user = User.objects.create_user(
            username='new@kings.test', email='new@kings.test', password=PASSWORD
        )
        self.invite(self.admin, roles=['org_admin'])
        inv = OrganizationInvitation.objects.get(email='new@kings.test')
        self.client.logout()
        self.client.get(reverse('accept_invitation', kwargs={'token': inv.token}))
        user = fresh(user)
        self.assertEqual(user.profile.organization, self.org)
        self.assertEqual(get_user_roles(user), {ORG_ADMIN})
        self.assertTrue(user.is_staff)

    def test_legacy_invitation_uses_org_default(self):
        self.org.default_users_can_create_cycles = True
        self.org.save()
        inv = OrganizationInvitation.objects.create(
            organization=self.org, email='legacy@kings.test', invited_by=self.admin,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.assertIsNone(inv.roles)
        user = self.accept_as_new_user(inv)
        self.assertEqual(get_user_roles(user), {CYCLE_MANAGER})

    # -- Team page ---------------------------------------------------------

    def test_team_page_shows_role_choices_and_pending_roles(self):
        self.invite(self.admin, roles=['report_viewer'], report_campuses=[self.north.id])
        response = self.client.get(reverse('team_list'))
        self.assertContains(response, 'id="invite-role-report-viewer"')
        self.assertContains(response, 'id="invite-campus-%d"' % self.north.id)
        self.assertNotContains(response, 'id="invite-role-owner"')  # admins can't grant Owner
        self.assertContains(response, 'Report Viewer &middot; Northside')

        self.client.force_login(self.owner)
        response = self.client.get(reverse('team_list'))
        self.assertContains(response, 'id="invite-role-owner"')


class PendingInvitationManagementTests(InvitationFixture):
    """Edit, resend and revoke on the Pending Invitations list."""

    def make_invitation(self, roles=(), email='pending@kings.test', expired=False, **extra):
        return OrganizationInvitation.objects.create(
            organization=self.org, email=email, invited_by=self.admin,
            expires_at=timezone.now() + timedelta(days=-1 if expired else 7),
            roles=list(roles), **extra,
        )

    def post(self, user, name, inv, **data):
        self.client.force_login(user)
        return self.client.post(reverse(name, kwargs={'pk': inv.pk}), data)

    # -- Edit --------------------------------------------------------------

    def test_edit_changes_roles_and_campuses_and_is_audited(self):
        inv = self.make_invitation(['cycle_manager'])
        token = inv.token
        self.post(self.admin, 'update_invitation', inv,
                  roles=['report_viewer'], report_campuses=[self.south.id])
        inv.refresh_from_db()
        self.assertEqual(inv.roles, ['report_viewer'])
        self.assertFalse(inv.report_all_campuses)
        self.assertEqual(list(inv.report_campuses.all()), [self.south])
        self.assertEqual(inv.token, token)  # same link still works
        entry = AuditLog.objects.get(action=Actions.INVITATION_UPDATED)
        self.assertEqual(entry.details['from']['roles'], ['cycle_manager'])
        self.assertEqual(entry.details['to']['roles'], ['report_viewer'])
        self.assertEqual(len(mail.outbox), 0)  # editing doesn't email

        user = self.accept_as_new_user(inv)
        self.assertEqual(get_user_roles(user), {REPORT_VIEWER})
        self.assertEqual(list(user.profile.report_campuses.all()), [self.south])

    def test_edit_ignores_email_field(self):
        inv = self.make_invitation()
        self.post(self.admin, 'update_invitation', inv, email='other@kings.test',
                  roles=['cycle_manager'])
        inv.refresh_from_db()
        self.assertEqual(inv.email, 'pending@kings.test')

    def test_admin_edit_keeps_owner_on_owner_invitation(self):
        inv = self.make_invitation(['owner', 'org_admin'])
        self.post(self.admin, 'update_invitation', inv, roles=['report_viewer'])
        inv.refresh_from_db()
        self.assertEqual(set(inv.roles), {OWNER, ORG_ADMIN, REPORT_VIEWER})

    def test_owner_can_remove_owner_from_invitation(self):
        inv = self.make_invitation(['owner', 'org_admin'])
        self.post(self.owner, 'update_invitation', inv, roles=['cycle_manager'])
        inv.refresh_from_db()
        self.assertEqual(inv.roles, ['cycle_manager'])

    def test_cannot_edit_another_orgs_invitation(self):
        other = Organization.objects.create(name='Other', email='x@other.test')
        inv = OrganizationInvitation.objects.create(
            organization=other, email='x@other.test', invited_by=self.admin,
            expires_at=timezone.now() + timedelta(days=7), roles=[],
        )
        response = self.post(self.admin, 'update_invitation', inv, roles=['org_admin'])
        self.assertEqual(response.status_code, 404)
        inv.refresh_from_db()
        self.assertEqual(inv.roles, [])

    def test_members_cannot_edit_resend_or_revoke(self):
        member = self.make_user('member', set())
        inv = self.make_invitation(['cycle_manager'])
        for name in ('update_invitation', 'resend_invitation', 'revoke_invitation'):
            self.post(member, name, inv, roles=['org_admin'])
        inv.refresh_from_db()
        self.assertEqual(inv.roles, ['cycle_manager'])
        self.assertEqual(len(mail.outbox), 0)

    def test_get_is_not_allowed(self):
        inv = self.make_invitation()
        self.client.force_login(self.admin)
        for name in ('update_invitation', 'resend_invitation', 'revoke_invitation'):
            response = self.client.get(reverse(name, kwargs={'pk': inv.pk}))
            self.assertEqual(response.status_code, 405)
        self.assertTrue(OrganizationInvitation.objects.filter(pk=inv.pk).exists())

    # -- Resend ------------------------------------------------------------

    def test_resend_emails_again_and_renews_expired_invitation(self):
        inv = self.make_invitation(['cycle_manager'], expired=True)
        self.assertFalse(inv.is_valid())
        self.post(self.admin, 'resend_invitation', inv)
        inv.refresh_from_db()
        self.assertTrue(inv.is_valid())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['pending@kings.test'])
        self.assertIn(inv.token, mail.outbox[0].body)
        self.assertIn('Cycle Manager', mail.outbox[0].body)
        entry = AuditLog.objects.get(action=Actions.INVITATION_RESENT)
        self.assertTrue(entry.details['was_expired'])

    def test_accepted_invitations_cannot_be_resent(self):
        inv = self.make_invitation(accepted_at=timezone.now())
        response = self.post(self.admin, 'resend_invitation', inv)
        self.assertEqual(response.status_code, 404)

    # -- Revoke ------------------------------------------------------------

    def test_revoke_deletes_invitation_and_link_stops_working(self):
        inv = self.make_invitation(['report_viewer'])
        token = inv.token
        self.post(self.admin, 'revoke_invitation', inv)
        self.assertFalse(OrganizationInvitation.objects.filter(pk=inv.pk).exists())
        entry = AuditLog.objects.get(action=Actions.INVITATION_REVOKED)
        self.assertEqual(entry.target_label, 'pending@kings.test')
        self.assertEqual(entry.details['roles'], ['report_viewer'])
        self.client.logout()
        response = self.client.get(reverse('accept_invitation', kwargs={'token': token}))
        self.assertEqual(response.status_code, 404)

    def test_revoked_email_can_be_invited_again(self):
        inv = self.make_invitation()
        self.post(self.admin, 'revoke_invitation', inv)
        self.invite(self.admin, email='pending@kings.test', roles=['cycle_manager'])
        self.assertTrue(OrganizationInvitation.objects.get(email='pending@kings.test').is_valid())

    def test_only_owner_can_revoke_owner_invitation(self):
        inv = self.make_invitation(['owner', 'org_admin'])
        self.post(self.admin, 'revoke_invitation', inv)
        self.assertTrue(OrganizationInvitation.objects.filter(pk=inv.pk).exists())
        self.post(self.owner, 'revoke_invitation', inv)
        self.assertFalse(OrganizationInvitation.objects.filter(pk=inv.pk).exists())

    # -- Team page ---------------------------------------------------------

    def test_team_page_shows_actions(self):
        inv = self.make_invitation(['cycle_manager'])
        owner_inv = self.make_invitation(['owner', 'org_admin'], email='boss@kings.test')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('team_list'))
        self.assertContains(response, reverse('update_invitation', kwargs={'pk': inv.pk}))
        self.assertContains(response, reverse('resend_invitation', kwargs={'pk': inv.pk}))
        self.assertContains(response, reverse('revoke_invitation', kwargs={'pk': inv.pk}))
        self.assertNotContains(response, reverse('revoke_invitation', kwargs={'pk': owner_inv.pk}))
        self.assertContains(response, 'id="invite-owner-note"')
