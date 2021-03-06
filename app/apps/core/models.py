import re
import logging
import math
import os
import json
import functools
import subprocess
import uuid
from PIL import Image
from datetime import datetime
from shapely.geometry import Polygon, LineString

from django.db import models, transaction
from django.db.models import Q, Prefetch
from django.db.models.signals import pre_delete
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.postgres.fields import JSONField
from django.core.validators import FileExtensionValidator
from django.dispatch import receiver
from django.forms import ValidationError
from django.utils.functional import cached_property
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from celery.task.control import inspect, revoke
from celery import chain
from django_redis import get_redis_connection
from easy_thumbnails.files import get_thumbnailer
from ordered_model.models import OrderedModel
from kraken import blla, rpred
from kraken.binarization import nlbin
from kraken.lib import vgsl, models as kraken_models
from kraken.lib.util import is_bitonal
from kraken.lib.segmentation import calculate_polygonal_environment

from versioning.models import Versioned
from core.tasks import (segtrain, train, binarize,
                        lossless_compression, convert, segment, transcribe,
                        generate_part_thumbnails)
from users.consumers import send_event

redis_ = get_redis_connection()
User = get_user_model()
logger = logging.getLogger(__name__)


class ProcessFailureException(Exception):
    pass


class AlreadyProcessingException(Exception):
    pass


class Typology(models.Model):
    name = models.CharField(max_length=128)

    # if True, is visible as a choice in the ontology edition in a new document
    public = models.BooleanField(default=False)
    # if True, is a valid choice by default in a new document
    default = models.BooleanField(default=False)

    class Meta:
        abstract = True

    def __str__(self):
        return self.name


class DocumentType(Typology):
    pass


class DocumentPartType(Typology):
    pass


class BlockType(Typology):
    pass


class LineType(Typology):
    pass


class Metadata(models.Model):
    name = models.CharField(max_length=128, unique=True)
    cidoc_id = models.CharField(max_length=8, null=True, blank=True)
    public = models.BooleanField(default=False)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return self.name


class Script(models.Model):
    TEXT_DIRECTION_HORIZONTAL_LTR = 'horizontal-lr'
    TEXT_DIRECTION_HORIZONTAL_RTL = 'horizontal-rl'
    TEXT_DIRECTION_VERTICAL_LTR = 'vertical-lr'
    TEXT_DIRECTION_VERTICAL_RTL = 'vertical-rl'
    TEXT_DIRECTION_TTB = 'ttb'
    TEXT_DIRECTION_CHOICES = (
        ((TEXT_DIRECTION_HORIZONTAL_LTR, _("Horizontal l2r")),
         (TEXT_DIRECTION_HORIZONTAL_RTL, _("Horizontal r2l")),
         (TEXT_DIRECTION_VERTICAL_LTR, _("Vertical l2r")),
         (TEXT_DIRECTION_VERTICAL_RTL, _("Vertical r2l")),
         (TEXT_DIRECTION_TTB, _("Top to bottom")))
    )

    name = models.CharField(max_length=128)
    name_fr = models.CharField(max_length=128, blank=True)
    iso_code = models.CharField(max_length=4, blank=True)
    text_direction = models.CharField(max_length=64, default='horizontal-lr',
                                      choices=TEXT_DIRECTION_CHOICES)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return self.name


class DocumentMetadata(models.Model):
    document = models.ForeignKey('core.Document', on_delete=models.CASCADE)
    key = models.ForeignKey(Metadata, on_delete=models.CASCADE)
    value = models.CharField(max_length=512)

    def __str__(self):
        return '%s:%s' % (self.document.name, self.key.name)


class DocumentManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().select_related('typology')

    def for_user(self, user):
        # return the list of editable documents
        # Note: Monitor this query
        return (Document.objects
                .filter(Q(owner=user)
                        | (Q(workflow_state__gt=Document.WORKFLOW_STATE_DRAFT)
                           & (Q(shared_with_users=user)
                              | Q(shared_with_groups__in=user.groups.all()))))
                .exclude(workflow_state=Document.WORKFLOW_STATE_ARCHIVED)
                .prefetch_related('shared_with_groups', 'transcriptions')
                .select_related('typology')
                .distinct())


