from django.urls import path
from .views import ExcelUploadAPIView, DashboardSummaryAPIView, CountryListAPIView, TransactionScreeningAPIView

urlpatterns = [
    path("upload/", ExcelUploadAPIView.as_view()),
    path("dashboard/", DashboardSummaryAPIView.as_view()),
    path("screening/", TransactionScreeningAPIView.as_view()),
    path("geographies/", CountryListAPIView.as_view(), name="geography-list"),
]