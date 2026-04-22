# management/commands/backfill_audit_embeddings.py
#
# Run once after migration:
#   python manage.py backfill_audit_embeddings
#
# Safe to re-run — skips records that already have embeddings.

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand
from openai import OpenAI

from audit.models import AuditRecord

client = OpenAI()

BATCH_SIZE = 50       # records per batch (OpenAI allows up to 2048 inputs per call)
MAX_WORKERS = 5       # parallel batches


def embed_batch(records):
    """Embed a batch of AuditRecord objects in a single OpenAI API call."""
    texts = [r.question for r in records]
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=texts,
    )
    embeddings = [item.embedding for item in response.data]
    return list(zip(records, embeddings))


class Command(BaseCommand):
    help = "Backfill question_embedding for all AuditRecord rows that are missing it."

    def handle(self, *args, **options):
        qs = AuditRecord.objects.filter(question_embedding__isnull=True)
        total = qs.count()

        if total == 0:
            self.stdout.write(self.style.SUCCESS("All records already have embeddings. Nothing to do."))
            return

        self.stdout.write(f"Backfilling embeddings for {total} records...")

        records = list(qs)
        batches = [records[i:i + BATCH_SIZE] for i in range(0, len(records), BATCH_SIZE)]

        done = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_to_batch = {pool.submit(embed_batch, b): b for b in batches}

            for future in as_completed(future_to_batch):
                try:
                    pairs = future.result()
                    bulk_update = []
                    for record, embedding in pairs:
                        record.question_embedding = embedding
                        bulk_update.append(record)

                    AuditRecord.objects.bulk_update(bulk_update, ["question_embedding"])
                    done += len(bulk_update)
                    self.stdout.write(f"  ✓ {done}/{total} embedded")

                except Exception as e:
                    failed += len(future_to_batch[future])
                    self.stdout.write(self.style.ERROR(f"  ✗ Batch failed: {e}"))
                    time.sleep(1)  # back off briefly on error

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {done} records embedded, {failed} failed."
        ))