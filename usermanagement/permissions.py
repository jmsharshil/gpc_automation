# authentication/permissions.py
from rest_framework import permissions

class IsAdminUser(permissions.BasePermission):
    """
    Permission class for admin users
    """
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.is_admin