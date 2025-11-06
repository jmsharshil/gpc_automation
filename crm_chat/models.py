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


    class Meta:
        ordering = ["created_at"]


class UserOpenAISetting(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='openai_setting')
    default_model = models.CharField(max_length=100, default='gpt-4o')
    temperature = models.FloatField(default=0.2)
    max_tokens = models.IntegerField(default=1024)


    def __str__(self):
        return f"OpenAI settings for {self.user}"