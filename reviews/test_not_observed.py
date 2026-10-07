from django.test import TestCase, Client
from django.urls import reverse

from core.factories import OrganizationFactory, UserFactory
from accounts.factories import RevieweeFactory, UserProfileFactory
from questionnaires.factories import (
    QuestionnaireFactory, QuestionSectionFactory, RatingQuestionFactory,
)
from reviews.factories import ReviewCycleFactory, ReviewerTokenFactory
from reviews.models import Response
from reports.services import generate_report


class UnableToObserveTestCase(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        # Setup-wizard middleware needs a user with a profile to exist
        UserProfileFactory(user=UserFactory(), organization=self.org)
        reviewee = RevieweeFactory(organization=self.org)
        questionnaire = QuestionnaireFactory(organization=self.org)
        section = QuestionSectionFactory(questionnaire=questionnaire, order=0)
        self.q1 = RatingQuestionFactory(section=section, order=0, required=True)
        self.q2 = RatingQuestionFactory(section=section, order=1, required=True)
        self.cycle = ReviewCycleFactory(reviewee=reviewee, questionnaire=questionnaire)
        self.client = Client()

    def _submit(self, token, data):
        return self.client.post(reverse('reviews:submit_feedback', args=[token.token]), data)

    def test_not_observed_bypasses_required_and_is_stored_as_marker(self):
        token = ReviewerTokenFactory(cycle=self.cycle, category='peer')
        resp = self._submit(token, {
            f'question_{self.q1.id}': '4',
            f'not_observed_{self.q2.id}': '1',
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        r = Response.objects.get(token=token, question=self.q2)
        self.assertEqual(r.answer_data, {'value': None, 'not_observed': True})

    def test_self_assessment_cannot_use_not_observed(self):
        token = ReviewerTokenFactory(cycle=self.cycle, category='self')
        resp = self._submit(token, {
            f'question_{self.q1.id}': '4',
            f'not_observed_{self.q2.id}': '1',
        })
        self.assertEqual(resp.status_code, 400)

    def test_form_shows_option_for_peers_only(self):
        peer = ReviewerTokenFactory(cycle=self.cycle, category='peer')
        me = ReviewerTokenFactory(cycle=self.cycle, category='self')
        html = self.client.get(reverse('reviews:feedback_form', args=[peer.token])).content.decode()
        self.assertIn(f'name="not_observed_{self.q1.id}"', html)
        html = self.client.get(reverse('reviews:feedback_form', args=[me.token])).content.decode()
        self.assertNotIn('not_observed_', html)

    def test_report_excludes_not_observed(self):
        t1 = ReviewerTokenFactory(cycle=self.cycle, category='peer')
        t2 = ReviewerTokenFactory(cycle=self.cycle, category='peer')
        self._submit(t1, {f'question_{self.q1.id}': '4', f'question_{self.q2.id}': '2'})
        self._submit(t2, {f'question_{self.q1.id}': '2', f'not_observed_{self.q2.id}': '1'})

        report = generate_report(self.cycle)
        questions = {}
        for section in report.report_data['by_section'].values():
            questions.update(section['questions'])

        q1 = questions[str(self.q1.id)]
        q2 = questions[str(self.q2.id)]
        self.assertEqual(q1['by_category']['peer']['count'], 2)
        self.assertEqual(q1['by_category']['peer']['avg'], 3.0)
        self.assertEqual(q1['not_observed_count'], 0)
        self.assertEqual(q2['by_category']['peer']['count'], 1)
        self.assertEqual(q2['by_category']['peer']['avg'], 2.0)
        self.assertEqual(q2['not_observed_count'], 1)
