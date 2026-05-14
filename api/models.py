from django.db import models
from django.conf import settings

class Company(models.Model):
    company_id = models.CharField(max_length=200, blank=True, null=True, unique=True)
    name = models.CharField(max_length=1024, unique=True)

    # descriptive fields (keep these)
    exchange_ticker = models.CharField(max_length=200, blank=True, null=True)
    primary_sector = models.CharField(max_length=256, blank=True, null=True)
    primary_industry = models.CharField(max_length=256, blank=True, null=True)
    headquarters_country_region = models.CharField(max_length=256, blank=True, null=True)
    website = models.CharField(max_length=512, blank=True, null=True)
    business_description = models.TextField(blank=True, null=True)
    industry_classifications = models.CharField(max_length=1024, blank=True, null=True)
    country = models.CharField(max_length=256, blank=True, null=True)
    first_pricing_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.company_id or self.name


class FinancialRecord(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='records')
    # keep a real period if you add one later; default 'latest' is fine for current workflow
    period = models.CharField(max_length=50, default='latest')

    market_cap = models.DecimalField(max_digits=30, decimal_places=6, blank=True, null=True)
    total_revenue = models.DecimalField(max_digits=30, decimal_places=6, blank=True, null=True)
    enterprise_value = models.DecimalField(max_digits=30, decimal_places=6, blank=True, null=True)
    ebitda = models.DecimalField(max_digits=30, decimal_places=6, blank=True, null=True)
    ev_revenu = models.DecimalField(max_digits=30, decimal_places=6, blank=True, null=True)
    ev_ebitda = models.DecimalField(max_digits=30, decimal_places=6, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'period')
        ordering = ['-created_at']
        
# in finance/models.py
class UploadJob(models.Model):
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    filename = models.CharField(max_length=255, blank=True)
    summary = models.JSONField(default=dict)
    file = models.FileField(upload_to='uploads/finance/', null=True, blank=True)

class ProjectDates(models.Model):
    gpc_date = models.CharField(max_length=100, null=True, blank=True)
    transaction_date = models.CharField(max_length=100, null=True, blank=True)
    audit_date = models.CharField(max_length=100, null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Project Dates"