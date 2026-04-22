from django.contrib import admin
from .models import Transaction, UploadJob


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "transaction_id",
        "target_issuer",
        "announced_date",
        "ma_closed_date",
        "total_transaction_value_usd",
        "geography",
        "primary_industry",
        "created_at",
        "target_stock_premium_1d",
        "target_stock_premium_1w",
        "target_stock_premium_1m",
    )

    list_filter = (
        "announced_date",
        "ma_closed_date",
        "geography",
        "primary_industry",
        "deal_attitude",
        "accounting_method",
    )

    search_fields = (
        "transaction_id",
        "target_issuer",
        "buyers_investors",
        "sellers",
        "ciq_transaction_id",
        "primary_industry",
        "geography",
    )

    readonly_fields = ("created_at",)

    ordering = ("-created_at",)

    fieldsets = (
        ("Basic Info", {
            "fields": (
                "transaction_id",
                "target_issuer",
                "announced_date",
                "ma_closed_date",
                "geography",
            )
        }),
        ("Transaction Values", {
            "fields": (
                "total_transaction_value_inr",
                "total_transaction_value_usd",
                "implied_ev_usd",
                "percent_sought",
                "target_stock_premium_1d",
                "target_stock_premium_1w",
                "target_stock_premium_1m",
            )
        }),
        ("Parties", {
            "fields": (
                "buyers_investors",
                "sellers",
            )
        }),
        ("Financials", {
            "fields": (
                "ev_revenue",
                "ev_ebitda",
                "target_revenue",
                "target_ebitda",
                "acquirer_revenue",
                "acquirer_ebitda",
            )
        }),
        ("Details", {
            "fields": (
                "primary_industry",
                "business_description",
                "consideration_offered",
                "target_security_type",
                "accounting_method",
                "deal_attitude",
                "comments",
            )
        }),
        ("External References", {
            "fields": (
                "ciq_transaction_id",
            )
        }),
        ("Metadata", {
            "fields": ("created_at",),
        }),
    )


@admin.register(UploadJob)
class UploadJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "uploaded_by",
        "filename",
        "uploaded_at",
    )

    list_filter = (
        "uploaded_at",
        "uploaded_by",
    )

    search_fields = (
        "filename",
        "uploaded_by__username",
        "uploaded_by__email",
    )

    readonly_fields = (
        "uploaded_at",
        "summary",
    )

    ordering = ("-uploaded_at",)