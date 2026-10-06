from django.test import TestCase, Client
from django.urls import reverse

from core.factories import OrganizationFactory, UserFactory
from accounts.factories import UserProfileFactory, RevieweeFactory
from questionnaires.factories import QuestionnaireFactory
from reviews.factories import ReviewCycleFactory, ReviewerTokenFactory
from accounts.permissions import assign_organization_admin


class ArchiveCycleTestCase(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.user = UserFactory(username='archiver')
        UserProfileFactory(user=self.user, organization=self.org, can_create_cycles_for_others=True)
        assign_organization_admin(self.user)
        self.reviewee = RevieweeFactory(organization=self.org, name='Pat Reviewee')
        self.questionnaire = QuestionnaireFactory(organization=self.org)
        self.cycle = ReviewCycleFactory(reviewee=self.reviewee, questionnaire=self.questionnaire)
        # A token nobody has completed — the case "Close Cycle" refuses
        self.token = ReviewerTokenFactory(cycle=self.cycle)
        self.client = Client()
        self.client.force_login(self.user)

    def test_archive_incomplete_cycle(self):
        response = self.client.post(reverse('archive_cycle', args=[self.cycle.uuid]))
        self.assertRedirects(response, reverse('review_cycle_detail', args=[self.cycle.uuid]))
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, 'archived')
        self.assertEqual(self.cycle.status_before_archive, 'active')
        self.assertIsNotNone(self.cycle.archived_at)
        # Nothing is deleted
        self.assertTrue(self.cycle.tokens.filter(pk=self.token.pk).exists())

    def test_archive_requires_post(self):
        response = self.client.get(reverse('archive_cycle', args=[self.cycle.uuid]))
        self.assertEqual(response.status_code, 405)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, 'active')

    def test_cannot_archive_other_orgs_cycle(self):
        other_cycle = ReviewCycleFactory(
            reviewee=RevieweeFactory(organization=OrganizationFactory()),
        )
        response = self.client.post(reverse('archive_cycle', args=[other_cycle.uuid]))
        self.assertEqual(response.status_code, 404)
        other_cycle.refresh_from_db()
        self.assertEqual(other_cycle.status, 'active')

    def test_unarchive_restores_previous_status(self):
        self.cycle.archive()
        self.client.post(reverse('unarchive_cycle', args=[self.cycle.uuid]))
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, 'active')
        self.assertIsNone(self.cycle.archived_at)

        self.cycle.status = 'completed'
        self.cycle.save()
        self.cycle.archive()
        self.assertTrue(self.cycle.was_completed)
        self.cycle.unarchive()
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, 'completed')

    def test_archived_cycle_blocks_feedback(self):
        self.cycle.archive()
        response = self.client.get(reverse('reviews:feedback_form', args=[self.token.token]))
        self.assertEqual(response.status_code, 410)
        response = self.client.post(reverse('reviews:submit_feedback', args=[self.token.token]))
        self.assertEqual(response.status_code, 410)

    def test_list_hides_archived_by_default(self):
        self.cycle.archive()
        response = self.client.get(reverse('review_cycle_list'))
        listed = [item['cycle'].pk for item in response.context['cycles_with_latest']]
        self.assertNotIn(self.cycle.pk, listed)
        self.assertEqual(response.context['archived_count'], 1)

        response = self.client.get(reverse('review_cycle_list') + '?archived=1')
        listed = [item['cycle'].pk for item in response.context['cycles_with_latest']]
        self.assertIn(self.cycle.pk, listed)

    def test_non_admin_cannot_archive(self):
        member = UserFactory(username='member', email=self.reviewee.email)
        UserProfileFactory(user=member, organization=self.org)
        client = Client()
        client.force_login(member)
        client.post(reverse('archive_cycle', args=[self.cycle.uuid]))
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, 'active')

    def test_detail_page_renders_for_archived(self):
        self.cycle.archive()
        response = self.client.get(reverse('review_cycle_detail', args=[self.cycle.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Restore Cycle')
