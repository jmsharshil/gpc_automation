from django.urls import path
from . import views

urlpatterns = [
    path('admin-panel/', views.AdminPanelView.as_view(), name='admin-panel'),
    path('admin-panel/analytics/', views.AdminAnalyticsView.as_view(), name='admin-panel'),
    path('feedback/', views.WorkflowFeedbackView.as_view(),name='feed-back'),
    path('client-master/',views.ClientMasterView.as_view(),name='client-master'),
    path('client-project-session/', views.ClientProjectSessionView.as_view(), name='client-project-session'),
]