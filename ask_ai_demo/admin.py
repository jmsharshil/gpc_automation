from django.contrib import admin
from .models import ChatDemo, MessageDemo, UserOpenAISettingDemo, BusinessContextMemoryDemo, BusinessDocumentDemo, BusinessDocumentChunkDemo




@admin.register(ChatDemo)
class ChatAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'owner', 'created_at', 'updated_at')
    search_fields = ('title', 'owner__username')




@admin.register(MessageDemo)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'chat', 'role', 'created_at')
    search_fields = ('content',)




@admin.register(UserOpenAISettingDemo)
class UserOpenAISettingAdmin(admin.ModelAdmin):
    list_display = ('user', 'default_model', 'temperature', 'max_tokens')


@admin.register(BusinessContextMemoryDemo)
class BusinessContextMemoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'is_active', 'updated_by', 'updated_at')
    search_fields = ('title', 'content')
    list_filter = ('is_active',)


@admin.register(BusinessDocumentDemo)
class BusinessDocumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'business_memory', 'total_pages', 'total_chunks', 'uploaded_by', 'created_at')
    search_fields = ('name',)
    list_filter = ('business_memory',)


@admin.register(BusinessDocumentChunkDemo)
class BusinessDocumentChunkAdmin(admin.ModelAdmin):
    list_display = ('id', 'business_document', 'chunk_index', 'page_number', 'char_count', 'created_at')
    search_fields = ('text',)
    list_filter = ('business_document',)

