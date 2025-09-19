# from django.contrib import admin
# from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
# from .models import User

# @admin.register(User)
# class UserAdmin(BaseUserAdmin):
#     fieldsets = BaseUserAdmin.fieldsets + (
#         ("MSAL", {"fields": ("msal_oid", "msal_email", "role")}),
#     )
#     list_display = ("username", "email", "role", "msal_oid", "is_staff", "is_active")