class Document(models.Model):
    WORKFLOW_STATE_DRAFT = 0
    WORKFLOW_STATE_SHARED = 1  # editable a viewable by shared_with people
    WORKFLOW_STATE_PUBLISHED = 2  # viewable by the world
    WORKFLOW_STATE_ARCHIVED = 3  #
    WORKFLOW_STATE_CHOICES = (
        (WORKFLOW_STATE_DRAFT, _("Draft")),
        (WORKFLOW_STATE_SHARED, _("Shared")),
        (WORKFLOW_STATE_PUBLISHED, _("Published")),
        (WORKFLOW_STATE_ARCHIVED, _("Archived")),
    )
    READ_DIRECTION_LTR = 'ltr'
    READ_DIRECTION_RTL = 'rtl'
    READ_DIRECTION_CHOICES = (
        (READ_DIRECTION_LTR, _("Left to right")),
        (READ_DIRECTION_RTL, _("Right to left")),
    )

    name = models.CharField(max_length=512)

    owner = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    workflow_state = models.PositiveSmallIntegerField(
        default=WORKFLOW_STATE_DRAFT,
        choices=WORKFLOW_STATE_CHOICES)
    shared_with_users = models.ManyToManyField(User, blank=True,
                                               verbose_name=_("Share with users"),
                                               related_name='shared_documents')
    shared_with_groups = models.ManyToManyField(Group, blank=True,
                                                verbose_name=_("Share with teams"),
                                                related_name='shared_documents')

    main_script = models.ForeignKey(Script, null=True, blank=True, on_delete=models.SET_NULL)
    read_direction = models.CharField(
        max_length=3,
        choices=READ_DIRECTION_CHOICES,
        default=READ_DIRECTION_LTR
    )
    typology = models.ForeignKey(DocumentType, null=True, blank=True, on_delete=models.SET_NULL)

    # A list of Typology(ies) which are valid to this document. Part of the document's ontology.
    valid_block_types = models.ManyToManyField(BlockType, blank=True, related_name='valid_in')
    valid_line_types = models.ManyToManyField(LineType, blank=True, related_name='valid_in')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    metadatas = models.ManyToManyField(Metadata, through=DocumentMetadata, blank=True)

    objects = DocumentManager()

    class Meta:
        ordering = ('-updated_at',)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        res = super().save(*args, **kwargs)
        Transcription.objects.get_or_create(document=self, name=_(Transcription.DEFAULT_NAME))
        return res

    @property
    def is_shared(self):
        return self.workflow_state in [self.WORKFLOW_STATE_PUBLISHED,
                                       self.WORKFLOW_STATE_SHARED]

    @property
    def is_published(self):
        return self.workflow_state == self.WORKFLOW_STATE_PUBLISHED

    @property
    def is_archived(self):
        return self.workflow_state == self.WORKFLOW_STATE_ARCHIVED

    @cached_property
    def is_transcribing(self):
        return (self.parts.filter(workflow_state__gte=DocumentPart.WORKFLOW_STATE_TRANSCRIBING)
                .first() is not None)

    @cached_property
    def default_text_direction(self):
        if self.main_script:
            if self.main_script.text_direction == Script.TEXT_DIRECTION_HORIZONTAL_RTL:
                return 'rtl'
            else:
                return 'ltr'
        else:
            if self.read_direction == self.READ_DIRECTION_RTL:
                return 'rtl'
            else:
                return 'ltr'

    @property
    def training_model(self):
        return self.ocr_models.filter(training=True).first()


def document_images_path(instance, filename):
    return 'documents/{0}/{1}'.format(instance.document.pk, filename)


