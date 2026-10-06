from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0007_reviewcycle_close_check_sent_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='reviewcycle',
            name='archived_at',
            field=models.DateTimeField(blank=True, help_text='When the cycle was archived', null=True),
        ),
        migrations.AddField(
            model_name='reviewcycle',
            name='status_before_archive',
            field=models.CharField(blank=True, default='', help_text='Status the cycle had before it was archived (restored on unarchive)', max_length=20),
        ),
        migrations.AlterField(
            model_name='reviewcycle',
            name='status',
            field=models.CharField(choices=[('active', 'Active'), ('completed', 'Completed'), ('archived', 'Archived')], default='active', max_length=20),
        ),
    ]
