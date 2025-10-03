from django.shortcuts import get_object_or_404
from rest_framework import generics, status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from django.conf import settings


from .models import Chat, Message, UserOpenAISetting
from .serializers import ChatSerializer, MessageSerializer, UserOpenAISettingSerializer
from .permissions import IsOwner


import openai


openai.api_key = getattr(settings, 'OPENAI_API_KEY', None)




class ChatListCreateAPIView(generics.ListCreateAPIView):
    serializer_class = ChatSerializer
    permission_classes = [permissions.IsAuthenticated]


    def get_queryset(self):
        return Chat.objects.filter(owner=self.request.user).order_by('-updated_at')


    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)




class ChatRetrieveAPIView(generics.RetrieveAPIView):
    serializer_class = ChatSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    queryset = Chat.objects.all()
    
class MessageListAPIView(generics.ListAPIView):
    serializer_class = MessageSerializer
    permission_classes = [permissions.IsAuthenticated]


    def get_queryset(self):
        chat_id = self.kwargs['chat_pk']
        chat = get_object_or_404(Chat, pk=chat_id, owner=self.request.user)
        return chat.messages.all().order_by('created_at')
    
class SendMessageAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_pk):
        user = request.user
        chat = get_object_or_404(Chat, pk=chat_pk, owner=user)

        user_text = request.data.get('content', '').strip()
        if not user_text:
            return Response({'error': 'content is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Save user message
        user_msg = Message.objects.create(chat=chat, role='user', content=user_text)

        # Prepare messages for OpenAI
        system_prompt = chat.system_prompt or ''
        messages_payload = []

        if system_prompt:
            messages_payload.append({'role': 'system', 'content': system_prompt})

        recent_messages = chat.messages.all().order_by('-created_at')[:20][::-1]
        for m in recent_messages:
            messages_payload.append({'role': m.role, 'content': m.content})

        # Model + params
        user_setting = getattr(user, 'openai_setting', None)
        model = request.data.get('model') or (user_setting.default_model if user_setting else 'gpt-4o')
        temperature = float(request.data.get('temperature') or (user_setting.temperature if user_setting else 0.2))
        max_tokens = int(request.data.get('max_tokens') or (user_setting.max_tokens if user_setting else 1024))

        try:
            # ✅ NEW SYNTAX for openai>=1.0.0
            client = openai.OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
            resp = client.chat.completions.create(
                model=model,
                messages=messages_payload,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            assistant_text = resp.choices[0].message.content
            usage = resp.usage.model_dump() if hasattr(resp.usage, 'model_dump') else {}

            assistant_msg = Message.objects.create(
                chat=chat,
                role='assistant',
                content=assistant_text,
                metadata={'openai_usage': usage}
            )

            serializer = MessageSerializer(assistant_msg)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        
# Simple endpoint to update/get user OpenAI settings
class UserOpenAISettingAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]


    def get(self, request):
        setting, _ = UserOpenAISetting.objects.get_or_create(user=request.user)
        return Response(UserOpenAISettingSerializer(setting).data)


    def post(self, request):
        setting, _ = UserOpenAISetting.objects.get_or_create(user=request.user)
        serializer = UserOpenAISettingSerializer(setting, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)