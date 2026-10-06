from django.test import TestCase

from questionnaires.models import Questionnaire, QuestionSection, Question
from reports.services import _calculate_insights


class DreyfusToggleTests(TestCase):
    def setUp(self):
        self.questionnaire = Questionnaire.objects.create(name="Toggle Test")
        section = QuestionSection.objects.create(
            questionnaire=self.questionnaire, title="S", order=1
        )
        Question.objects.create(
            section=section,
            question_text="Q",
            question_type="rating",
            config={"min": 1, "max": 5, "dreyfus_mapping": {"skill": 1.0, "agency": 1.0}},
            order=1,
        )

    def test_enabled_by_default(self):
        self.assertTrue(self.questionnaire.dreyfus_enabled)
        self.assertEqual(self.questionnaire.report_type_label, "Dreyfus (Skill + Agency)")

    def test_disabled_reports_standard_360(self):
        self.questionnaire.dreyfus_enabled = False
        self.questionnaire.save()
        self.assertEqual(self.questionnaire.dreyfus_dimensions, (False, False))
        self.assertEqual(self.questionnaire.report_type_label, "Standard 360")

    def test_insights_skip_dreyfus_when_disabled(self):
        insights, _ = _calculate_insights({
            'by_section': {},
            'questionnaire_id': self.questionnaire.id,
            'dreyfus_enabled': False,
        })
        self.assertIsNone(insights['skill_profile'])
        self.assertIsNone(insights['agency_profile'])
        self.assertIsNone(insights['dreyfus_quadrant'])
        self.assertIsNone(insights['development_plan'])
