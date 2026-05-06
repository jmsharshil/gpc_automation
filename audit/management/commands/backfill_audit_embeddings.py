# management/commands/backfill_audit_embeddings.py
#
# Run once after migration:
#   python manage.py backfill_audit_embeddings
#
# Safe to re-run:
#   - Skips records where BOTH question_embedding and response_embedding exist.
#   - Re-processes records where either field is still null.

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand
from openai import OpenAI
from django.db import models

from audit.models import AuditRecord

client  = OpenAI()
BATCH   = 50    # records per OpenAI batch call (max 2048 inputs per call)
WORKERS = 5     # parallel batches


def _embed_texts(texts: list) -> list:
    """Embed a list of texts in a single API call. Returns list of vectors."""
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=texts,
    )
    return [item.embedding for item in response.data]


class Command(BaseCommand):
    help = (
        "Backfill question_embedding and response_embedding for all "
        "AuditRecord rows that are missing either field."
    )

    def handle(self, *args, **options):
        # Records missing at least one embedding
        qs = AuditRecord.objects.filter(
            models.Q(question_embedding__isnull=True) |
            models.Q(response_embedding__isnull=True)
        )
        total = qs.count()

        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                "All records already have both embeddings. Nothing to do."
            ))
            return

        self.stdout.write(f"Backfilling embeddings for {total} records...")

        records = list(qs)
        batches = [records[i:i + BATCH] for i in range(0, len(records), BATCH)]
        done = 0
        failed = 0

        def process_batch(batch):
            # Embed questions and responses in two separate batch calls
            # (keeps them independent — response text can be very long)
            q_texts = [r.question for r in batch]
            r_texts = [r.response for r in batch]

            q_embeddings = _embed_texts(q_texts)
            r_embeddings = _embed_texts(r_texts)

            updates = []
            for record, q_emb, r_emb in zip(batch, q_embeddings, r_embeddings):
                record.question_embedding = q_emb
                record.response_embedding = r_emb
                updates.append(record)
            return updates

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            future_to_batch = {pool.submit(process_batch, b): b for b in batches}
            for future in as_completed(future_to_batch):
                try:
                    updates = future.result()
                    AuditRecord.objects.bulk_update(
                        updates, ["question_embedding", "response_embedding"]
                    )
                    done += len(updates)
                    self.stdout.write(f"  ✓ {done}/{total} embedded")
                except Exception as e:
                    failed += len(future_to_batch[future])
                    self.stdout.write(self.style.ERROR(f"  ✗ Batch failed: {e}"))
                    time.sleep(1)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {done} records embedded successfully, {failed} failed."
        ))