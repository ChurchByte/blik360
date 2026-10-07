"""
Campuses: reviewees belong to zero or more campuses, and a Report Viewer either
covers all campuses ("Select All") or only the campuses ticked for them.
Reviewees with no campus are only visible to "all campuses" Report Viewers.
"""
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Campus, Reviewee, UserProfile
from accounts.permissions import (
    ORG_ADMIN, OWNER, REPORT_VIEWER,
    can_view_all_reports, can_view_reviewee_report, set_report_campus_scope,
    set_user_roles, visible_cycles, visible_reports,
)
from core.audit import Actions
from core.models import AuditLog, Organization
from questionnaires.models import Questionnaire
from reports.models import Report
from reports.services import generate_report
from reviews.models import ReviewCycle

User = get_user_model()


def fresh(user):
    return User.objects.get(pk=user.pk)


class CampusFixture(TestCase):

    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name='Kings', email='hr@kings.test')
        self.tweed = Campus.objects.create(organization=self.org, name='Tweed Heads')
        self.gold = Campus.objects.create(organization=self.org, name='Gold Coast')

        self.owner = self.make_user('owner', {OWNER})
        self.hr_all = self.make_user('hr-all', {REPORT_VIEWER})
        self.hr_tweed = self.make_user('hr-tweed', {REPORT_VIEWER}, campuses=[self.tweed])
        self.hr_none = self.make_user('hr-none', {REPORT_VIEWER}, campuses=[])
        self.member = self.make_user('member', set())

        self.questionnaire = Questionnaire.objects.create(organization=self.org, name='360')
        self.alice = self.make_reviewee('alice', [self.tweed])
        self.bob = self.make_reviewee('bob', [self.gold])
        self.carol = self.make_reviewee('carol', [self.tweed, self.gold])
        self.dave = self.make_reviewee('dave', [])
        self.cycles = {r.name: self.make_cycle(r) for r in (self.alice, self.bob, self.carol, self.dave)}

        # A second organization, to check campus ids can't cross over.
        self.other_org = Organization.objects.create(name='Other', email='x@other.test')
        self.other_campus = Campus.objects.create(organization=self.other_org, name='Elsewhere')

    def make_user(self, name, roles, campuses=None):
        user = User.objects.create_user(
            username=f'{name}@kings.test', email=f'{name}@kings.test', password='pw-123456!')
        profile = UserProfile.objects.create(user=user, organization=self.org)
        set_user_roles(user, roles)
        if campuses is not None:
            set_report_campus_scope(profile, False, [c.id for c in campuses])
        return fresh(user)

    def make_reviewee(self, name, campuses):
        reviewee = Reviewee.objects.create(
            organization=self.org, name=name, email=f'{name}@people.test')
        reviewee.campuses.set(campuses)
        return reviewee

    def make_cycle(self, reviewee):
        cycle = ReviewCycle.objects.create(
            reviewee=reviewee, questionnaire=self.questionnaire,
            created_by=self.owner, status='completed')
        generate_report(cycle)
        return cycle

    def report_names(self, user):
        qs = visible_reports(fresh(user), Report.objects.filter(cycle__reviewee__organization=self.org))
        return {r.cycle.reviewee.name for r in qs if r.cycle.reviewee.name in self.cycles}

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class CampusScopeTests(CampusFixture):

    def test_new_profiles_default_to_all_campuses(self):
        self.assertTrue(self.member.profile.report_all_campuses)

    def test_visible_reports_follow_campus_scope(self):
        self.assertEqual(self.report_names(self.hr_all), {'alice', 'bob', 'carol', 'dave'})
        self.assertEqual(self.report_names(self.hr_tweed), {'alice', 'carol'})
        self.assertEqual(self.report_names(self.hr_none), set())
        self.assertEqual(self.report_names(self.member), set())

    def test_no_duplicate_rows_for_multi_campus_reviewees(self):
        set_report_campus_scope(self.hr_tweed.profile, False, [self.tweed.id, self.gold.id])
        qs = visible_reports(fresh(self.hr_tweed), Report.objects.filter(cycle=self.cycles['carol']))
        self.assertEqual(qs.count(), 1)

    def test_unassigned_reviewees_need_all_campuses(self):
        set_report_campus_scope(self.hr_tweed.profile, False, [self.tweed.id, self.gold.id])
        user = fresh(self.hr_tweed)
        self.assertFalse(can_view_reviewee_report(user, self.dave))
        self.assertTrue(can_view_reviewee_report(fresh(self.hr_all), self.dave))

    def test_scoped_viewer_still_sees_own_report(self):
        own = Reviewee.objects.get(organization=self.org, email=self.hr_tweed.email)
        self.assertTrue(can_view_reviewee_report(fresh(self.hr_tweed), own))

    def test_can_view_all_reports_means_all_campuses(self):
        self.assertTrue(can_view_all_reports(fresh(self.hr_all)))
        self.assertFalse(can_view_all_reports(fresh(self.hr_tweed)))
        self.assertFalse(can_view_all_reports(fresh(self.owner)))

    def test_new_campus_is_covered_by_select_all(self):
        newcastle = Campus.objects.create(organization=self.org, name='Newcastle')
        erin = self.make_reviewee('erin', [newcastle])
        self.assertTrue(can_view_reviewee_report(fresh(self.hr_all), erin))
        self.assertFalse(can_view_reviewee_report(fresh(self.hr_tweed), erin))

    def test_visible_cycles_for_report_viewer_is_scoped_but_admins_see_all(self):
        base = ReviewCycle.objects.filter(reviewee__organization=self.org)
        names = {c.reviewee.name for c in visible_cycles(fresh(self.hr_tweed), base)}
        self.assertEqual(names & set(self.cycles), {'alice', 'carol'})
        names = {c.reviewee.name for c in visible_cycles(fresh(self.owner), base)}
        self.assertEqual(names & set(self.cycles), set(self.cycles))


