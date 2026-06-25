from django.conf import settings
from django.db import models
from django.utils import timezone


class ArticlesOpenAISetting(models.Model):
    """
    Per-user OpenAI settings for the Articles Extractor.
    Matches the same pattern as your existing UserOpenAISetting.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='articles_openai_setting',
    )
    default_model = models.CharField(max_length=100, default='gpt-5.4-mini')
    reasoning_effort = models.CharField(
        max_length=10,
        choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('xhigh', 'X-High')],
        default='medium',
    )
    quality_reasoning_effort = models.CharField(
        max_length=10,
        choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('xhigh', 'X-High')],
        default='high',
    )
    max_output_tokens = models.IntegerField(default=8000)
    max_chars = models.IntegerField(default=120_000)
    quality_pass = models.BooleanField(default=True)
    seniority_pass = models.BooleanField(default=True)

    def __str__(self):
        return f"Articles OpenAI settings for {self.user}"


class ArticleDocument(models.Model):
    """
    Stores one uploaded document per record.
    API 1 creates this. API 2 reads it.
    """
    STATUS_CHOICES = [
        ('pending',    'Pending'),
        ('processing', 'Processing'),
        ('completed',  'Completed'),
        ('failed',     'Failed'),
    ]

    uploaded_by    = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='article_documents',
    )
    file           = models.FileField(upload_to='articles/uploads/%Y/%m/%d/')
    original_name  = models.CharField(max_length=512)
    status         = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    error_message  = models.TextField(blank=True)
    created_at     = models.DateTimeField(default=timezone.now)
    completed_at   = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Doc #{self.pk} — {self.original_name} [{self.status}]"


class ArticleExtractionResult(models.Model):
    """
    The full extracted JSON result for a document (API 2 output).
    Raw JSON is stored so you can always re-serve it without re-calling OpenAI.
    """
    document     = models.OneToOneField(
        ArticleDocument,
        on_delete=models.CASCADE,
        related_name='extraction_result',
    )
    company_name  = models.CharField(max_length=500, blank=True)
    document_name = models.CharField(max_length=500, blank=True)
    # Full raw JSON returned by OpenAI (the securities array + meta)
    raw_json      = models.JSONField(default=dict)
    created_at    = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Result for Doc #{self.document_id}"
    
class ArticleGlobalOpenAISetting(models.Model):
    """
    Global OpenAI settings used by ALL users.
    Only one record should exist.
    """

    default_model = models.CharField(
        max_length=100,
        default='gpt-5.4-mini'
    )

    reasoning_effort = models.CharField(
        max_length=10,
        choices=[
            ('low', 'Low'),
            ('medium', 'Medium'),
            ('high', 'High'),
            ('xhigh', 'X-High'),
        ],
        default='medium',
    )

    quality_reasoning_effort = models.CharField(
        max_length=10,
        choices=[
            ('low', 'Low'),
            ('medium', 'Medium'),
            ('high', 'High'),
            ('xhigh', 'X-High'),
        ],
        default='high',
    )

    max_output_tokens = models.IntegerField(default=8000)
    max_chars = models.IntegerField(default=120000)

    quality_pass = models.BooleanField(default=True)
    seniority_pass = models.BooleanField(default=True)

    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.pk = 1  # force single row
        super().save(*args, **kwargs)

    @classmethod
    def get_settings(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Global Articles OpenAI Settings"
    
class ExtractionRecord(models.Model):
    """
    Stores one full extraction result per API call, linked to the user
    who triggered it.  The raw_json field is the living copy — it is
    updated in-place whenever the user edits a security field via the
    update API.
    """
    STATUS_CHOICES = [
        ("pending","Pending"),
        ("processing", "Processing"),
        ("done","Done"),
        ("failed","Failed"),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='extraction_records',
    )
    original_name = models.CharField(max_length=512, blank=True)
    company_name  = models.CharField(max_length=500, blank=True)
    document_name = models.CharField(max_length=500, blank=True)
    status= models.CharField(max_length=20,choices=STATUS_CHOICES,default="pending",db_index=True,)
    error_message = models.TextField(blank=True)
    raw_json      = models.JSONField(default=dict)
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Extraction #{self.pk} — {self.company_name} ({self.user})"


class SecurityFieldAudit(models.Model):
    """
    One row per field change made via the update API.
    Records who changed what, from what value to what value, and when.
    """
    extraction    = models.ForeignKey(
        ExtractionRecord,
        on_delete=models.CASCADE,
        related_name='audit_logs',
    )
    changed_by    = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='security_field_changes',
    )
    security_index = models.IntegerField(
        help_text="0-based index of the security inside raw_json['securities']",
    )
    security_name  = models.CharField(
        max_length=500, blank=True,
        help_text="Snapshot of security_name at the time of the change",
    )
    field_name     = models.CharField(
        max_length=200,
        help_text="Name of the field that was changed, e.g. 'conversion_ratio'",
    )
    old_value      = models.TextField(blank=True)
    new_value      = models.TextField(blank=True)
    changed_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-changed_at']

    def __str__(self):
        return (
            f"Audit #{self.pk} — {self.field_name} on "
            f"'{self.security_name}' by {self.changed_by} at {self.changed_at:%Y-%m-%d %H:%M}"
        )