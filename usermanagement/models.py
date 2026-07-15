from django.db import models
from django.db import models
from django.conf import settings
# Create your models here.
 
class UserActivity(models.Model):
    """Records workflow usage with client/project details."""

    WORKFLOW_CHOICES = (
        ('gpc_screening', 'GPC Screening'),
        ('transaction_screening', 'Transaction Screening'),
        ('audit_ai', 'Audit AI'),
        ('ask_ai', 'Ask AI'),
        ('article_interpretation_ai', 'Article Interpretation AI'),
    )

    user = models.ForeignKey(
        'user_auth.User',
        on_delete=models.CASCADE,
        related_name='activities'
    )

    workflow = models.CharField(
        max_length=30,
        choices=WORKFLOW_CHOICES
    )

    client_name = models.CharField(
        max_length=512,
        blank=True,
        default=''
    )

    project_name = models.CharField(
        max_length=255,
        blank=True,
        null=True
    )

    details = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'User Activities'

        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['workflow']),
        ]

    def __str__(self):
        return (
            f"{self.user.username} — "
            f"{self.get_workflow_display()} — "
            f"{self.created_at:%Y-%m-%d %H:%M}"
        )


class WorkflowFeedback(models.Model):
    """Single feedback per user per workflow."""

    WORKFLOW_CHOICES = (
        ('gpc_screening', 'GPC Screening'),
        ('transaction_screening', 'Transaction Screening'),
        ('audit_ai', 'Audit AI'),
        ('ask_ai', 'Ask AI'),
        ('article_interpretation_ai', 'Article Interpretation AI'),
    )

    RATING_CHOICES = (
        (0.5, '0.5'),
        (1.0, '1 — Poor'),
        (1.5, '1.5'),
        (2.0, '2 — Fair'),
        (2.5, '2.5'),
        (3.0, '3 — Good'),
        (3.5, '3.5'),
        (4.0, '4 — Very Good'),
        (4.5, '4.5'),
        (5.0, '5 — Excellent'),
    )

    user = models.ForeignKey(
        'user_auth.User',
        on_delete=models.CASCADE,
        related_name='feedbacks'
    )

    workflow = models.CharField(
        max_length=40,
        choices=WORKFLOW_CHOICES
    )

    rating = models.DecimalField(
        max_digits=2,
        decimal_places=1,
        choices=RATING_CHOICES
    )

    feedback = models.TextField(
        blank=True,
        default=''
    )

    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Workflow Feedbacks'

        unique_together = ('user', 'workflow')

    def __str__(self):
        return (
            f"{self.user.username} — "
            f"{self.get_workflow_display()} — ⭐{self.rating}"
        )
    
class ClientMaster(models.Model):
    name = models.CharField(
        max_length=255,
        unique=True
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name
    
class ClientProjectSession(models.Model):
    """
    Stores the client and project submitted by a user at login.
    The primary key (id) of this model is used as a session ID to track workflow activities.
    """
 
    user = models.ForeignKey('user_auth.User', on_delete=models.CASCADE, related_name='client_sessions')
    client_name = models.CharField(max_length=512)
    project_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
 
    def __str__(self):
        return f"Session {self.id} — {self.client_name} / {self.project_name}"