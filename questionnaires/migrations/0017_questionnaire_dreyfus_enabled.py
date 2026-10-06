from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('questionnaires', '0016_fix_agency_questionnaire_template_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='questionnaire',
            name='dreyfus_enabled',
            field=models.BooleanField(
                default=True,
                help_text='Show Dreyfus (Skill + Agency) configuration in the builder and Dreyfus profile sections in generated reports',
            ),
        ),
    ]
