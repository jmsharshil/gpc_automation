from django.urls import path
from .views import ArticleExtractView, ArticleGlobalOpenAISettingView, ArticlesOpenAISettingView, ExtractionAuditView, ExtractionRecordDetailView, ExtractionRecordListView, ExtractionStatusView

urlpatterns = [
    path("extract/", ArticleExtractView.as_view(),name="extract"),
    path("extract/<int:pk>/status/", ExtractionStatusView.as_view(), name="extract-status"),
    path("openai-settings/", ArticlesOpenAISettingView.as_view(),  name="openai-settings"),
    path("global-openai-settings/", ArticleGlobalOpenAISettingView.as_view(), name="global-openai-settings"),
    path("extractions/", ExtractionRecordListView.as_view(), name="extraction-list"),
    path("extractions/<int:pk>/", ExtractionRecordDetailView.as_view(), name="extraction-detail"),
    path("extractions/<int:pk>/audit/", ExtractionAuditView.as_view(),name="extraction-audit"),
]
