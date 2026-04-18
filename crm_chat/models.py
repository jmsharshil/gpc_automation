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

class Chat(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chats")
    title = models.CharField(max_length=255, blank=True)
    system_prompt = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    metadata = models.JSONField(default=dict, blank=True)

    has_document = models.BooleanField(default=False)
    document_name = models.CharField(max_length=512, blank=True)


    def __str__(self):
        return self.title or f"Chat {self.pk}"


class Message(models.Model):
    ROLE_CHOICES = (("system", "system"), ("user", "user"), ("assistant", "assistant"))
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    tokens = models.IntegerField(null=True, blank=True)
    openai_response_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    metadata = models.JSONField(default=dict, blank=True)
    
    attachment = models.FileField(upload_to='chat_attachments/%Y/%m/%d/', null=True, blank=True)
    attachment_name = models.CharField(max_length=512, blank=True)
    attachment_content_type = models.CharField(max_length=255, blank=True)

    edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)


    class Meta:
        ordering = ["created_at"]

class DocumentChunk(models.Model):
    chat=models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="document_chunks")
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
 
class DocumentProcessing(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    )
    
    chat = models.OneToOneField(Chat, on_delete=models.CASCADE, related_name="document_processing")
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

class UserOpenAISetting(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='openai_setting')
    default_model = models.CharField(max_length=100, default='gpt-4o')
    temperature = models.FloatField(default=0.2)
    max_tokens = models.IntegerField(default=1024)

    use_rag_for_documents = models.BooleanField(default=True)
    max_context_chunks = models.IntegerField(default=10)  # Number of relevant chunks to include


    def __str__(self):
        return f"OpenAI settings for {self.user}"