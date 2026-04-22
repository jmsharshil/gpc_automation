from django.db import models
from django.conf import settings


class Transaction(models.Model):
    transaction_id = models.CharField(max_length=255, unique=True, null=True, blank=True)

    announced_date = models.DateField(null=True, blank=True)
    target_issuer = models.CharField(max_length=1024, null=True, blank=True)

    total_transaction_value_inr = models.DecimalField(max_digits=30, decimal_places=6, null=True, blank=True)
    total_transaction_value_usd = models.DecimalField(max_digits=30, decimal_places=6, null=True, blank=True)

    buyers_investors = models.TextField(null=True, blank=True)
    sellers = models.TextField(null=True, blank=True)

    ciq_transaction_id = models.CharField(max_length=255, null=True, blank=True)
    ma_closed_date = models.DateField(null=True, blank=True)

    geography = models.CharField(max_length=512, null=True, blank=True)

    ev_revenue = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    ev_ebitda = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)

    percent_sought = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    target_stock_premium_1d = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    target_stock_premium_1w = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    target_stock_premium_1m = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    comments = models.TextField(null=True, blank=True)

    implied_ev_usd = models.DecimalField(max_digits=30, decimal_places=6, null=True, blank=True)

    business_description = models.TextField(null=True, blank=True)
    primary_industry = models.CharField(max_length=512, null=True, blank=True)

    target_revenue = models.DecimalField(max_digits=30, decimal_places=6, null=True, blank=True)
    target_ebitda = models.DecimalField(max_digits=30, decimal_places=6, null=True, blank=True)

    acquirer_revenue = models.DecimalField(max_digits=30, decimal_places=6, null=True, blank=True)
    acquirer_ebitda = models.DecimalField(max_digits=30, decimal_places=6, null=True, blank=True)

    consideration_offered = models.CharField(max_length=512, null=True, blank=True)
    target_security_type = models.CharField(max_length=512, null=True, blank=True)
    accounting_method = models.CharField(max_length=512, null=True, blank=True)
    deal_attitude = models.CharField(max_length=512, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.target_issuer or "Transaction"


class UploadJob(models.Model):
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='transaction_upload_jobs')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    filename = models.CharField(max_length=255, blank=True)
    summary = models.JSONField(default=dict)
    file = models.FileField(upload_to='uploads/transactions/', null=True, blank=True)