class DocumentPart(OrderedModel):
    """
    Represents a physical part of a larger document that is usually a page
    """
    name = models.CharField(max_length=512, blank=True)
    image = models.ImageField(upload_to=document_images_path)
    original_filename = models.CharField(max_length=1024, blank=True)
    source = models.CharField(max_length=1024, blank=True)
    bw_backend = models.CharField(max_length=128, default='kraken')
    bw_image = models.ImageField(upload_to=document_images_path,
                                 null=True, blank=True,
                                 help_text=_("Binarized image needs to be the same size as original image."))
    typology = models.ForeignKey(DocumentPartType, null=True, blank=True,
                                 on_delete=models.SET_NULL)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='parts')
    order_with_respect_to = 'document'

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    WORKFLOW_STATE_CREATED = 0
    WORKFLOW_STATE_CONVERTING = 1
    WORKFLOW_STATE_CONVERTED = 2
    # WORKFLOW_STATE_BINARIZING = 3  # legacy
    # WORKFLOW_STATE_BINARIZED = 4
    WORKFLOW_STATE_SEGMENTING = 5
    WORKFLOW_STATE_SEGMENTED = 6
    WORKFLOW_STATE_TRANSCRIBING = 7
    WORKFLOW_STATE_CHOICES = (
        (WORKFLOW_STATE_CREATED, _("Created")),
        (WORKFLOW_STATE_CONVERTING, _("Converting")),
        (WORKFLOW_STATE_CONVERTED, _("Converted")),
        (WORKFLOW_STATE_SEGMENTING, _("Segmenting")),
        (WORKFLOW_STATE_SEGMENTED, _("Segmented")),
        (WORKFLOW_STATE_TRANSCRIBING, _("Transcribing")),
    )
    workflow_state = models.PositiveSmallIntegerField(
        choices=WORKFLOW_STATE_CHOICES,
        default=WORKFLOW_STATE_CREATED)

    # this is denormalized because it's too heavy to calculate on the fly
    transcription_progress = models.PositiveSmallIntegerField(default=0)

    class Meta(OrderedModel.Meta):
        pass

    def __str__(self):
        if self.name:
            return self.name
        return '%s %d' % (self.typology or _("Element"), self.order + 1)

    @property
    def title(self):
        return str(self)

    @property
    def converted(self):
        return self.workflow_state >= self.WORKFLOW_STATE_CONVERTED

    @property
    def binarized(self):
        try:
            self.bw_image.file
        except ValueError:
            # catches ValueError: The 'bw_image' attribute has no file associated with it.
            return False
        else:
            return True

    @property
    def segmented(self):
        return self.lines.count() > 0

    @property
    def has_masks(self):
        try:
            # Note: imposing a limit and catching IndexError is faster than counting
            self.lines.exclude(mask=None)[0]
            return True
        except IndexError:
            return False

    @property
    def filename(self):
        return self.original_filename or os.path.split(self.image.path)[1]

    def calculate_progress(self):
        total = self.lines.count()
        if not total:
            return 0
        transcribed = LineTranscription.objects.filter(line__document_part=self).count()
        self.transcription_progress = min(int(transcribed / total * 100), 100)

    def recalculate_ordering(self, read_direction=None):
        """
        Re-order the lines of the DocumentPart depending on read direction.
        """
        read_direction = read_direction or self.document.read_direction
        # imgbox = ((0, 0), (self.image.width, self.image.height))
        if read_direction == Document.READ_DIRECTION_RTL:
            origin_box = [self.image.width, 0]
        else:
            origin_box = [0, 0]

        def distance(x, y):
            return math.sqrt(sum([(a - b) ** 2 for a, b in zip(x, y)]))

        def poly_origin_pt(shape):
            return min(shape, key=lambda pt: distance(pt, origin_box))

        def line_origin_pt(line):
            if read_direction == Document.READ_DIRECTION_RTL:
                return line[-1]
            else:
                return line[0]

        # fetch all lines and regroup them by block
        qs = self.lines.select_related('block').all()
        ls = list(qs)
        if len(ls) == 0:
            return
        ords = list(map(lambda l: (line_origin_pt(l.baseline) if l.baseline
                                   else poly_origin_pt(l.mask))[0], ls))
        averageLineHeight = (max(ords) - min(ords)) / len(ords)

        def cmp_lines(a, b):
            # cache origin pts for efficiency
            if not hasattr(a, 'origin_pt'):
                a.origin_pt = line_origin_pt(a.baseline) if a.baseline else poly_origin_pt(a.mask)
            if not hasattr(b, 'origin_pt'):
                b.origin_pt = line_origin_pt(b.baseline) if b.baseline else poly_origin_pt(b.mask)

            try:
                if a.block != b.block:
                    pt1 = poly_origin_pt(a.block.box) if a.block else a.origin_pt
                    pt2 = poly_origin_pt(b.block.box) if b.block else b.origin_pt

                    # when comparing blocks we can use the distance
                    return distance(pt1, origin_box) - distance(pt2, origin_box)
                else:
                    pt1 = a.origin_pt
                    pt2 = b.origin_pt

                    # # 2 lines more or less on the same level
                    if abs(pt1[1] - pt2[1]) < averageLineHeight:
                        return distance(pt1, origin_box) - distance(pt2, origin_box)
                return pt1[1] - pt2[1]
            except TypeError as e:  # invalid line
                return 0

        # sort depending on the distance to the origin
        ls.sort(key=functools.cmp_to_key(lambda a, b: cmp_lines(a, b)))
        # one query / line, super gory
        for order, line in enumerate(ls):
            if line.order != order:
                line.order = order
                line.save()

    def save(self, *args, **kwargs):
        new = self.pk is None
        instance = super().save(*args, **kwargs)
        if new:
            self.task('convert')
            send_event('document', self.document.pk, "part:new", {"id": self.pk})
        else:
            self.calculate_progress()
        return instance

    def delete(self, *args, **kwargs):
        redis_.delete('process-%d' % self.pk)
        send_event('document', self.document.pk, "part:delete", {
            "id": self.pk
        })
        return super().delete(*args, **kwargs)

    @cached_property
    def tasks(self):
        try:
            return json.loads(redis_.get('process-%d' % self.pk) or '{}')
        except json.JSONDecodeError:
            return {}

    @property
    def workflow(self):
        w = {}
        if self.workflow_state == self.WORKFLOW_STATE_CONVERTING:
            w['convert'] = 'ongoing'
        elif self.workflow_state > self.WORKFLOW_STATE_CONVERTING:
            w['convert'] = 'done'
        if self.workflow_state == self.WORKFLOW_STATE_SEGMENTING:
            w['segment'] = 'ongoing'
        elif self.workflow_state > self.WORKFLOW_STATE_SEGMENTING:
            w['segment'] = 'done'
        if self.workflow_state == self.WORKFLOW_STATE_TRANSCRIBING:
            w['transcribe'] = 'done'

        # check on redis for reruns
        for task_name in ['core.tasks.binarize', 'core.tasks.segment', 'core.tasks.transcribe']:
            if task_name in self.tasks:
                if self.tasks[task_name]['status'] == 'pending':
                    w[task_name.split('.')[-1]] = 'pending'
                elif self.tasks[task_name]['status'] in ['before_task_publish', 'task_prerun']:
                    w[task_name.split('.')[-1]] = 'ongoing'
                elif self.tasks[task_name]['status'] == 'canceled':
                    w[task_name.split('.')[-1]] = 'canceled'
                elif self.tasks[task_name]['status'] in ['task_failure', 'error']:
                    w[task_name.split('.')[-1]] = 'error'
        return w

    def tasks_finished(self):
        try:
            return len([t for t in self.workflow if t['status'] != 'done']) == 0
        except (KeyError, TypeError):
            return True

    def in_queue(self):
        statuses = self.tasks.values()
        try:
            return (len([t for t in statuses if t['status'] == 'ongoing']) == 0 and
                    len([t for t in statuses if t['status']
                         in ['pending', 'before_task_publish']]) > 0)
        except (KeyError, TypeError):
            return False

    def cancel_tasks(self):
        uncancelable = ['core.tasks.convert',
                        'core.tasks.lossless_compression',
                        'core.tasks.generate_part_thumbnails']
        if self.workflow_state == self.WORKFLOW_STATE_SEGMENTING:
            self.workflow_state = self.WORKFLOW_STATE_CONVERTED
            self.save()

        for task_name, task in self.tasks.items():
            if task_name not in uncancelable:
                if 'task_id' in task:  # if not, it is still pending
                    revoke(task['task_id'], terminate=True)
                redis_.set('process-%d' % self.pk, json.dumps({task_name: {"status": "canceled"}}))

    def recoverable(self):
        now = round(datetime.utcnow().timestamp())
        try:
            return len([task for task in self.tasks
                        if getattr(task, 'timestamp', 0) +
                        getattr(settings, 'TASK_RECOVER_DELAY', 60*60*24) > now]) != 0
        except KeyError:
            return True  # probably old school stored task

    def recover(self):
        i = inspect()
        # Important: this is really slow!
        queued = ([task['id'] for queue in i.scheduled().values() for task in queue] +
                  [task['id'] for queue in i.active().values() for task in queue] +
                  [task['id'] for queue in i.reserved().values() for task in queue])

        data = self.tasks

        for task_name in [task_name for task_name in data.keys()
                          if task_name not in queued]:
            # redis seems desync, but it could really be pending!
            # but if it is it doesn't really matter since state will be updated when the worker pick it up.
            del data[task_name]

        tasks_map = {  # map a task to a workflow state it should go back to if failed
            'core.tasks.convert': (self.WORKFLOW_STATE_CONVERTING,
                                   self.WORKFLOW_STATE_CREATED),
            'core.tasks.segment': (self.WORKFLOW_STATE_SEGMENTING,
                                   self.WORKFLOW_STATE_CONVERTED),
            'core.tasks.transcribe': (self.WORKFLOW_STATE_TRANSCRIBING,
                                      self.WORKFLOW_STATE_SEGMENTED),
        }
        for task_name in tasks_map:
            if self.workflow_state == tasks_map[task_name][0] and task_name not in data:
                data[task_name] = {"status": "error"}
                self.workflow_state = tasks_map[task_name][1]

        redis_.set('process-%d' % self.pk, json.dumps(data))
        self.save()

    def convert(self):
        if not getattr(settings, 'ALWAYS_CONVERT', False):
            return

        if self.workflow_state < self.WORKFLOW_STATE_CONVERTING:
            self.workflow_state = self.WORKFLOW_STATE_CONVERTING
            self.save()

        old_name = self.image.file.name
        filename, extension = os.path.splitext(old_name)
        if extension != ".png":
            new_name = filename + ".png"
            error = subprocess.check_call(["convert", old_name, new_name])
            if error:
                raise RuntimeError("Error trying to convert file(%s) to png.")

            self.image = new_name.split(settings.MEDIA_ROOT)[1][1:]
            os.remove(old_name)

        if self.workflow_state < self.WORKFLOW_STATE_CONVERTED:
            self.workflow_state = self.WORKFLOW_STATE_CONVERTED

        with Image.open(self.image.path) as im:
            if is_bitonal(im):
                self.bw_image = self.image

        self.save()

    def compress(self):
        if not getattr(settings, 'COMPRESS_ENABLE', True):
            return
        filename, extension = os.path.splitext(self.image.file.name)
        if extension != 'png':
            return
        opti_name = filename + '_opti.png'
        try:
            subprocess.check_call(["pngcrush", "-q", self.image.file.name, opti_name])
        except Exception as e:
            # Note: let it fail it's fine
            logger.exception("png optimization failed for %s." % filename)
            if settings.DEBUG:
                raise e
        else:
            os.rename(opti_name, self.image.file.name)

    def binarize(self, threshold=None):
        fname = os.path.basename(self.image.file.name)
        # should be formated to png already by by lossless_compression but better safe than sorry
        form = None
        f_, ext = os.path.splitext(self.image.file.name)
        if ext[1] in ['.jpg', '.jpeg', '.JPG', '.JPEG', '']:
            if ext:
                logger.warning('jpeg does not support 1bpp images. Forcing to png.')
            form = 'png'
            fname = '%s.%s' % (f_, form)
        bw_file_name = 'bw_' + fname
        bw_file = os.path.join(os.path.dirname(self.image.file.name), bw_file_name)
        with Image.open(self.image.path) as im:
            # threshold, zoom, escale, border, perc, range, low, high
            if threshold is not None:
                res = nlbin(im, threshold)
            else:
                res = nlbin(im)
            res.save(bw_file, format=form)

        self.bw_image = document_images_path(self, bw_file_name)
        self.save()

    def segment(self, steps=None, text_direction=None, read_direction=None,
                model=None, override=False):
        """
        steps: lines regions masks
        """
        self.workflow_state = self.WORKFLOW_STATE_SEGMENTING
        self.save()

        if model:
            model_path = model.file.path
        else:
            model_path = settings.KRAKEN_DEFAULT_SEGMENTATION_MODEL
        model_ = vgsl.TorchVGSLModel.load_model(model_path)
        # TODO: check model_type [None, 'recognition', 'segmentation']
        #    &  seg_type [None, 'bbox', 'baselines']

        im = Image.open(self.image.file.name)
        # will be fixed sometime in the future
        # if model_.one_channel_mode == '1':
        #     # TODO: need to binarize, probably not live...
        #     if not self.bw_image:
        #         self.binarize()
        #     im = Image.open(self.bw_image.file.name)
        # elif model_.one_channel_mode == 'L':
        #     im = Image.open(self.image.file.name).convert('L')
        # else:
        #     im = Image.open(self.image.file.name)

        options = {
            'device': getattr(settings, 'KRAKEN_TRAINING_DEVICE', 'cpu'),
            'model': model_
        }
        if text_direction:
            options['text_direction'] = text_direction

        with transaction.atomic():
            # cleanup pre-existing
            if steps in ['lines', 'both'] and override:
                self.lines.all().delete()
            if steps in ['regions', 'both'] and override:
                self.blocks.all().delete()

            res = blla.segment(im, **options)

            if steps in ['regions', 'both']:
                block_types = {t.name: t for t in self.document.valid_block_types.all()}
                for region_type, regions in res['regions'].items():
                    for region in regions:
                        Block.objects.create(
                            document_part=self,
                            typology=block_types.get(region_type),
                            box=region)

            regions = self.blocks.all()
            if steps in ['lines', 'both']:
                line_types = {t.name: t for t in self.document.valid_line_types.all()}
                for line in res['lines']:
                    mask = line['boundary'] if line['boundary'] is not None else None
                    baseline = line['baseline']

                    # calculate if the center of the line is contained in one of the region
                    # (pick the first one that matches)
                    center = LineString(baseline).interpolate(0.5, normalized=True)
                    region = next((r for r in regions if Polygon(r.box).contains(center)), None)

                    Line.objects.create(
                        document_part=self,
                        typology=line_types.get(line['script']),
                        block=region,
                        baseline=baseline,
                        mask=mask)

        im.close()

        self.workflow_state = self.WORKFLOW_STATE_SEGMENTED
        self.save()
        self.recalculate_ordering(read_direction=read_direction)

    def transcribe(self, model=None, text_direction=None):
        if model:
            trans, created = Transcription.objects.get_or_create(
                name='kraken:' + model.name,
                document=self.document)
            model_ = kraken_models.load_any(model.file.path)
            lines = self.lines.all()
            text_direction = (text_direction
                              or (self.document.main_script
                                  and self.document.main_script.text_direction)
                              or 'horizontal-lr')

            with Image.open(self.image.file.name) as im:
                for line in lines:
                    if not line.baseline:
                        bounds = {
                            'boxes': [line.box],
                            'text_direction': text_direction,
                            'type': 'baselines',
                            # 'script_detection': True
                        }
                    else:
                        bounds = {
                            'lines': [{'baseline': line.baseline,
                                       'boundary': line.mask,
                                       'text_direction': text_direction,
                                       'script': 'default'}],  # self.document.main_script.name
                            'type': 'baselines',
                            # 'selfcript_detection': True
                        }
                    it = rpred.rpred(
                            model_, im,
                            bounds=bounds,
                            pad=16,  # TODO: % of the image?
                            bidi_reordering=True)
                    lt, created = LineTranscription.objects.get_or_create(
                        line=line, transcription=trans)
                    for pred in it:
                        lt.content = pred.prediction
                    lt.save()
        else:
            Transcription.objects.get_or_create(
                name='manual',
                document=self.document)

        self.workflow_state = self.WORKFLOW_STATE_TRANSCRIBING
        self.calculate_progress()
        self.save()

    def chain_tasks(self, *tasks):
        redis_.set('process-%d' % self.pk, json.dumps({tasks[-1].name: {"status": "pending"}}))
        chain(*tasks).delay()

    def task(self, task_name, commit=True, **kwargs):
        if not self.tasks_finished():
            raise AlreadyProcessingException
        tasks = []
        if task_name == 'convert' or self.workflow_state < self.WORKFLOW_STATE_CONVERTED:
            sig = convert.si(self.pk)

            if getattr(settings, 'THUMBNAIL_ENABLE', True):
                sig.link(chain(lossless_compression.si(self.pk),
                               generate_part_thumbnails.si(self.pk)))
            else:
                sig.link(lossless_compression.si(self.pk))
            tasks.append(sig)

        if task_name == 'binarize':
            tasks.append(binarize.si(self.pk, **kwargs))

        if (task_name == 'segment' or (task_name == 'transcribe'
                                       and not self.segmented)):
            tasks.append(segment.si(self.pk, **kwargs))

        if task_name == 'transcribe':
            tasks.append(transcribe.si(self.pk, **kwargs))

        if commit:
            self.chain_tasks(*tasks)

        return tasks

    def make_masks(self, only=None):
        im = Image.open(self.image).convert('L')
        lines = list(self.lines.all())  # needs to store the qs result
        to_calc = [l for l in lines if (only and l.pk in only) or (only is None)]

        for line in to_calc:
            context = [l.baseline for l in lines if l.pk != line.pk]
            if line.block:
                poly = line.block.box
                poly.append(line.block.box[0])  # close it
                context.append(poly)

            mask = calculate_polygonal_environment(
                im,
                [line.baseline],
                suppl_obj=context,
                scale=(1200, 0))
            if mask[0]:
                line.mask = mask[0]
                line.save()

        return to_calc

    def enforce_line_order(self):
        # django-ordered-model doesn't care about unicity and linearity...
        lines = self.lines.order_by('order', 'pk')
        for i, line in enumerate(lines):
            if line.order != i:
                logger.debug('Enforcing line order %d : %d' % (line.pk, i))
                line.order = i
                line.save()