class CampusReportViewTests(CampusFixture):

    def view(self, user, name):
        self.client.force_login(user)
        return self.client.get(reverse('reports:view_report', args=[self.cycles[name].uuid]))

    def test_report_page_access(self):
        expected = {
            'alice': 200, 'carol': 200, 'bob': 404, 'dave': 404,
        }
        for name, status in expected.items():
            with self.subTest(reviewee=name):
                self.assertEqual(self.view(self.hr_tweed, name).status_code, status)
        for name in expected:
            with self.subTest(all_campuses=name):
                self.assertEqual(self.view(self.hr_all, name).status_code, 200)

    def test_investigation_respects_campus(self):
        self.client.force_login(self.hr_tweed)
        url = reverse('reports:investigate_responses', args=[self.cycles['bob'].uuid])
        self.assertEqual(self.client.get(url).status_code, 404)
        url = reverse('reports:investigate_responses', args=[self.cycles['alice'].uuid])
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_cycle_detail_hides_report_link_outside_campus(self):
        set_user_roles(self.hr_tweed, {REPORT_VIEWER, ORG_ADMIN})
        set_report_campus_scope(self.hr_tweed.profile, False, [self.tweed.id])
        self.client.force_login(fresh(self.hr_tweed))
        response = self.client.get(reverse('review_cycle_detail', args=[self.cycles['bob'].uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['can_view_report'])
        response = self.client.get(reverse('review_cycle_detail', args=[self.cycles['alice'].uuid]))
        self.assertTrue(response.context['can_view_report'])

    def test_reviewee_list_report_links(self):
        self.client.force_login(self.hr_tweed)
        response = self.client.get(reverse('reviewee_list'))
        allowed = {i['reviewee'].name: i['can_view_report'] for i in response.context['reviewees_with_latest']}
        self.assertTrue(allowed['alice'])
        self.assertTrue(allowed['carol'])
        self.assertFalse(allowed['bob'])
        self.assertFalse(allowed['dave'])
        self.assertContains(response, 'Tweed Heads')

    def test_api_reports_are_scoped(self):
        client = APIClient()
        client.force_login(self.hr_tweed)
        response = client.get('/api/v1/reports/')
        self.assertEqual(response.status_code, 200)
        uuids = {r['uuid'] for r in response.data['results']}
        self.assertIn(str(self.cycles['alice'].report.uuid), uuids)
        self.assertNotIn(str(self.cycles['bob'].report.uuid), uuids)
        response = client.get(f"/api/v1/reports/{self.cycles['bob'].report.uuid}/")
        self.assertEqual(response.status_code, 404)

    def test_export_needs_all_campuses(self):
        set_user_roles(self.hr_tweed, {REPORT_VIEWER, ORG_ADMIN})
        self.client.force_login(fresh(self.hr_tweed))
        response = self.client.get(reverse('account:export_data'))
        self.assertEqual(response.status_code, 302)

        set_user_roles(self.hr_all, {REPORT_VIEWER, ORG_ADMIN})
        self.client.force_login(fresh(self.hr_all))
        response = self.client.get(reverse('account:export_data'))
        self.assertEqual(response.status_code, 200)
        import json
        data = json.loads(response.content)
        self.assertEqual({c['name'] for c in data['campuses']}, {'Tweed Heads', 'Gold Coast'})
        carol = next(r for r in data['reviewees'] if r['name'] == 'carol')
        self.assertEqual(carol['campuses'], ['Gold Coast', 'Tweed Heads'])


class CampusRoleAssignmentTests(CampusFixture):

    def post_roles(self, target, **extra):
        self.client.force_login(self.owner)
        data = {'user_profile_id': target.profile.id, 'roles': ['report_viewer']}
        data.update(extra)
        return self.client.post(reverse('update_user_permissions'), data)

    def test_limit_viewer_to_campuses(self):
        self.post_roles(self.hr_all, campus_scope_submitted='1',
                        report_campuses=[str(self.gold.id), str(self.other_campus.id)])
        profile = UserProfile.objects.get(user=self.hr_all)
        self.assertFalse(profile.report_all_campuses)
        self.assertEqual(set(profile.report_campuses.all()), {self.gold})  # other org ignored
        log = AuditLog.objects.filter(action=Actions.ROLES_CHANGED).latest('id')
        self.assertEqual(log.details['report_campuses']['to'],
                         {'all_campuses': False, 'campuses': ['Gold Coast']})

    def test_select_all(self):
        self.post_roles(self.hr_tweed, campus_scope_submitted='1', report_all_campuses='on',
                        report_campuses=[str(self.tweed.id)])
        profile = UserProfile.objects.get(user=self.hr_tweed)
        self.assertTrue(profile.report_all_campuses)
        self.assertEqual(profile.report_campuses.count(), 0)

    def test_roles_only_post_keeps_scope(self):
        response = self.post_roles(self.hr_tweed)
        profile = UserProfile.objects.get(user=self.hr_tweed)
        self.assertFalse(profile.report_all_campuses)
        self.assertEqual(set(profile.report_campuses.all()), {self.tweed})
        self.assertIn('No changes made.', self.messages(response))

    def test_team_page_lists_campuses(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('team_list'))
        self.assertContains(response, 'Select All')
        self.assertContains(response, 'Report Viewer &middot; Tweed Heads')
        self.assertContains(response, 'Report Viewer &middot; All campuses')


class CampusSettingsTests(CampusFixture):

    def test_admin_can_add_rename_delete(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('campus_create'), {'name': '  Newcastle  '})
        newcastle = Campus.objects.get(organization=self.org, name='Newcastle')

        response = self.client.post(reverse('campus_create'), {'name': 'newcastle'})
        self.assertEqual(Campus.objects.filter(organization=self.org, name__iexact='newcastle').count(), 1)
        self.assertTrue(any('already exists' in m for m in self.messages(response)))

        self.client.post(reverse('campus_rename', args=[newcastle.id]), {'name': 'Newcastle NSW'})
        newcastle.refresh_from_db()
        self.assertEqual(newcastle.name, 'Newcastle NSW')

        self.client.post(reverse('campus_delete', args=[self.gold.id]))
        self.assertFalse(Campus.objects.filter(id=self.gold.id).exists())
        self.assertEqual(list(self.carol.campuses.all()), [self.tweed])
        self.assertTrue(AuditLog.objects.filter(
            action=Actions.SETTINGS_CHANGED, details__section='campuses').exists())

    def test_members_cannot_manage_campuses(self):
        for user in (self.member, self.hr_all):
            with self.subTest(user=user.email):
                self.client.force_login(user)
                self.client.post(reverse('campus_create'), {'name': 'Sneaky'})
                self.client.post(reverse('campus_delete', args=[self.tweed.id]))
        self.assertFalse(Campus.objects.filter(name='Sneaky').exists())
        self.assertTrue(Campus.objects.filter(id=self.tweed.id).exists())

    def test_cannot_touch_another_orgs_campus(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('campus_delete', args=[self.other_campus.id]))
        self.assertEqual(response.status_code, 404)

    def test_settings_page_shows_campuses(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('settings'))
        self.assertContains(response, 'id="campuses"')
        self.assertContains(response, 'value="Tweed Heads"')


class RevieweeCampusFormTests(CampusFixture):

    def test_create_and_edit_reviewee_campuses(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('reviewee_create'), {
            'name': 'Frank', 'email': 'frank@people.test',
            'campuses': [str(self.tweed.id), str(self.other_campus.id)],
        })
        frank = Reviewee.objects.get(organization=self.org, email='frank@people.test')
        self.assertEqual(list(frank.campuses.all()), [self.tweed])

        response = self.client.get(reverse('reviewee_edit', args=[frank.id]))
        self.assertContains(response, 'Gold Coast')

        self.client.post(reverse('reviewee_edit', args=[frank.id]), {
            'name': 'Frank', 'email': 'frank@people.test', 'campuses': [str(self.gold.id)],
        })
        self.assertEqual(list(frank.campuses.all()), [self.gold])

        self.client.post(reverse('reviewee_edit', args=[frank.id]), {
            'name': 'Frank', 'email': 'frank@people.test',
        })
        self.assertEqual(frank.campuses.count(), 0)

    def test_cannot_edit_another_orgs_reviewee(self):
        stranger = Reviewee.objects.create(organization=self.other_org, name='S', email='s@other.test')
        self.client.force_login(self.owner)
        response = self.client.get(reverse('reviewee_edit', args=[stranger.id]))
        self.assertEqual(response.status_code, 404)
