# finance/management/commands/backfill_financial_records.py
from django.core.management.base import BaseCommand
from api.models import Company, FinancialRecord
from django.db import transaction

class Command(BaseCommand):
    help = "Backfill FinancialRecord rows from Company.latest_* snapshot fields."

    def handle(self, *args, **options):
        companies = Company.objects.all()
        created = 0
        skipped = 0
        with transaction.atomic():
            for c in companies:
                # detect if company already has a 'latest' record
                exists = FinancialRecord.objects.filter(company=c, period='latest').exists()
                if exists:
                    skipped += 1
                    continue

                # Try to collect numeric values from company fields (adjust names to your schema)
                try:
                    fr = FinancialRecord.objects.create(
                        company=c,
                        period='latest',
                        market_cap=getattr(c, 'latest_market_cap', None),
                        total_revenue=getattr(c, 'latest_total_revenue', None),
                        enterprise_value=getattr(c, 'latest_enterprise_value', None),
                        ebitda=getattr(c, 'latest_ebitda', None),
                        ev_revenu=getattr(c, 'latest_ev_revenu', None)
                    )
                    created += 1
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Failed for company {c.pk}: {e}"))
        self.stdout.write(self.style.SUCCESS(f"Created {created} records, skipped {skipped} (already exist)."))
