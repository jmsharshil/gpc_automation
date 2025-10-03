from django.contrib import admin
from .models import Chat, Message, UserOpenAISetting




@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'owner', 'created_at', 'updated_at')
    search_fields = ('title', 'owner__username')




@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'chat', 'role', 'created_at')
    search_fields = ('content',)




@admin.register(UserOpenAISetting)
class UserOpenAISettingAdmin(admin.ModelAdmin):
    list_display = ('user', 'default_model', 'temperature', 'max_tokens')