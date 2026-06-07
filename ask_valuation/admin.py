from django.contrib import admin
from .models import Guide, GuideChunk, ValuationSession, ValuationMessage

@admin.register(Guide)
class GuideAdmin(admin.ModelAdmin):
    list_display = ['name', 'is_active', 'total_pages', 'year','total_chunks', 'processing_status', 'created_at']
    list_filter = ['is_active', 'processing_status']
    search_fields = ['name', 'year']
    readonly_fields = ['total_pages', 'total_chunks', 'processing_status', 'processing_error', 'created_at', 'updated_at']
    ordering = ['name']


@admin.register(GuideChunk)
class GuideChunkAdmin(admin.ModelAdmin):
    list_display = ['guide', 'page_number', 'chunk_index', 'char_count', 'embedding_model']
    list_filter = ['guide', 'embedding_model']
    search_fields = ['guide__name', 'text']
    readonly_fields = ['guide', 'page_number', 'chunk_index', 'text', 'embedding', 'char_count', 'embedding_model', 'created_at']


@admin.register(ValuationSession)
class ValuationSessionAdmin(admin.ModelAdmin):
    list_display = ['id', 'title', 'owner', 'guide_list', 'created_at', 'updated_at']
    search_fields = ['title', 'owner__email']
    filter_horizontal = ['selected_guides']
    readonly_fields = ['created_at', 'updated_at']

    def guide_list(self, obj):
        return ", ".join(g.name for g in obj.selected_guides.all()) or "—"
    guide_list.short_description = "Guides"


@admin.register(ValuationMessage)
class ValuationMessageAdmin(admin.ModelAdmin):
    list_display = ['id', 'session', 'role', 'short_content', 'edited', 'tokens_used', 'created_at']
    list_filter = ['role', 'edited']
    search_fields = ['content', 'session__title']
    readonly_fields = ['session', 'role', 'sources', 'guides_used', 'tokens_used', 'created_at', 'edited_at']

    def short_content(self, obj):
        return obj.content[:80] + ("…" if len(obj.content) > 80 else "")
    short_content.short_description = "Content"
