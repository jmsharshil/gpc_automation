# authentication/permissions.py
from rest_framework import permissions

class IsAdminUser(permissions.BasePermission):
    """
    Permission class for admin users
    """
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.is_admin

class IsRegularUser(permissions.BasePermission):
    """
    Permission class for regular users
    """
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.is_regular_user

class IsAdminOrOwner(permissions.BasePermission):
    """
    Permission class for admin users or object owners
    """
    def has_permission(self, request, view):
        return request.user.is_authenticated
    
    def has_object_permission(self, request, view, obj):
        return request.user.is_admin or obj.user == request.user