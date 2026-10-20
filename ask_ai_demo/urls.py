# from django.urls import path
# from .views import (
# ChatListCreateAPIView, ChatRetrieveAPIView,
# MessageListAPIView, SendMessageAPIView, DeleteChatAPIView, UserOpenAISettingAPIView, EditAndResendAPIView, ExportLatestAssistantMessageAPIView, ChatNameListAPIView
# )

# urlpatterns = [
# path('chats/', ChatListCreateAPIView.as_view(), name='chat-list-create'),
# path('chats/<int:pk>/', ChatRetrieveAPIView.as_view(), name='chat-retrieve'),
# path('chats/<int:chat_pk>/messages/', MessageListAPIView.as_view(), name='message-list'),
# path('chats/<int:chat_pk>/messages/send/', SendMessageAPIView.as_view(), name='message-send'),
# path('chats/<int:chat_pk>/messages/<int:message_pk>/edit-and-resend/', EditAndResendAPIView.as_view(), name='edit-and-resend'),
# path('chats/<int:chat_pk>/delete/', DeleteChatAPIView.as_view(), name='chat-delete'),
# # path('chats/delete-bulk/', BulkDeleteChatsAPIView.as_view(), name='chat-delete-bulk'),
# path('user/openai-settings/', UserOpenAISettingAPIView.as_view(), name='user-openai-settings'),
# path('chats/<int:chat_pk>/export/', ExportLatestAssistantMessageAPIView.as_view(), name='export-latest-assistant-message'),
# path('chats/names/', ChatNameListAPIView.as_view(), name='chat-name-list'),
# ]

from django.urls import path
from .views import (
    ChatListCreateAPIView, ChatRetrieveAPIView,
    MessageListAPIView, SendMessageAPIView, DeleteChatAPIView,
    UserOpenAISettingAPIView, StreamingChatAPIView, ChatNameListAPIView, EditAndResendAPIView, ClearChatSessionAPIView, UpdateGlobalOpenAISettingsAPIView,
    BusinessContextMemoryAPIView, BusinessDocumentDeleteAPIView
)

urlpatterns = [
    path('chats/', ChatListCreateAPIView.as_view(), name='chat-list-create'),
    path('chats/<int:pk>/', ChatRetrieveAPIView.as_view(), name='chat-retrieve'),
    path('chats/<int:chat_pk>/messages/', MessageListAPIView.as_view(), name='message-list'),
    path('chats/<int:chat_pk>/messages/send/', SendMessageAPIView.as_view(), name='message-send'),
    path('chats/<int:chat_pk>/messages/stream/', StreamingChatAPIView.as_view(), name='message-stream'),
    path('chats/<int:chat_pk>/messages/<int:message_pk>/edit-and-resend/', EditAndResendAPIView.as_view(), name='edit-and-resend'),
    # path('chats/<int:chat_pk>/document-status/', DocumentProcessingStatusAPIView.as_view()),
    path('chats/<int:chat_pk>/delete/', DeleteChatAPIView.as_view(), name='chat-delete'),
    # path('chats/delete-bulk/', BulkDeleteChatsAPIView.as_view(), name='chat-delete-bulk'),
    path('user/openai-settings/', UserOpenAISettingAPIView.as_view(), name='user-openai-settings'),
    path('business-memory/', BusinessContextMemoryAPIView.as_view(), name='business-context-memory'),
    path('business-memory/documents/<int:document_id>/', BusinessDocumentDeleteAPIView.as_view(), name='business-document-delete'),
    path('chats/names/', ChatNameListAPIView.as_view(), name='chat-name-list'),
    path('chats/<int:chat_pk>/clear/', ClearChatSessionAPIView.as_view(), name='chat-clear'),
    path('admin/openai-settings/global/', UpdateGlobalOpenAISettingsAPIView.as_view(), name='global-openai-settings'),
]