def validate_polygon(value):
    if value is None:
        return
    try:
        value[0][0]
    except (TypeError, KeyError, IndexError):
        raise ValidationError(
            _('%(value)s is not a polygon - a 2 dimensional array.'),
            params={'value': value})


def validate_2_points(value):
    if len(value) < 2:
        raise ValidationError(
            _('Polygon needs to have at least 2 points, it has %(length)d: %(value)s.'),
            params={'length': len(value), 'value': value})


def validate_3_points(value):
    if len(value) < 3:
        raise ValidationError(
            _('Polygon needs to have at least 3 points, it has %(length)d: %(value)s.'),
            params={'length': len(value), 'value': value})


class Block(OrderedModel, models.Model):
    """
    Represents a visualy close group of graphemes (characters) bound by the same semantic
    example: a paragraph, a margin note or floating text
    """
    # box = models.BoxField()  # in case we use PostGIS
    box = JSONField(validators=[validate_polygon, validate_3_points])
    typology = models.ForeignKey(BlockType, null=True, blank=True,
                                 on_delete=models.SET_NULL)
    document_part = models.ForeignKey(DocumentPart, on_delete=models.CASCADE,
                                      related_name='blocks')
    order_with_respect_to = 'document_part'

    external_id = models.CharField(max_length=128, blank=True, null=True)

    class Meta(OrderedModel.Meta):
        pass

    @property
    def coordinates_box(self):
        """
        Cast the box field to the format [xmin, ymin, xmax, ymax]
        to make it usable to calculate VPOS, HPOS, WIDTH, HEIGHT for Alto
        """
        return [*map(min, *self.box), *map(max, *self.box)]

    @property
    def width(self):
        return self.coordinates_box[2] - self.coordinates_box[0]

    @property
    def height(self):
        return self.coordinates_box[3] - self.coordinates_box[1]

    def make_external_id(self):
        self.external_id = 'eSc_textblock_%s' % str(uuid.uuid4())[:8]

    def save(self, *args, **kwargs):
        if self.external_id is None:
            self.make_external_id()
        return super().save(*args, **kwargs)


