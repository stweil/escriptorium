# Generated by Django 2.1.4 on 2018-12-05 09:36

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_auto_20181205_0932'),
    ]

    operations = [
        migrations.AlterField(
            model_name='metadata',
            name='cidoc_id',
            field=models.CharField(blank=True, max_length=8, null=True),
        ),
    ]
