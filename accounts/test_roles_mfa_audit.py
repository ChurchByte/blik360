"""
Tests for the split admin roles (Owner / Organization Admin / Cycle Manager /
Report Viewer / Member), Report Viewer investigation access, the audit log,
email-code MFA, and the reviewer privacy notice.
"""
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import EmailMFACode, UserProfile
from accounts.permissions import (
    CYCLE_MANAGER, ORG_ADMIN, OWNER, REPORT_VIEWER,
    add_user_roles, get_user_roles, requires_mfa, set_user_roles, transfer_ownership,
)
from core.audit import Actions
from core.models import AuditLog, Organization
from questionnaires.models import Question, QuestionSection, Questionnaire
from reports.services import generate_report
from reviews.models import ReviewCycle, ReviewerToken, Response

User = get_user_model()


def fresh(user):
    """Re-fetch so Django's per-instance permission cache is empty."""
    return User.objects.get(pk=user.pk)


class OrgFixture(TestCase):
    """An organization with one person per role and a completed cycle."""

    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name='Kings Test', email='hr@kings.test')
        self.owner = self.make_user('owner', {OWNER})
        self.admin = self.make_user('admin', {ORG_ADMIN})
        self.cycle_manager = self.make_user('cm', {CYCLE_MANAGER})
        self.report_viewer = self.make_user('hr', {REPORT_VIEWER})
        self.member = self.make_user('member', set())
        self.subject = self.make_user('subject', set())

        self.questionnaire = Questionnaire.objects.create(organization=self.org, name='360')
        section = QuestionSection.objects.create(questionnaire=self.questionnaire, title='Leadership', order=0)
        self.question = Question.objects.create(
            section=section, question_text='What should they keep doing?',
            question_type='text', order=0, config={},
        )
        reviewee = self.subject.profile.organization.reviewees.get(email=self.subject.email)
        self.cycle = ReviewCycle.objects.create(
            reviewee=reviewee, questionnaire=self.questionnaire,
            created_by=self.owner, status='active',
        )
        self.invited_token = ReviewerToken.objects.create(
            cycle=self.cycle, category='peer', reviewer_email='peer.one@kings.test',
            invitation_sent_at=timezone.now(), claimed_at=timezone.now(), completed_at=timezone.now(),
        )
        Response.objects.create(
            cycle=self.cycle, question=self.question, token=self.invited_token,
            category='peer', answer_data={'value': 'Clear weekly priorities'},
        )
        self.link_token = ReviewerToken.objects.create(
            cycle=self.cycle, category='peer', claimed_at=timezone.now(), completed_at=timezone.now(),
        )
        Response.objects.create(
            cycle=self.cycle, question=self.question, token=self.link_token,
            category='peer', answer_data={'value': 'Listens well'},
        )
        self.cycle.status = 'completed'
        self.cycle.save()
        self.report = generate_report(self.cycle)

    def make_user(self, name, roles):
        user = User.objects.create_user(
            username=f'{name}@kings.test', email=f'{name}@kings.test', password='pw-123456!'
        )
        UserProfile.objects.create(user=user, organization=self.org)
        set_user_roles(user, roles)
        return fresh(user)

    def login(self, user):
        self.client.force_login(user)

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


# ---------------------------------------------------------------------------
# Role model
# ---------------------------------------------------------------------------