class LineManager(models.Manager):
    def prefetch_transcription(self, transcription):
        return (self.get_queryset().order_by('order')
                                   .prefetch_related(
                                       Prefetch('transcriptions',
                                                to_attr='transcription',
                                                queryset=LineTranscription.objects.filter(
                                                    transcription=transcription))))


class Line(OrderedModel):  # Versioned,
    """
    Represents a segmented line from a DocumentPart
    """
    # box = gis_models.PolygonField()  # in case we use PostGIS
    # Closed Polygon: [[x1, y1], [x2, y2], ..]
    mask = JSONField(null=True, blank=True, validators=[validate_polygon, validate_3_points])
    # Polygon: [[x1, y1], [x2, y2], ..]
    baseline = JSONField(null=True, blank=True, validators=[validate_polygon, validate_2_points])
    document_part = models.ForeignKey(DocumentPart,
                                      on_delete=models.CASCADE,
                                      related_name='lines')
    block = models.ForeignKey(Block, null=True, blank=True, on_delete=models.SET_NULL, related_name='lines')
    script = models.CharField(max_length=8, null=True, blank=True)  # choices ??
    # text direction
    order_with_respect_to = 'document_part'
    # version_ignore_fields = ('document_part', 'order')

    typology = models.ForeignKey(LineType, null=True, blank=True,
                                 on_delete=models.SET_NULL)

    external_id = models.CharField(max_length=128, blank=True, null=True)

    objects = LineManager()

    class Meta(OrderedModel.Meta):
        pass

    def __str__(self):
        return '%s#%d' % (self.document_part, self.order)

    @property
    def width(self):
        return self.box[2] - self.box[0]

    @property
    def height(self):
        return self.box[3] - self.box[1]

    def get_box(self):
        if self.mask:
            return [*map(min, *self.mask), *map(max, *self.mask)]
        else:
            return [*map(min, *self.baseline), *map(max, *self.baseline)]

    def set_box(self, box):
        self.mask = [(box[0], box[1]),
                     (box[0], box[3]),
                     (box[2], box[3]),
                     (box[2], box[1])]

    box = cached_property(get_box, set_box)

    def make_external_id(self):
        self.external_id = 'eSc_line_%s' % str(uuid.uuid4())[:8]

    def save(self, *args, **kwargs):
        if self.external_id is None:
            self.make_external_id()
        return super().save(*args, **kwargs)


