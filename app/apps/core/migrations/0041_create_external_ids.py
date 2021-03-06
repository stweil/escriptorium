# Generated by Django 2.1.4 on 2020-09-17 13:02
import uuid

from django.db import migrations


def batch_qs(qs, batch_size=1000):
    """
    Returns a (start, end, total, queryset) tuple for each batch in the given
    queryset.

    Usage:
        # Make sure to order your querset
        article_qs = Article.objects.order_by('id')
        for start, end, total, qs in batch_qs(article_qs):
            print "Now processing %s - %s of %s" % (start + 1, end, total)
            for article in qs:
                print article.body
    """
    total = qs.count()
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        yield (start, end, total, qs[start:end])

def forward(apps, se):
    Block = apps.get_model('core', 'Block')
    Line = apps.get_model('core', 'Line')

    for s,e,t, blocks in batch_qs(Block.objects.filter(external_id=None)):
        for block in blocks:
            block.external_id = 'eSc_textblock_%s' % str(uuid.uuid4())[:8]
            block.save()

    for s,e,t, lines in batch_qs(Line.objects.filter(external_id=None)):
        for line in lines:
            line.external_id = 'eSc_line_%s' % str(uuid.uuid4())[:8]
            line.save()


def backward(apps, se):
    # no need to do anything
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0040_link_default_typology'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
