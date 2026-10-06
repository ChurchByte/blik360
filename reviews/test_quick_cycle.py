from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse

from core.factories import OrganizationFactory, UserFactory
from accounts.factories import UserProfileFactory, RevieweeFactory
from questionnaires.factories import QuestionnaireFactory
from reviews.models import ReviewCycle

NOTIFY_PATH = 'reviews.services.send_reviewee_notifications'


class QuickCycleCreateNotificationTestCase(TestCase):
    """Quick-cycle creation must email the reviewee their links."""

    def setUp(self):
        self.org = OrganizationFactory()
        self.user = UserFactory(username='quickadmin', is_superuser=True, is_staff=True)
        UserProfileFactory(
            user=self.user,
            organization=self.org,
            can_create_cycles_for_others=True,
        )
        self.questionnaire = QuestionnaireFactory(organization=self.org, is_default=True)
        self.reviewee = RevieweeFactory(organization=self.org, name='Quick Reviewee')
        self.client = Client()
        self.client.force_login(self.user)

    def _quick_create(self):
        return self.client.post(
            reverse('quick_cycle_create', args=[self.reviewee.id]),
            {'questionnaire_id': str(self.questionnaire.id)},
        )

    def test_first_quick_cycle_notifies_reviewee(self):
        with patch(NOTIFY_PATH) as notify:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._quick_create()

        cycle = ReviewCycle.objects.get(reviewee=self.reviewee)
        self.assertRedirects(
            response, reverse('review_cycle_detail', args=[cycle.uuid]),
            fetch_redirect_response=False,
        )
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], cycle)

    def test_repeat_quick_cycle_notifies_reviewee(self):
        with patch(NOTIFY_PATH):
            with self.captureOnCommitCallbacks(execute=True):
                self._quick_create()

        with patch(NOTIFY_PATH) as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self._quick_create()

        latest = ReviewCycle.objects.filter(reviewee=self.reviewee).order_by('-created_at').first()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], latest)
