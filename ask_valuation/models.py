import json
import io
import logging
from django.conf import settings
from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# GUIDE  (admin-managed PDF guides)
# ─────────────────────────────────────────────

class Guide(models.Model):
    """A single valuation-guideline PDF uploaded by an admin."""

    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True)
    pdf_file = models.FileField(upload_to='ask_valuation/guides/')
    is_active = models.BooleanField(default=True)
    total_pages = models.IntegerField(default=0)
    total_chunks = models.IntegerField(default=0)
    processing_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
        ],
        default='pending',
    )
    processing_error = models.TextField(blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

class GuideChunk(models.Model):
    """One semantic chunk extracted from a Guide PDF."""

    guide = models.ForeignKey(Guide, on_delete=models.CASCADE, related_name='chunks')
    page_number = models.IntegerField()
    chunk_index = models.IntegerField()
    text = models.TextField()
    embedding = models.BinaryField()          # JSON-encoded float list
    embedding_model = models.CharField(max_length=100, default='text-embedding-3-small')
    char_count = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['guide', 'page_number']),
            models.Index(fields=['guide', 'chunk_index']),
        ]

    def __str__(self):
        return f"[{self.guide.name}] Chunk {self.chunk_index} – Page {self.page_number}"

class ValuationSession(models.Model):
    """A user's Q&A conversation, tied to one or more selected guides."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='valuation_sessions',
    )
    title = models.CharField(max_length=255, blank=True)
    system_prompt = models.TextField(blank=True)
    selected_guides = models.ManyToManyField(
        Guide,
        related_name='sessions',
        blank=True,
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.title or f"Session {self.pk}"

class ValuationMessage(models.Model):
    """A single message (user question or assistant answer) in a ValuationSession."""

    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
    ]

    session = models.ForeignKey(
        ValuationSession,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()

    # Source citation: list of {guide_id, guide_name}
    sources = models.JSONField(default=list, blank=True)

    # The guide IDs that were active when this Q was asked
    guides_used = models.JSONField(default=list, blank=True)

    # Editing support
    edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)

    # Token tracking
    tokens_used = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"[{self.session}] {self.role}: {self.content[:60]}"
    




