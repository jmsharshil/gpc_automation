from django.contrib import admin

from .models import (
    ArticlesOpenAISetting,
    ArticleDocument,
    ArticleExtractionResult,
)


@admin.register(ArticlesOpenAISetting)
class ArticlesOpenAISettingAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "default_model",
        "reasoning_effort",
        "quality_reasoning_effort",
        "quality_pass",
        "seniority_pass",
        "max_output_tokens",
        "max_chars",
    )
    list_filter = (
        "reasoning_effort",
        "quality_reasoning_effort",
        "quality_pass",
        "seniority_pass",
    )
    search_fields = (
        "user__username",
        "user__email",
    )


@admin.register(ArticleDocument)
class ArticleDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "original_name",
        "uploaded_by",
        "status",
        "created_at",
        "completed_at",
    )
    list_filter = (
        "status",
        "created_at",
        "completed_at",
    )
    search_fields = (
        "original_name",
        "uploaded_by__username",
        "uploaded_by__email",
    )
    readonly_fields = (
        "created_at",
        "completed_at",
    )
    date_hierarchy = "created_at"

    fieldsets = (
        (
            "Document Information",
            {
                "fields": (
                    "uploaded_by",
                    "file",
                    "original_name",
                )
            },
        ),
        (
            "Processing Status",
            {
                "fields": (
                    "status",
                    "error_message",
                    "created_at",
                    "completed_at",
                )
            },
        ),
    )


@admin.register(ArticleExtractionResult)
class ArticleExtractionResultAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "document",
        "company_name",
        "document_name",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = (
        "company_name",
        "document_name",
        "document__original_name",
    )
    readonly_fields = (
        "created_at",
        "raw_json",
    )

    fieldsets = (
        (
            "Extraction Details",
            {
                "fields": (
                    "document",
                    "company_name",
                    "document_name",
                    "created_at",
                )
            },
        ),
        (
            "Raw JSON Output",
            {
                "fields": ("raw_json",),
            },
        ),
    )