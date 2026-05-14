from rest_framework import serializers
from .models import Company, FinancialRecord, ProjectDates
from django.conf import settings

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
            'market_cap', 'total_revenue', 'enterprise_value', 'ebitda', 'ev_revenu', 'ev_ebitda',
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
            'records', 'first_pricing_date'
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
    
MAX_COMPANIES = getattr(settings, "COMPARE_MAX_COMPANIES", 10)    
class AdhocCompanySerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)

class CompareRequestSerializer(serializers.Serializer):
    compare_description = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    companies = AdhocCompanySerializer(many=True, required=False)

    def validate_companies(self, value):
        if len(value) > MAX_COMPANIES:
            raise serializers.ValidationError(
                f"Too many companies. Max allowed is {MAX_COMPANIES}."
            )
        return value

class ProjectDatesSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectDates
        fields = '__all__'