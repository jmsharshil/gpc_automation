# from django.contrib import admin
# from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
# from .models import User

# @admin.register(User)
# class UserAdmin(BaseUserAdmin):
#     fieldsets = BaseUserAdmin.fieldsets + (
#         ("MSAL", {"fields": ("msal_oid", "msal_email", "role")}),
#     )
#     list_display = ("username", "email", "role", "msal_oid", "is_staff", "is_active")
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    model = User

    # Fields shown in the user list page
    list_display = (
        'username',
        'email',
        'role',
        'is_microsoft_user',
        'is_staff',
        'is_active',
    )

    list_filter = (
        'role',
        'is_microsoft_user',
        'is_staff',
        'is_active',
    )

    search_fields = (
        'username',
        'email',
        'microsoft_id',
    )

    ordering = ('username',)

    # Fields shown on the user detail/edit page
    fieldsets = UserAdmin.fieldsets + (
        (
            'Custom Fields',
            {
                'fields': (
                    'role',
                    'microsoft_id',
                    'profile_picture',
                    'is_microsoft_user',
                )
            },
        ),
    )

    # Fields shown when creating a new user
    add_fieldsets = UserAdmin.add_fieldsets + (
        (
            'Custom Fields',
            {
                'fields': (
                    'role',
                    'microsoft_id',
                    'profile_picture',
                    'is_microsoft_user',
                )
            },
        ),
    )
