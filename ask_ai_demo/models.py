# from django.conf import settings
# from django.db import models
# from django.utils import timezone

# class Chat(models.Model):
#     owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chats")
#     title = models.CharField(max_length=255, blank=True)
#     system_prompt = models.TextField(blank=True)
#     created_at = models.DateTimeField(default=timezone.now)
#     updated_at = models.DateTimeField(auto_now=True)
#     metadata = models.JSONField(default=dict, blank=True)


#     def __str__(self):
#         return self.title or f"Chat {self.pk}"


# class Message(models.Model):
#     ROLE_CHOICES = (("system", "system"), ("user", "user"), ("assistant", "assistant"))
#     chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="messages")
#     role = models.CharField(max_length=20, choices=ROLE_CHOICES)
#     content = models.TextField()
#     tokens = models.IntegerField(null=True, blank=True)
#     openai_response_id = models.CharField(max_length=255, blank=True)
#     created_at = models.DateTimeField(default=timezone.now)
#     metadata = models.JSONField(default=dict, blank=True)
    
#     attachment = models.FileField(upload_to='chat_attachments/%Y/%m/%d/', null=True, blank=True)
#     attachment_name = models.CharField(max_length=512, blank=True)
#     attachment_content_type = models.CharField(max_length=255, blank=True)


#     class Meta:
#         ordering = ["created_at"]


# class UserOpenAISetting(models.Model):
#     user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='openai_setting')
#     default_model = models.CharField(max_length=100, default='gpt-4.1')
#     temperature = models.FloatField(default=0.3)
#     max_tokens = models.IntegerField(default=1200)


#     def __str__(self):
#         return f"OpenAI settings for {self.user}"

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.db.models.signals import post_save
from django.dispatch import receiver

class ChatDemo(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chats_demo")
    title = models.CharField(max_length=255, blank=True)
    system_prompt = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    metadata = models.JSONField(default=dict, blank=True)
    is_processing_document = models.BooleanField(default=False)

    has_document = models.BooleanField(default=False)
    document_name = models.CharField(max_length=512, blank=True)


    def __str__(self):
        return self.title or f"Chat {self.pk}"


class MessageDemo(models.Model):
    ROLE_CHOICES = (("system", "system"), ("user", "user"), ("assistant", "assistant"))
    chat = models.ForeignKey(ChatDemo, on_delete=models.CASCADE, related_name="messages_demo")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    tokens = models.IntegerField(null=True, blank=True)
    openai_response_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    metadata = models.JSONField(default=dict, blank=True)
    
    attachment = models.FileField(upload_to='chat_attachments/%Y/%m/%d/', max_length=1000, null=True, blank=True)
    attachment_name = models.CharField(max_length=512, blank=True)
    attachment_content_type = models.CharField(max_length=255, blank=True)

    edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)


    class Meta:
        ordering = ["created_at"]

class DocumentChunkDemo(models.Model):
    chat=models.ForeignKey(ChatDemo, on_delete=models.CASCADE, related_name="document_chunks_demo")
    page_number = models.IntegerField()
    chunk_index = models.IntegerField()
    section_title = models.CharField(max_length=255, blank=True)
    text=models.TextField()
    embedding = models.BinaryField()  # Store embedding as binary data
    embedding_model = models.CharField(max_length=100, default='text-embedding-3-small')
    char_count = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['chat', 'page_number']),
            models.Index(fields=['chat', 'chunk_index']),
        ]
    def __str__(self):
        return f"Chunk {self.chunk_index} - Page {self.page_number}"
 
class DocumentProcessingDemo(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    )
    
    chat = models.OneToOneField(ChatDemo, on_delete=models.CASCADE, related_name="document_processing_demo")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    # Processing details
    total_pages = models.IntegerField(default=0)
    processed_pages = models.IntegerField(default=0)
    total_chunks = models.IntegerField(default=0)
    
    # Error handling
    error_message = models.TextField(blank=True)
    
    # Timestamps
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        verbose_name_plural = "Document Processings"
 
    def __str__(self):
        return f"Processing for {self.chat.title} - {self.status}"

