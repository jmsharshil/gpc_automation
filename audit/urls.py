from django.urls import path
from .views import UploadExcelView, DashboardStatsView, FilterRecordsView, RunAIScreenStreamView, ProcessingStatusView

urlpatterns = [
    path('upload-excel/', UploadExcelView.as_view(), name='upload-excel'),
    path('dashboard/', DashboardStatsView.as_view(), name='dashboard-stats'),
    path('filter-records/', FilterRecordsView.as_view(), name='filter-records'),
    path('run-ai-stream/', RunAIScreenStreamView.as_view(), name='run-ai-stream'),
    path('processing-status/', ProcessingStatusView.as_view()),
]