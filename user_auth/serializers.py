from rest_framework import serializers
from .models import User

class UserListSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'first_name',
            'last_name',
            'email',
            'role',
            'is_active',
            'date_joined'
        ]


class ChangeUserRoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(
        choices=['admin', 'user']
    )