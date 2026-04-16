from django.db import models

class AuditRecord(models.Model):
    serial_no = models.IntegerField()
    type = models.CharField(max_length=255)
    classification = models.CharField(max_length=255)
    project = models.CharField(max_length=255)
    auditor = models.CharField(max_length=255)
    question = models.TextField()
    response = models.TextField()

    def __str__(self):
        return f"{self.serial_no} - {self.project}"
    
class DocumentChunk(models.Model):
    content = models.TextField()
    embedding = models.JSONField()
    status = models.CharField(max_length=20, default="processing")
    created_at = models.DateTimeField(auto_now_add=True)
    content_hash = models.CharField(max_length=64, null=True, blank=True)
    source = models.CharField(max_length=255, null=True, blank=True)