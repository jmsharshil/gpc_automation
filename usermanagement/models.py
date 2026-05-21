from django.db import models
from django.db import models
from django.conf import settings
# Create your models here.
 
class UserActivity(models.Model):
    """Records which workflow a user used and optional client name."""
    WORKFLOW_CHOICES = (
        ('gpc_screening', 'GPC Screening'),
        ('transaction_screening', 'Transaction Screening'),
        ('audit_ai', 'Audit AI'),
        ('ask_ai', 'Ask AI'),
        ("article_interpretation_ai","Article Interpertation AI")
    )
 
    user = models.ForeignKey(
        'user_auth.User', on_delete=models.CASCADE, related_name='activities'
    )
    workflow = models.CharField(max_length=30, choices=WORKFLOW_CHOICES)
    client_name = models.CharField(max_length=512, blank=True, default='')
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    project_name = models.CharField(max_length=255, blank=True, null=True)
 
    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'User Activities'
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['workflow']),
        ]
 
    def __str__(self):
        return f"{self.user.username} — {self.get_workflow_display()} — {self.created_at:%Y-%m-%d %H:%M}"
 
 
class WorkflowFeedback(models.Model):
    """User rating (1-5) and optional feedback text for a workflow."""
 
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
        'user_auth.User', on_delete=models.CASCADE, related_name='feedbacks'
    )
    
    activity = models.ForeignKey(
        'UserActivity',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='feedbacks'
    )
    
    workflow = models.CharField(max_length=40, choices=WORKFLOW_CHOICES)
    rating = models.DecimalField(max_digits=2, decimal_places=1, choices=RATING_CHOICES)
    feedback = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
 
    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Workflow Feedbacks'
 
    def __str__(self):
        return f"{self.user.username} — {self.get_workflow_display()} — ⭐{self.rating}"