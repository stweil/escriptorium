# Generated by Django 2.1.4 on 2020-08-28 08:23

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0009_contactus'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='contactus',
            options={'verbose_name': 'message', 'verbose_name_plural': 'messages'},
        ),
        migrations.RemoveField(
            model_name='contactus',
            name='subject',
        ),
    ]