# finance/urls.py
from django.urls import path
from .views import ExcelUploadAPIView, DashboardSummaryAPIView, CountryListAPIView, SectorListAPIView, IndustryListAPIView, CompanyListAPIView, CompareAPIView, ProjectDatesAPIView

urlpatterns = [
    path('upload-excel/', ExcelUploadAPIView.as_view(), name='upload-excel'),
    
    path('dashboard/summary/', DashboardSummaryAPIView.as_view(), name='dashboard-summary'),
    path('dashboard/countries/', CountryListAPIView.as_view(), name='dashboard-countries'),
    path('dashboard/sectors/', SectorListAPIView.as_view(), name='dashboard-sectors'),
    path('dashboard/industries/', IndustryListAPIView.as_view(), name='dashboard-industries'),
    
    path('api/companies/', CompanyListAPIView.as_view(), name='companies-list'),
    
    path('api/companies/compare/', CompareAPIView.as_view(), name='companies-compare'),
    
    path('project-dates/', ProjectDatesAPIView.as_view(), name='project-dates'),
    
    # path('companies/', CompanyListAPIView.as_view(), name='company-list'),
    # path('companies/<int:pk>/', CompanyDetailAPIView.as_view(), name='company-detail'),
    # path('records/', FinancialRecordListAPIView.as_view(), name='record-list'),
    
    # optional drilldown
    # path('dashboard/countries/<str:country_name>/companies/', CompaniesByCountryAPIView.as_view(), name='companies-by-country'),
    # path('dashboard/sectors/<str:sector_name>/companies/', CompaniesBySectorAPIView.as_view(), name='companies-by-sector'),
    # path('dashboard/industries/<str:industry_name>/companies/', CompaniesByIndustryAPIView.as_view(), name='companies-by-industry'),
]
