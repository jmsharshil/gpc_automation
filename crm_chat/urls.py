from django.urls import path
from .views import (
ChatListCreateAPIView, ChatRetrieveAPIView,
MessageListAPIView, SendMessageAPIView, UserOpenAISettingAPIView
)

urlpatterns = [
path('chats/', ChatListCreateAPIView.as_view(), name='chat-list-create'),
path('chats/<int:pk>/', ChatRetrieveAPIView.as_view(), name='chat-retrieve'),
path('chats/<int:chat_pk>/messages/', MessageListAPIView.as_view(), name='message-list'),
path('chats/<int:chat_pk>/messages/send/', SendMessageAPIView.as_view(), name='message-send'),
path('user/openai-settings/', UserOpenAISettingAPIView.as_view(), name='user-openai-settings'),
]