class Transcription(models.Model):
    name = models.CharField(max_length=512)
    document = models.ForeignKey(Document, on_delete=models.CASCADE,
                                 related_name='transcriptions')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived = models.BooleanField(default=False)

    DEFAULT_NAME = 'manual'

    class Meta:
        ordering = ('-updated_at',)
        unique_together = (('name', 'document'),)

    def __str__(self):
        return self.name

    def archive(self):
        self.archived = True
        self.save()


class LineTranscription(Versioned, models.Model):
    """
    Represents a transcribded line of a document part in a given transcription
    """
    transcription = models.ForeignKey(Transcription, on_delete=models.CASCADE)
    content = models.CharField(blank=True, default="", max_length=2048)
    # graphs = [  # WIP
    # {c: <graph_code>, bbox: ((x1, y1), (x2, y2)), confidence: 0-1}
    # ]
    graphs = JSONField(null=True, blank=True)  # on postgres it maps to jsonb!

    # nullable in case we re-segment ?? for now we lose data.
    line = models.ForeignKey(Line, null=True, on_delete=models.CASCADE,
                             related_name='transcriptions')
    version_ignore_fields = ('line', 'transcription')

    class Meta:
        unique_together = (('line', 'transcription'),)

    @property
    def text(self):
        return re.sub('<[^<]+?>', '', self.content)


