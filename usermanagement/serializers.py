from rest_framework import serializers
from .models import ClientMaster


class ClientMasterSerializer(serializers.ModelSerializer):

    class Meta:
        model = ClientMaster
        fields = "__all__"