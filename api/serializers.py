from rest_framework import serializers
from .models import Company, FinancialRecord


class GroupCountSerializer(serializers.Serializer):
    name = serializers.CharField()
    company_count = serializers.IntegerField()


class DashboardSummarySerializer(serializers.Serializer):
    total_companies = serializers.IntegerField()
    total_countries = serializers.IntegerField()
    total_sectors = serializers.IntegerField()
    total_industries = serializers.IntegerField()



class FinancialRecordSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)
    company_id = serializers.CharField(source='company.company_id', read_only=True)

    class Meta:
        model = FinancialRecord
        fields = [
            'id', 'company', 'company_id', 'company_name', 'period',
            'market_cap', 'total_revenue', 'enterprise_value', 'ebitda', 'ev_revenu',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'company_name', 'company_id']


class CompanySerializer(serializers.ModelSerializer):
    records = FinancialRecordSerializer(many=True, read_only=True)
    latest_financial = serializers.SerializerMethodField()
    class Meta:
        model = Company
        fields = [
            'id', 'company_id', 'name',
            'exchange_ticker', 'primary_sector', 'primary_industry',
            'headquarters_country_region', 'website', 'business_description',
            'industry_classifications', 'country','latest_financial',
            'records'
        ]
        read_only_fields = [
            'id', 'records'
        ]
        
    def get_latest_financial(self, obj):
        # `obj._latest_record` is set in the view for efficiency if available
        record = getattr(obj, '_latest_record', None)
        if record is None:
            # fallback: attempt to get one (will hit DB if not prefetched)
            record = obj.records.filter(period='latest').first()
        if not record:
            return None
        return FinancialRecordSerializer(record).data