class RoleModelTests(OrgFixture):

    def test_permissions_per_role(self):
        matrix = {
            'owner': (self.owner, {
                'can_manage_billing', 'can_delete_organization', 'can_transfer_ownership',
                'can_view_audit_log', 'can_manage_organization', 'can_invite_members',
                'can_manage_cycles'}),
            'admin': (self.admin, {'can_manage_organization', 'can_invite_members', 'can_manage_cycles'}),
            'cm': (self.cycle_manager, {'can_manage_cycles'}),
            'rv': (self.report_viewer, {'can_view_all_reports', 'can_investigate_responses'}),
            'member': (self.member, set()),
        }
        all_perms = {
            'can_manage_billing', 'can_delete_organization', 'can_transfer_ownership',
            'can_view_audit_log', 'can_manage_organization', 'can_invite_members',
            'can_manage_cycles', 'can_view_all_reports', 'can_investigate_responses',
        }
        for label, (user, expected) in matrix.items():
            for perm in all_perms:
                with self.subTest(role=label, perm=perm):
                    self.assertEqual(user.has_perm(f'accounts.{perm}'), perm in expected)

    def test_owner_always_includes_org_admin(self):
        set_user_roles(self.member, {OWNER})
        self.assertEqual(get_user_roles(self.member), {OWNER, ORG_ADMIN})

    def test_roles_sync_staff_flag_and_cycle_flag(self):
        set_user_roles(self.member, {CYCLE_MANAGER})
        user = fresh(self.member)
        self.assertFalse(user.is_staff)
        self.assertTrue(user.profile.can_create_cycles_for_others)
        set_user_roles(user, {ORG_ADMIN})
        user = fresh(user)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.profile.can_create_cycles_for_others)
        set_user_roles(user, set())
        user = fresh(user)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.profile.can_create_cycles_for_others)
        self.assertTrue(user.groups.filter(name='Organization Member').exists())

    def test_unknown_role_rejected(self):
        with self.assertRaises(ValueError):
            set_user_roles(self.member, {'superhero'})

    def test_transfer_ownership(self):
        transfer_ownership(self.org, self.owner, self.report_viewer)
        self.assertEqual(get_user_roles(self.owner), {ORG_ADMIN})
        self.assertEqual(get_user_roles(self.report_viewer), {OWNER, ORG_ADMIN, REPORT_VIEWER})

    def test_transfer_ownership_requires_same_org(self):
        other_org = Organization.objects.create(name='Other')
        outsider = User.objects.create_user(username='out', email='out@x.test', password='x')
        UserProfile.objects.create(user=outsider, organization=other_org)
        with self.assertRaises(ValueError):
            transfer_ownership(self.org, self.owner, outsider)

    def test_mfa_required_for_admin_roles_and_report_viewers_only(self):
        self.assertTrue(requires_mfa(self.owner))
        self.assertTrue(requires_mfa(self.admin))
        self.assertTrue(requires_mfa(self.report_viewer))
        self.assertFalse(requires_mfa(self.cycle_manager))
        self.assertFalse(requires_mfa(self.member))


# ---------------------------------------------------------------------------
# Team page: changing roles and ownership
# ---------------------------------------------------------------------------

