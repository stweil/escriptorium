# Generated by Django 2.1.4 on 2019-04-10 13:18

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_auto_20190317_1550'),
    ]

    operations = [
        migrations.AddField(
            model_name='documentpart',
            name='source',
            field=models.CharField(blank=True, max_length=1024),
        ),
    ]