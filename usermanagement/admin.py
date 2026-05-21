from django.contrib import admin
from .models import (
    UserActivity,
    WorkflowFeedback
)

# Register your models here.
@admin.register(UserActivity)
class UserActivityAdmin(admin.ModelAdmin):

    list_display = (
        'id',
        'user',
        'workflow',
        'client_name',
        'project_name',
        'created_at',
    )

    list_filter = (
        'workflow',
        'created_at',
    )

    search_fields = (
        'user__username',
        'user__email',
        'client_name',
        'project_name',
    )

    ordering = ('-created_at',)


# ============================================================
# WORKFLOW FEEDBACK ADMIN
# ============================================================

@admin.register(WorkflowFeedback)
class WorkflowFeedbackAdmin(admin.ModelAdmin):

    list_display = (
        'id',
        'user',
        'workflow',
        'rating',
        'client_name',
        'project_name',
        'created_at',
    )

    list_filter = (
        'workflow',
        'rating',
        'created_at',
    )

    search_fields = (
        'user__username',
        'user__email',
        'feedback',
        'activity__client_name',
        'activity__project_name',
    )

    ordering = ('-created_at',)

    def client_name(self, obj):
        return obj.activity.client_name if obj.activity else '-'

    client_name.short_description = 'Client Name'

    def project_name(self, obj):
        return obj.activity.project_name if obj.activity else '-'

    project_name.short_description = 'Project Name'