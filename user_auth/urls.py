from django.urls import path
from . import views

urlpatterns = [
    path('microsoft/login/', views.microsoft_login, name='microsoft_login'),
    path('microsoft/callback/', views.microsoft_callback, name='microsoft_callback'),
    path('microsoft/callback-json/', views.microsoft_callback_json, name='microsoft_callback_json'),
    path('microsoft/mobile/', views.microsoft_login_mobile, name='microsoft_login_mobile'),
    path('profile/', views.user_profile, name='user_profile'),
    path('logout/', views.logout, name='logout'),
    path('users/', views.UserListView.as_view(), name='user-list'),
    path('users/<int:user_id>/change-role/', views.ChangeUserRoleView.as_view(), name='change-user-role'),    
    # path('debug/', views.debug_config, name='debug_config'),
]