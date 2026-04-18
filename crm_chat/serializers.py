# from rest_framework import serializers
# from .models import Chat, Message, UserOpenAISetting


# class MessageSerializer(serializers.ModelSerializer):
#     attachment_url = serializers.SerializerMethodField()
#     class Meta:
#         model = Message
#         fields = ('id', 'chat', 'role', 'content', 'tokens', 'openai_response_id', 'created_at', 'metadata','attachment', 'attachment_name', 'attachment_content_type', 'attachment_url')
#         read_only_fields = ('id', 'tokens', 'openai_response_id', 'created_at', 'metadata', 'attachment_url')
#         extra_kwargs = {
#             'attachment': {'write_only': True, 'required': False}
#         }

#     def get_attachment_url(self, obj):
#         request = self.context.get('request')
#         if obj.attachment and hasattr(obj.attachment, 'url'):
#             url = obj.attachment.url  # Azure URL or SAS URL in prod; /media/... in dev
#             return request.build_absolute_uri(url) if request else url
#         return None

# class ChatSerializer(serializers.ModelSerializer):
#     messages = MessageSerializer(many=True, read_only=True)

#     class Meta:
#         model = Chat
#         fields = ('id', 'owner', 'title', 'system_prompt', 'created_at', 'updated_at', 'messages')
#         read_only_fields = ('id', 'owner', 'created_at', 'updated_at')

# class UserOpenAISettingSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = UserOpenAISetting
#         fields = ('default_model', 'temperature', 'max_tokens')
        
# class ChatNameSerializer(serializers.ModelSerializer):
#     created_at = serializers.DateTimeField(format="%Y-%m-%d %H:%M:%S")
#     updated_at = serializers.DateTimeField(format="%Y-%m-%d %H:%M:%S")
    
#     class Meta:
#         model = Chat
#         fields = ["id", "title", "created_at", "updated_at"]

from rest_framework import serializers
from .models import Chat, Message, UserOpenAISetting,DocumentChunk


class MessageSerializer(serializers.ModelSerializer):
    attachment_url = serializers.SerializerMethodField()
    class Meta:
        model = Message
        fields = ('id', 'chat', 'role', 'content', 'tokens', 'openai_response_id', 'created_at', 'metadata','attachment', 'attachment_name', 'attachment_content_type', 'attachment_url')
        read_only_fields = ('id', 'tokens', 'openai_response_id', 'created_at', 'metadata', 'attachment_url')
        extra_kwargs = {
            'attachment': {'write_only': True, 'required': False}
        }

    def get_attachment_url(self, obj):
        request = self.context.get('request')
        if obj.attachment and hasattr(obj.attachment, 'url'):
            url = obj.attachment.url  # Azure URL or SAS URL in prod; /media/... in dev
            return request.build_absolute_uri(url) if request else url
        return None

class ChatSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)

    class Meta:
        model = Chat
        fields = ('id', 'owner', 'title', 'system_prompt', 'created_at', 'updated_at', 'messages')
        read_only_fields = ('id', 'owner', 'created_at', 'updated_at')

class DocumentChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentChunk
        fields = ('id', 'page_number', 'chunk_index', 'text', 'char_count')

class UserOpenAISettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserOpenAISetting
        fields = ('default_model', 'temperature', 'max_tokens')

class UserOpenAISettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserOpenAISetting
        fields = ('default_model', 'temperature', 'max_tokens', 'use_rag_for_documents', 'max_context_chunks')