from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

User = get_user_model()


class TaskReport(models.Model):
    WORKFLOW_STATE_QUEUED = 0
    WORKFLOW_STATE_STARTED = 1
    WORKFLOW_STATE_ERROR = 2
    WORKFLOW_STATE_DONE = 3
    WORKFLOW_STATE_CHOICES = (
        (WORKFLOW_STATE_QUEUED, _("Queued")),
        (WORKFLOW_STATE_STARTED, _("Running")),
        (WORKFLOW_STATE_ERROR, _("Crashed")),
        (WORKFLOW_STATE_DONE, _("Finished"))
    )

    workflow_state = models.PositiveSmallIntegerField(
        default=WORKFLOW_STATE_QUEUED,
        choices=WORKFLOW_STATE_CHOICES
    )
    label = models.CharField(max_length=256)
    messages = models.TextField(blank=True)

    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    done_at = models.DateTimeField(null=True)

    user = models.ForeignKey(User, on_delete=models.CASCADE)

    # celery task id
    task_id = models.CharField(max_length=64, blank=True, null=True)

    def append(self, text):
        self.messages += text + '\n'

    @property
    def uri(self):
        return reverse('report-detail', kwargs={'pk': self.pk})

    def start(self, task_id):
        self.task_id = task_id
        self.workflow_state = self.WORKFLOW_STATE_STARTED
        self.started_at = datetime.now(timezone.utc)
        self.save()

    def error(self, message):
        # unrecoverable error
        self.workflow_state = self.WORKFLOW_STATE_ERROR
        self.append(message)
        self.save()

        self.user.notify(_('%(task_label)s error!') % {'task_label': self.label},
                         level='danger',
                         links=[{'text': 'Report', 'src': self.uri}])

    def end(self, extra_links=None):
        self.workflow_state = self.WORKFLOW_STATE_DONE
        self.done_at = datetime.now(timezone.utc)
        self.save()

        links = extra_links or []
        if self.messages != '':
            links.append({'text': 'Report', 'src': self.uri})
        self.user.notify(_('%(task_label)s done!') % {'task_label': self.label},
                         level='success',
                         links=links)