class UserOpenAISettingDemo(models.Model):
    REASONING_EFFORT_CHOICES = [
        ('minimal', 'Minimal'),
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='openai_setting_demo')
    default_model = models.CharField(max_length=100, default='gpt-4o')
    temperature = models.FloatField(default=0.2)  # kept for backward compat with non-reasoning models
    max_tokens = models.IntegerField(default=1024)
    reasoning_effort = models.CharField(max_length=20,choices=REASONING_EFFORT_CHOICES,default='medium',help_text="Used for reasoning-tier models instead of temperature.")
    use_rag_for_documents = models.BooleanField(default=True)
    max_context_chunks = models.IntegerField(default=10)  # Number of relevant chunks to include
    context_memory = models.TextField(blank=True, help_text="Long-term memory about the user across all chats")

    def __str__(self):
        return f"OpenAI settings for {self.user}"
    
    def is_reasoning_model(self) -> bool:
        """Returns True if the configured model is a reasoning-tier model that uses
        reasoning_effort instead of temperature. Extend this list as new models ship.
        """
        reasoning_models = ('o1', 'o3', 'o4', 'gpt-5', 'gpt-5.6')
        return any(self.default_model.startswith(m) for m in reasoning_models)
    
@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_openai_setting(sender, instance, created, **kwargs):
    if created:
        UserOpenAISettingDemo.objects.get_or_create(user=instance)


class BusinessContextMemoryDemo(models.Model):
    """
    Business-level context memory shared across all users in crm_chat.
    Created and updated by users (e.g. admins, managers, or authorized team members).
    This memory provides organization-wide rules, guidelines, facts, and standards
    that are injected into the system prompt for all users alongside their personal context memory.
    """
    title = models.CharField(max_length=255, default="Company Business Memory")
    content = models.TextField(
        blank=True,
        help_text="Shared organization-level rules, standards, guidelines, and domain knowledge."
    )
    is_active = models.BooleanField(default=True, db_index=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_business_memories_demo"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = "Business Context Memory"
        verbose_name_plural = "Business Context Memories"

    def __str__(self):
        return f"{self.title} (Active: {self.is_active})"

    @classmethod
    def get_active_memory(cls) -> str:
        """Returns the active business memory content or empty string."""
        active = cls.objects.filter(is_active=True).first()
        return active.content.strip() if active and active.content else ""


class BusinessDocumentDemo(models.Model):
    """
    Stores uploaded organization-level knowledge documents (e.g. Valuation Manuals,
    IRS guidelines, Standard Operating Procedures, Contracts, Presentations).
    """
    business_memory = models.ForeignKey(
        BusinessContextMemoryDemo,
        on_delete=models.CASCADE,
        related_name="documents_demo"
    )
    file = models.FileField(upload_to="business_docs/%Y/%m/")
    name = models.CharField(max_length=512)
    file_type = models.CharField(max_length=50, blank=True)
    total_pages = models.IntegerField(default=1)
    total_chunks = models.IntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_business_docs"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Business Document"
        verbose_name_plural = "Business Documents"

    def __str__(self):
        return f"{self.name} ({self.total_chunks} chunks)"


class BusinessDocumentChunkDemo(models.Model):
    """
    Stores vector embeddings for chunks of business documents for semantic RAG retrieval.
    """
    business_document = models.ForeignKey(
        BusinessDocumentDemo,
        on_delete=models.CASCADE,
        related_name="chunks_demo"
    )
    page_number = models.IntegerField()
    chunk_index = models.IntegerField()
    text = models.TextField()
    embedding = models.BinaryField()
    embedding_model = models.CharField(max_length=100, default='text-embedding-3-small')
    char_count = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['business_document', 'chunk_index']),
        ]
        verbose_name = "Business Document Chunk"
        verbose_name_plural = "Business Document Chunks"

    def __str__(self):
        return f"Chunk {self.chunk_index} (Page {self.page_number}) of {self.business_document.name}"

