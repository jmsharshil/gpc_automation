from django.db import models
import uuid


class AuditRecord(models.Model):
    serial_no = models.IntegerField()
    type = models.CharField(max_length=255)
    classification = models.CharField(max_length=255)
    project = models.CharField(max_length=255)
    auditor = models.CharField(max_length=255)
    question = models.TextField()
    response = models.TextField()

    # Pre-stored embedding — computed once, reused on every query (no N API calls)
    question_embedding = models.JSONField(null=True, blank=True)

    def __str__(self):
        return f"{self.serial_no} - {self.project}"


class DocumentChunk(models.Model):
    content = models.TextField()
    embedding = models.JSONField()
    status = models.CharField(max_length=20, default="processing")
    created_at = models.DateTimeField(auto_now_add=True)
    content_hash = models.CharField(max_length=64, null=True, blank=True)
    source = models.CharField(max_length=255, null=True, blank=True)

    # FIX: Each chunk is tagged with the job that created it.
    # get_relevant_chunks() filters by job_id so chunks from
    # previous requests are NEVER visible to new requests.
    # Chunks are also deleted after the job completes (cleanup_job_chunks).
    job_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["source"]),
            models.Index(fields=["job_id"]),         # fast per-job lookup
            models.Index(fields=["job_id", "status"]), # fast filtered query
        ]


class ProcessingJob(models.Model):
    STATUS_PENDING    = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED  = "completed"
    STATUS_FAILED     = "failed"

    STATUS_CHOICES = [
        (STATUS_PENDING,    "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED,  "Completed"),
        (STATUS_FAILED,     "Failed"),
    ]

    job_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)

    total_files      = models.IntegerField(default=0)
    processed_files  = models.IntegerField(default=0)
    total_chunks     = models.IntegerField(default=0)
    processed_chunks = models.IntegerField(default=0)

    total_queries     = models.IntegerField(default=0)
    processed_queries = models.IntegerField(default=0)

    current_phase = models.CharField(max_length=50, default="pending")
    current_file  = models.CharField(max_length=255, null=True, blank=True)
    current_query = models.TextField(null=True, blank=True)

    error_message = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def file_progress(self):
        if self.total_chunks == 0:
            return 100.0
        return round((self.processed_chunks / self.total_chunks) * 100, 1)

    def query_progress(self):
        if self.total_queries == 0:
            return 0.0
        return round((self.processed_queries / self.total_queries) * 100, 1)

    def __str__(self):
        return f"Job {self.job_id} [{self.status}]"


class ProcessingJobFile(models.Model):
    job              = models.ForeignKey(ProcessingJob, on_delete=models.CASCADE, related_name="files")
    file_name        = models.CharField(max_length=255)
    total_chunks     = models.IntegerField(default=0)
    processed_chunks = models.IntegerField(default=0)
    status           = models.CharField(max_length=20, default="pending")
    error            = models.TextField(null=True, blank=True)

    def progress(self):
        if self.total_chunks == 0:
            return 0.0
        return round((self.processed_chunks / self.total_chunks) * 100, 1)