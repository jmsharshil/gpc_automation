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
from .models import ChatDemo, MessageDemo, UserOpenAISettingDemo, DocumentChunkDemo, BusinessContextMemoryDemo, BusinessDocumentDemo


class MessageSerializer(serializers.ModelSerializer):
    attachment_url = serializers.SerializerMethodField()
    class Meta:
        model = MessageDemo
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
        model = ChatDemo
        fields = ('id', 'owner', 'title', 'system_prompt', 'created_at', 'updated_at', 'messages')
        read_only_fields = ('id', 'owner', 'created_at', 'updated_at')

class DocumentChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentChunkDemo
        fields = ('id', 'page_number', 'chunk_index', 'text', 'char_count')

class UserOpenAISettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserOpenAISettingDemo
        fields = (
            'default_model', 'temperature', 'max_tokens',
            'use_rag_for_documents', 'max_context_chunks',
            'context_memory', 'reasoning_effort',
        )

class BusinessDocumentSerializer(serializers.ModelSerializer):
    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True)
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = BusinessDocumentDemo
        fields = (
            'id', 'name', 'file', 'file_url', 'file_type',
            'total_pages', 'total_chunks',
            'uploaded_by', 'uploaded_by_username', 'created_at'
        )
        read_only_fields = ('id', 'file_url', 'uploaded_by', 'uploaded_by_username', 'created_at', 'total_pages', 'total_chunks')

    def get_file_url(self, obj):
        if obj.file and hasattr(obj.file, 'url'):
            request = self.context.get('request')
            return request.build_absolute_uri(obj.file.url) if request else obj.file.url
        return None


class BusinessContextMemorySerializer(serializers.ModelSerializer):
    updated_by_username = serializers.CharField(source='updated_by.username', read_only=True)
    documents = BusinessDocumentSerializer(many=True, read_only=True)

    class Meta:
        model = BusinessContextMemoryDemo
        fields = (
            'id', 'title', 'content', 'is_active',
            'updated_by', 'updated_by_username',
            'documents',
            'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'created_at', 'updated_at', 'updated_by', 'updated_by_username', 'documents')


class ChatNameSerializer(serializers.ModelSerializer):
    created_at = serializers.DateTimeField(format="%Y-%m-%d %H:%M:%S")
    updated_at = serializers.DateTimeField(format="%Y-%m-%d %H:%M:%S")
    
    class Meta:
        model = ChatDemo
        fields = ["id", "title", "created_at", "updated_at"]