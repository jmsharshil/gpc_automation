from django.urls import path
from .views import ExcelUploadAPIView, DashboardSummaryAPIView, GeographyListAPIView, TransactionScreeningAPIView

urlpatterns = [
    path("upload/", ExcelUploadAPIView.as_view()),
    path("dashboard/", DashboardSummaryAPIView.as_view()),
    path("screening/", TransactionScreeningAPIView.as_view()),
    path("geographies/", GeographyListAPIView.as_view(), name="geography-list"),
]