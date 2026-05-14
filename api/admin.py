from django.contrib import admin
from .models import Company, FinancialRecord, UploadJob, ProjectDates


class FinancialRecordInline(admin.TabularInline):
    model = FinancialRecord
    extra = 0
    readonly_fields = ("created_at",)
    fields = (
        "period",
        "market_cap",
        "total_revenue",
        "enterprise_value",
        "ebitda",
        "ev_revenu",
        "ev_ebitda",
        "created_at",
    )


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = (
        "company_id",
        "name",
        "exchange_ticker",
        "primary_sector",
        "primary_industry",
        "country",
        "first_pricing_date",
    )
    search_fields = (
        "company_id",
        "name",
        "exchange_ticker",
        "primary_sector",
        "primary_industry",
        "country",
    )
    list_filter = (
        "primary_sector",
        "primary_industry",
        "country",
        "first_pricing_date",
    )
    ordering = ("name",)
    inlines = [FinancialRecordInline]


@admin.register(FinancialRecord)
class FinancialRecordAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "period",
        "market_cap",
        "total_revenue",
        "enterprise_value",
        "ebitda",
        "created_at",
    )
    list_filter = ("period", "created_at")
    search_fields = ("company__name", "company__company_id")
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(UploadJob)
class UploadJobAdmin(admin.ModelAdmin):
    list_display = (
        "filename",
        "uploaded_by",
        "uploaded_at",
    )
    list_filter = ("uploaded_at", "uploaded_by")
    search_fields = ("filename", "uploaded_by__username")
    readonly_fields = ("uploaded_at",)

@admin.register(ProjectDates)
class ProjectDatesAdmin(admin.ModelAdmin):
    list_display = (
        'gpc_date',
        'transaction_date',
        'audit_date',
        'updated_at',
    )

    search_fields = (
        'gpc_date',
        'transaction_date',
        'audit_date',
    )