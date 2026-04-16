from rest_framework import serializers
from .models import AuditRecord

class AuditRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditRecord
        fields = "__all__"