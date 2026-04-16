from rest_framework import serializers
from .models import Transaction


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = "__all__"


class DashboardSummarySerializer(serializers.Serializer):
    total_transactions = serializers.IntegerField()
    total_countries = serializers.IntegerField()
    total_industries = serializers.IntegerField()
    countries = serializers.ListField(child=serializers.CharField())
    industries = serializers.ListField(child=serializers.CharField())