from rest_framework import serializers
from .models import Chat, Message, UserOpenAISetting




class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ('id', 'chat', 'role', 'content', 'tokens', 'openai_response_id', 'created_at', 'metadata')
        read_only_fields = ('id', 'tokens', 'openai_response_id', 'created_at', 'metadata')




class ChatSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)


    class Meta:
        model = Chat
        fields = ('id', 'owner', 'title', 'system_prompt', 'created_at', 'updated_at', 'messages')
        read_only_fields = ('id', 'owner', 'created_at', 'updated_at')




class UserOpenAISettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserOpenAISetting
        fields = ('default_model', 'temperature', 'max_tokens')