def models_path(instance, filename):
    fn, ext = os.path.splitext(filename)
    return 'models/%d/%s%s' % (instance.pk, slugify(fn), ext)


class OcrModel(Versioned, models.Model):
    name = models.CharField(max_length=256)
    file = models.FileField(upload_to=models_path, null=True,
                            validators=[FileExtensionValidator(
                                allowed_extensions=['mlmodel'])])

    MODEL_JOB_SEGMENT = 1
    MODEL_JOB_RECOGNIZE = 2
    MODEL_JOB_CHOICES = (
        (MODEL_JOB_SEGMENT, _("Segment")),
        (MODEL_JOB_RECOGNIZE, _("Recognize"))
    )
    job = models.PositiveSmallIntegerField(choices=MODEL_JOB_CHOICES)
    owner = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    training = models.BooleanField(default=False)
    training_epoch = models.PositiveSmallIntegerField(default=0)
    training_accuracy = models.FloatField(default=0.0)
    training_total = models.IntegerField(default=0)
    training_errors = models.IntegerField(default=0)
    document = models.ForeignKey(Document, blank=True, null=True,
                                 related_name='ocr_models',
                                 default=None, on_delete=models.SET_NULL)
    script = models.ForeignKey(Script, blank=True, null=True, on_delete=models.SET_NULL)

    version_ignore_fields = ('name', 'owner', 'document', 'script', 'training')
    version_history_max_length = None  # keep em all

    class Meta:
        ordering = ('-version_updated_at',)
        permissions = (('can_train', 'Can train models'),)

    def __str__(self):
        return self.name

    @cached_property
    def accuracy_percent(self):
        return self.training_accuracy * 100

    def segtrain(self, document, parts_qs, user=None):
        segtrain.delay(self.pk, document.pk,
                       list(parts_qs.values_list('pk', flat=True)))

    def train(self, parts_qs, transcription, user=None):
        train.delay(list(parts_qs.values_list('pk', flat=True)),
                    transcription.pk,
                    model_pk=self.pk,
                    user_pk=user and user.pk or None)

    def cancel_training(self):
        try:
            if self.job == self.MODEL_JOB_RECOGNIZE:
                task_id = json.loads(redis_.get('training-%d' % self.pk))['task_id']
            elif self.job == self.MODEL_JOB_SEGMENT:
                task_id = json.loads(redis_.get('segtrain-%d' % self.pk))['task_id']
        except (TypeError, KeyError) as e:
            raise ProcessFailureException(_("Couldn't find the training task."))
        else:
            if task_id:
                revoke(task_id, terminate=True)
                self.training = False
                self.save()

    # versioning
    def pack(self, **kwargs):
        # we use the name kraken generated
        kwargs['file'] = kwargs.get('file', self.file.name)
        return super().pack(**kwargs)

    def revert(self, revision):
        # we want the file to be swaped but the filename to stay the same
        for version in self.versions:
            if version['revision'] == revision:
                current_filename = self.file.path
                target_filename = os.path.join(settings.MEDIA_ROOT, version['data']['file'])
                tmp_filename = current_filename + '.tmp'
                break
        else:
            raise ValueError("Revision %s not found for %s" % (revision, self))
        os.rename(current_filename, tmp_filename)
        os.rename(target_filename, current_filename)
        os.rename(tmp_filename, target_filename)
        super().revert(revision)

    def delete_revision(self, revision):
        for version in self.versions:
            if version['revision'] == revision:
                os.remove(os.path.join(settings.MEDIA_ROOT, version['data']['file']))
                break
        super().delete_revision(revision)


@receiver(pre_delete, sender=DocumentPart, dispatch_uid='thumbnails_delete_signal')
def delete_thumbnails(sender, instance, using, **kwargs):
    thumbnailer = get_thumbnailer(instance.image)
    thumbnailer.delete_thumbnails()
    thumbnailer = get_thumbnailer(instance.bw_image)
    thumbnailer.delete_thumbnails()
