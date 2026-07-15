from django.contrib import admin
from .models import ClientProjectSession, UserActivity, WorkflowFeedback, ClientMaster


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

    readonly_fields = (
        'created_at',
    )

    ordering = ('-created_at',)

    date_hierarchy = 'created_at'

    list_per_page = 25

    fieldsets = (
        ('User Information', {
            'fields': (
                'user',
                'workflow',
            )
        }),
        ('Project Details', {
            'fields': (
                'client_name',
                'project_name',
            )
        }),
        ('Additional Details', {
            'fields': (
                'details',
            )
        }),
        ('Timestamps', {
            'fields': (
                'created_at',
            )
        }),
    )


@admin.register(WorkflowFeedback)
class WorkflowFeedbackAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'workflow',
        'rating',
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
    )

    readonly_fields = (
        'created_at',
    )

    ordering = ('-created_at',)

    date_hierarchy = 'created_at'

    list_per_page = 25

    fieldsets = (
        ('Feedback Information', {
            'fields': (
                'user',
                'workflow',
                'rating',
            )
        }),
        ('User Feedback', {
            'fields': (
                'feedback',
            )
        }),
        ('Timestamp', {
            'fields': (
                'created_at',
            )
        }),
    )


@admin.register(ClientMaster)
class ClientMasterAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'name',
        'is_active',
        'created_at',
        'updated_at',
    )

    list_filter = (
        'is_active',
        'created_at',
        'updated_at',
    )

    search_fields = (
        'name',
    )

    readonly_fields = (
        'created_at',
        'updated_at',
    )

    ordering = ('name',)

    list_editable = (
        'is_active',
    )

    list_per_page = 25

    fieldsets = (
        ('Client Information', {
            'fields': (
                'name',
                'is_active',
            )
        }),
        ('Timestamps', {
            'fields': (
                'created_at',
                'updated_at',
            )
        }),
    )
    
admin.register(ClientProjectSession)
class ClientProjectSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'client_name', 'project_name', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('user__username', 'client_name', 'project_name')
    readonly_fields = ('created_at',)