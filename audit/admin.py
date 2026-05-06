from django.contrib import admin
from .models import AuditRecord, DocumentChunk, ProcessingJob, ProcessingJobFile


@admin.register(AuditRecord)
class AuditRecordAdmin(admin.ModelAdmin):
    list_display = ("serial_no", "project", "type", "classification", "auditor")
    search_fields = ("project", "type", "classification", "auditor", "question")
    list_filter = ("type", "classification", "project")
    ordering = ("serial_no",)
    readonly_fields = ("question_embedding","response_embedding")


@admin.register(DocumentChunk)
class DocumentChunkAdmin(admin.ModelAdmin):
    list_display = ("short_content", "status", "source", "created_at")
    search_fields = ("content", "source")
    list_filter = ("status", "source", "created_at")
    readonly_fields = ("embedding", "created_at", "content_hash")

    def short_content(self, obj):
        return obj.content[:75] + "..." if len(obj.content) > 75 else obj.content
    short_content.short_description = "Content Preview"


class ProcessingJobFileInline(admin.TabularInline):
    model = ProcessingJobFile
    extra = 0
    readonly_fields = ("file_name", "total_chunks", "processed_chunks", "status", "error", "progress_display")

    def progress_display(self, obj):
        return f"{obj.progress()}%"
    progress_display.short_description = "Progress"


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin):
    list_display = (
        "job_id",
        "status",
        "current_phase",
        "file_progress_display",
        "query_progress_display",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "current_phase", "created_at")
    search_fields = ("job_id", "current_file", "current_query")
    readonly_fields = (
        "job_id",
        "created_at",
        "updated_at",
        "file_progress_display",
        "query_progress_display",
    )
    inlines = [ProcessingJobFileInline]

    def file_progress_display(self, obj):
        return f"{obj.file_progress()}%"
    file_progress_display.short_description = "File Progress"

    def query_progress_display(self, obj):
        return f"{obj.query_progress()}%"
    query_progress_display.short_description = "Query Progress"


@admin.register(ProcessingJobFile)
class ProcessingJobFileAdmin(admin.ModelAdmin):
    list_display = ("file_name", "job", "status", "progress_display")
    list_filter = ("status",)
    search_fields = ("file_name", "job__job_id")
    readonly_fields = ("progress_display",)

    def progress_display(self, obj):
        return f"{obj.progress()}%"
    progress_display.short_description = "Progress"