class TeamRoleManagementTests(OrgFixture):

    def post_roles(self, target, roles):
        return self.client.post(reverse('update_user_permissions'), {
            'user_profile_id': target.profile.id, 'roles': roles,
        })

    def test_admin_can_grant_report_viewer_and_it_is_audited(self):
        self.login(self.admin)
        self.post_roles(self.member, ['report_viewer'])
        self.assertEqual(get_user_roles(self.member), {REPORT_VIEWER})
        entry = AuditLog.objects.get(action=Actions.ROLES_CHANGED)
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.organization, self.org)
        self.assertEqual(entry.details['added'], ['report_viewer'])

    def test_cycle_manager_ignored_for_admins(self):
        self.login(self.owner)
        self.post_roles(self.member, ['org_admin', 'cycle_manager'])
        self.assertEqual(get_user_roles(self.member), {ORG_ADMIN})

    def test_cannot_change_own_roles(self):
        self.login(self.admin)
        self.post_roles(self.admin, ['report_viewer'])
        self.assertEqual(get_user_roles(self.admin), {ORG_ADMIN})

    def test_owner_keeps_admin_role(self):
        self.login(self.admin)
        self.post_roles(self.owner, ['report_viewer'])
        self.assertEqual(get_user_roles(self.owner), {OWNER, ORG_ADMIN, REPORT_VIEWER})

    def test_owner_role_cannot_be_granted_on_team_page(self):
        self.login(self.admin)
        self.post_roles(self.member, ['owner', 'org_admin'])
        self.assertEqual(get_user_roles(self.member), {ORG_ADMIN})

    def test_non_admins_cannot_change_roles(self):
        for user in (self.cycle_manager, self.report_viewer, self.member):
            with self.subTest(user=user.email):
                self.login(user)
                self.post_roles(self.subject, ['report_viewer'])
                self.assertEqual(get_user_roles(self.subject), set())

    def test_transfer_ownership_view(self):
        self.login(self.owner)
        response = self.client.post(reverse('transfer_ownership'), {
            'user_profile_id': self.admin.profile.id, 'password': 'pw-123456!',
        })
        self.assertRedirects(response, reverse('team_list'), fetch_redirect_response=False)
        self.assertIn(OWNER, get_user_roles(self.admin))
        self.assertNotIn(OWNER, get_user_roles(self.owner))
        self.assertTrue(AuditLog.objects.filter(action=Actions.OWNERSHIP_TRANSFERRED).exists())

    def test_transfer_ownership_needs_password(self):
        self.login(self.owner)
        self.client.post(reverse('transfer_ownership'), {
            'user_profile_id': self.admin.profile.id, 'password': 'wrong',
        })
        self.assertIn(OWNER, get_user_roles(self.owner))

    def test_only_owner_can_transfer(self):
        self.login(self.admin)
        self.client.post(reverse('transfer_ownership'), {
            'user_profile_id': self.member.profile.id, 'password': 'pw-123456!',
        })
        self.assertNotIn(OWNER, get_user_roles(self.member))

    def test_team_page_renders_roles(self):
        self.login(self.owner)
        response = self.client.get(reverse('team_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Report Viewer')
        self.assertContains(response, 'Make owner')


# ---------------------------------------------------------------------------
# Who can see what
# ---------------------------------------------------------------------------

class ReportAccessTests(OrgFixture):

    def test_admin_and_cycle_manager_see_progress_but_not_report(self):
        for user in (self.owner, self.admin, self.cycle_manager):
            with self.subTest(user=user.email):
                self.login(user)
                detail = self.client.get(reverse('review_cycle_detail', args=[self.cycle.uuid]))
                self.assertEqual(detail.status_code, 200)
                # The tokenised reviewee link opens the report without login.
                self.assertNotContains(detail, str(self.report.access_token))
                self.assertNotContains(detail, reverse('reports:view_report', args=[self.cycle.uuid]))
                self.assertContains(detail, 'only visible to')

                report = self.client.get(reverse('reports:view_report', args=[self.cycle.uuid]))
                self.assertEqual(report.status_code, 404)

    def test_report_viewer_can_read_report_and_view_is_audited(self):
        self.login(self.report_viewer)
        detail = self.client.get(reverse('review_cycle_detail', args=[self.cycle.uuid]))
        self.assertContains(detail, reverse('reports:view_report', args=[self.cycle.uuid]))
        response = self.client.get(reverse('reports:view_report', args=[self.cycle.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(
            action=Actions.REPORT_VIEWED, actor=self.report_viewer).exists())

    def test_member_cannot_see_other_cycles(self):
        self.login(self.member)
        response = self.client.get(reverse('review_cycle_detail', args=[self.cycle.uuid]))
        self.assertEqual(response.status_code, 404)

    def test_reviewee_reaches_own_report(self):
        self.login(self.subject)
        response = self.client.get(reverse('reports:view_report', args=[self.cycle.uuid]))
        self.assertRedirects(
            response, reverse('reports:reviewee_report', args=[self.report.access_token]),
            fetch_redirect_response=False,
        )

    def test_api_complete_hides_report_url_from_cycle_managers(self):
        cycle = ReviewCycle.objects.create(
            reviewee=self.cycle.reviewee, questionnaire=self.questionnaire,
            created_by=self.owner, status='active',
        )
        self.login(self.cycle_manager)
        response = self.client.post(f'/api/v1/cycles/{cycle.uuid}/complete/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('report_url', response.json())


class CycleCreationTests(OrgFixture):

    def test_member_cannot_create_cycle_for_someone_else(self):
        self.login(self.member)
        before = ReviewCycle.objects.count()
        self.client.post(reverse('review_cycle_create'), {
            'creation_mode': 'single', 'questionnaire': self.questionnaire.id,
            'reviewee': self.cycle.reviewee.id,
        })
        self.assertEqual(ReviewCycle.objects.count(), before)

    def test_member_cannot_bulk_create(self):
        self.login(self.member)
        before = ReviewCycle.objects.count()
        self.client.post(reverse('review_cycle_create'), {
            'creation_mode': 'bulk', 'questionnaire': self.questionnaire.id,
        })
        self.assertEqual(ReviewCycle.objects.count(), before)

    def test_member_can_create_own_cycle(self):
        own = self.org.reviewees.get(email=self.member.email)
        self.login(self.member)
        self.client.post(reverse('review_cycle_create'), {
            'creation_mode': 'single', 'questionnaire': self.questionnaire.id, 'reviewee': own.id,
        })
        self.assertTrue(ReviewCycle.objects.filter(reviewee=own).exists())

    def test_cycle_manager_can_create_for_others(self):
        self.login(self.cycle_manager)
        self.client.post(reverse('review_cycle_create'), {
            'creation_mode': 'single', 'questionnaire': self.questionnaire.id,
            'reviewee': self.cycle.reviewee.id,
        })
        self.assertEqual(ReviewCycle.objects.filter(reviewee=self.cycle.reviewee).count(), 2)
        self.assertTrue(AuditLog.objects.filter(action=Actions.CYCLE_CREATED).exists())


class OrganizationDataTests(OrgFixture):

    def test_export_needs_admin_and_report_viewer(self):
        for user in (self.member, self.admin, self.report_viewer, self.owner):
            with self.subTest(user=user.email):
                self.login(user)
                response = self.client.get(reverse('account:export_data'))
                self.assertEqual(response.status_code, 302)
        add_user_roles(self.admin, REPORT_VIEWER)
        self.login(fresh(self.admin))
        response = self.client.get(reverse('account:export_data'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action=Actions.DATA_EXPORTED).exists())

    def test_only_owner_manages_billing(self):
        self.login(self.admin)
        response = self.client.post(reverse('account:cancel_subscription'))
        self.assertIn('Only the organization owner can manage billing.', self.messages(response))

    def test_only_owner_can_delete_organization(self):
        self.login(self.admin)
        self.client.post(reverse('account:delete_organization'), {
            'password': 'pw-123456!', 'organization_name': self.org.name,
        })
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())

    def test_owner_can_delete_organization_and_audit_rows_survive(self):
        empty_org = Organization.objects.create(name='Empty Org')
        boss = User.objects.create_user(username='boss', email='boss@empty.test', password='pw-123456!')
        UserProfile.objects.create(user=boss, organization=empty_org)
        set_user_roles(boss, {OWNER})
        self.login(fresh(boss))
        self.client.post(reverse('account:delete_organization'), {
            'password': 'pw-123456!', 'organization_name': 'Empty Org',
        })
        self.assertFalse(Organization.objects.filter(pk=empty_org.pk).exists())
        entry = AuditLog.objects.get(action=Actions.ORGANIZATION_DELETED)
        self.assertIsNone(entry.organization)
        self.assertEqual(entry.organization_name, 'Empty Org')

    def test_superuser_is_not_treated_as_owner_for_transfer(self):
        root = User.objects.create_superuser('root', 'root@kings.test', 'pw-123456!')
        self.login(root)
        self.client.post(reverse('transfer_ownership'), {
            'user_profile_id': self.member.profile.id, 'password': 'pw-123456!',
        })
        self.assertNotIn(OWNER, get_user_roles(self.member))

    def test_owner_cannot_delete_own_account_without_transfer(self):
        self.login(self.owner)
        self.client.post(reverse('account:delete_account'), {'password': 'pw-123456!'})
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())

    def test_settings_change_is_audited_without_password(self):
        self.login(self.admin)
        self.client.post(reverse('settings'), {
            'section': 'reports', 'min_responses_for_anonymity': '4',
        })
        entry = AuditLog.objects.get(action=Actions.SETTINGS_CHANGED)
        self.assertEqual(entry.details['changes']['min_responses_for_anonymity']['to'], 4)

    def test_pages_render_for_every_role(self):
        pages = ['settings', 'admin_dashboard', 'profile', 'review_cycle_list', 'reviewee_list']
        for user in (self.owner, self.admin, self.cycle_manager, self.report_viewer, self.member):
            for page in pages:
                with self.subTest(user=user.email, page=page):
                    self.login(user)
                    self.assertEqual(self.client.get(reverse(page)).status_code, 200)
        self.login(self.owner)
        response = self.client.get(reverse('settings'))
        self.assertContains(response, 'onclick="showDeleteOrgModal()"')
        self.assertNotContains(response, 'Download Export')
        self.login(self.admin)
        self.assertNotContains(self.client.get(reverse('settings')), 'onclick="showDeleteOrgModal()"')

    def test_setup_pages_locked_after_setup(self):
        self.login(self.member)
        response = self.client.post(reverse('setup_email'), {'smtp_host': 'evil.example'})
        self.assertRedirects(response, reverse('admin_dashboard'), fetch_redirect_response=False)
        self.org.refresh_from_db()
        self.assertNotEqual(self.org.smtp_host, 'evil.example')


# ---------------------------------------------------------------------------
# Investigation access to reviewer identities
# ---------------------------------------------------------------------------

class InvestigationTests(OrgFixture):

    def url(self):
        return reverse('reports:investigate_responses', args=[self.cycle.uuid])

    def test_requires_reason_and_confirmation(self):
        self.login(self.report_viewer)
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        response = self.client.post(self.url(), {'reason': 'too short', 'confirm': 'on'})
        self.assertNotContains(response, 'peer.one@kings.test')
        response = self.client.post(self.url(), {'reason': 'x' * 40})
        self.assertNotContains(response, 'peer.one@kings.test')
        self.assertFalse(AuditLog.objects.filter(action=Actions.INVESTIGATION_ACCESS).exists())

    def test_shows_identities_and_records_reason(self):
        self.login(self.report_viewer)
        reason = 'Formal complaint HR-2026-14 about a comment in this review.'
        response = self.client.post(self.url(), {
            'reason': reason, 'reference': 'HR-2026-14', 'confirm': 'on',
        })
        self.assertContains(response, 'peer.one@kings.test')
        self.assertContains(response, 'Clear weekly priorities')
        self.assertContains(response, 'Not identifiable')
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        entry = AuditLog.objects.get(action=Actions.INVESTIGATION_ACCESS)
        self.assertEqual(entry.actor, self.report_viewer)
        self.assertEqual(entry.details['reason'], reason)
        self.assertEqual(entry.details['identified_reviewers'], 1)
        self.assertNotIn('Clear weekly priorities', str(entry.details))

    def test_other_roles_cannot_investigate(self):
        for user in (self.owner, self.admin, self.cycle_manager, self.member):
            with self.subTest(user=user.email):
                self.login(user)
                response = self.client.post(self.url(), {'reason': 'x' * 40, 'confirm': 'on'})
                self.assertEqual(response.status_code, 404)

    def test_report_viewer_cannot_investigate_own_feedback(self):
        add_user_roles(self.subject, REPORT_VIEWER)
        self.login(fresh(self.subject))
        response = self.client.post(self.url(), {'reason': 'x' * 40, 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditLog.objects.filter(action=Actions.INVESTIGATION_DENIED).exists())


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditLogTests(OrgFixture):

    def test_entries_cannot_be_edited(self):
        entry = AuditLog.objects.create(organization=self.org, action='test')
        entry.action = 'changed'
        with self.assertRaises(ValueError):
            entry.save()

    def test_only_owner_sees_audit_log(self):
        AuditLog.objects.create(organization=self.org, action=Actions.REPORT_VIEWED,
                                actor_email='hr@kings.test', target_label='Subject')
        self.login(self.owner)
        response = self.client.get(reverse('audit_log'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Report viewed')
        for user in (self.admin, self.report_viewer, self.member):
            with self.subTest(user=user.email):
                self.login(user)
                self.assertEqual(self.client.get(reverse('audit_log')).status_code, 302)

    def test_audit_log_is_scoped_to_organization(self):
        other = Organization.objects.create(name='Other Org')
        AuditLog.objects.create(organization=other, action=Actions.REPORT_VIEWED,
                                target_label='Someone Else')
        self.login(self.owner)
        response = self.client.get(reverse('audit_log'))
        self.assertNotContains(response, 'Someone Else')

    def test_login_is_audited(self):
        self.login(self.member)
        self.assertTrue(AuditLog.objects.filter(action=Actions.LOGIN, actor=self.member).exists())


# ---------------------------------------------------------------------------
# Email-code MFA
# ---------------------------------------------------------------------------

@override_settings(MFA_REQUIRED=True)
class MFATests(OrgFixture):

    def latest_code(self):
        return re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)

    def test_privileged_user_is_held_until_verified(self):
        self.login(self.admin)
        response = self.client.get(reverse('team_list'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(reverse('mfa_verify')))

        response = self.client.get(response['Location'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.admin.email])

        response = self.client.post(reverse('mfa_verify'), {
            'code': self.latest_code(), 'next': reverse('team_list'),
        })
        self.assertRedirects(response, reverse('team_list'), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('team_list')).status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action=Actions.MFA_VERIFIED, actor=self.admin).exists())

    def test_report_viewer_needs_mfa_but_cycle_manager_and_member_do_not(self):
        self.login(self.report_viewer)
        self.assertEqual(self.client.get(reverse('admin_dashboard')).status_code, 302)
        for user in (self.cycle_manager, self.member):
            with self.subTest(user=user.email):
                self.login(user)
                self.assertEqual(self.client.get(reverse('admin_dashboard')).status_code, 200)

    def test_wrong_code_counts_attempts_and_locks(self):
        self.login(self.admin)
        self.client.get(reverse('mfa_verify'))
        good = self.latest_code()
        bad = '000000' if good != '000000' else '111111'
        for _ in range(5):
            self.client.post(reverse('mfa_verify'), {'code': bad})
        self.client.post(reverse('mfa_verify'), {'code': good})
        self.assertEqual(self.client.get(reverse('team_list')).status_code, 302)
        self.assertEqual(AuditLog.objects.filter(action=Actions.MFA_FAILED).count(), 6)

    def test_expired_code_rejected(self):
        self.login(self.admin)
        self.client.get(reverse('mfa_verify'))
        code = self.latest_code()
        EmailMFACode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.client.post(reverse('mfa_verify'), {'code': code})
        self.assertEqual(self.client.get(reverse('team_list')).status_code, 302)

    def test_only_hash_is_stored(self):
        self.login(self.admin)
        self.client.get(reverse('mfa_verify'))
        code = self.latest_code()
        stored = EmailMFACode.objects.get()
        self.assertNotIn(code, stored.code_hash)

    def test_resend_is_throttled(self):
        self.login(self.admin)
        self.client.get(reverse('mfa_verify'))
        self.client.post(reverse('mfa_resend'))
        self.assertEqual(len(mail.outbox), 1)

    def test_new_login_needs_new_code(self):
        self.login(self.admin)
        self.client.get(reverse('mfa_verify'))
        self.client.post(reverse('mfa_verify'), {'code': self.latest_code()})
        self.assertEqual(self.client.get(reverse('team_list')).status_code, 200)
        self.client.logout()
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse('team_list')).status_code, 302)

    def test_api_session_request_blocked_until_verified(self):
        self.login(self.report_viewer)
        response = self.client.get('/api/v1/reports/')
        self.assertEqual(response.status_code, 403)

    def test_password_login_goes_to_mfa(self):
        response = self.client.post(reverse('login'), {
            'login': self.admin.email, 'password': 'pw-123456!',
        }, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse('mfa_verify'))
        self.assertEqual(len(mail.outbox), 1)

    def test_open_redirect_blocked(self):
        self.login(self.admin)
        self.client.get(reverse('mfa_verify'))
        response = self.client.post(reverse('mfa_verify'), {
            'code': self.latest_code(), 'next': 'https://evil.example/',
        })
        self.assertEqual(response['Location'], reverse('admin_dashboard'))

    @override_settings(MFA_REQUIRED=False)
    def test_can_be_switched_off(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse('team_list')).status_code, 200)


# ---------------------------------------------------------------------------
# Reviewer privacy notice
# ---------------------------------------------------------------------------

class ReviewerNoticeTests(OrgFixture):

    def setUp(self):
        super().setUp()
        self.open_cycle = ReviewCycle.objects.create(
            reviewee=self.cycle.reviewee, questionnaire=self.questionnaire,
            created_by=self.owner, status='active',
        )

    def test_email_invited_reviewer_told_hr_can_identify_them(self):
        token = ReviewerToken.objects.create(
            cycle=self.open_cycle, category='peer', reviewer_email='p2@kings.test',
        )
        response = self.client.get(reverse('reviews:feedback_form', args=[token.token]))
        self.assertContains(response, 'Who will see your feedback')
        self.assertContains(response, 'HR can link your answers to your email address')
        self.assertContains(response, 'at least 3 people')
        self.assertContains(response, 'hr@kings.test')

    def test_shared_link_reviewer_told_they_are_not_identified(self):
        token = ReviewerToken.objects.create(cycle=self.open_cycle, category='peer')
        response = self.client.get(reverse('reviews:feedback_form', args=[token.token]))
        self.assertContains(response, "aren't linked to your name or email address")
