from django.urls import path
from .views import (GuideListCreateAPIView, GuideDetailAPIView, GuideReprocessAPIView,ValuationSessionListCreateAPIView, ValuationSessionDetailAPIView,ValuationMessageListAPIView, AskQuestionAPIView,AskQuestionStreamAPIView, EditMessageAPIView, UpdateSessionGuidesAPIView,DeleteSessionAPIView, DeleteGuideAPIView,)

urlpatterns = [
    # ── Guides ──────────────────────────────────────────
    path('guides/', GuideListCreateAPIView.as_view(), name='guide-list-create'),
    path('guides/<int:pk>/', GuideDetailAPIView.as_view(), name='guide-detail'),
    path('guides/<int:pk>/reprocess/', GuideReprocessAPIView.as_view(), name='guide-reprocess'),
    path('guides/<int:pk>/delete/', DeleteGuideAPIView.as_view(), name='guide-delete'),

    # ── Sessions ─────────────────────────────────────────
    path('chats/', ValuationSessionListCreateAPIView.as_view(), name='session-list-create'),
    path('chats/<int:pk>/', ValuationSessionDetailAPIView.as_view(), name='session-detail'),
    path('chats/<int:pk>/delete/', DeleteSessionAPIView.as_view(), name='session-delete'),
    path('chats/<int:session_pk>/guides/', UpdateSessionGuidesAPIView.as_view(), name='session-guides-update'),

    # ── Messages ─────────────────────────────────────────
    path('chats/<int:session_pk>/messages/', ValuationMessageListAPIView.as_view(), name='message-list'),
    path('chats/<int:session_pk>/messages/<int:message_pk>/edit/', EditMessageAPIView.as_view(), name='message-edit'),

    # ── Ask (Q&A) ────────────────────────────────────────
    path('chats/<int:session_pk>/ask/', AskQuestionAPIView.as_view(), name='ask'),
    path('chats/<int:session_pk>/ask/stream/', AskQuestionStreamAPIView.as_view(), name='ask-stream'),
    
]
