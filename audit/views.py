from django.db import models
import pandas as pd
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .models import AuditRecord, DocumentChunk, ProcessingJob, ProcessingJobFile
from django.db.models import Count
import json
from openai import OpenAI
from django.conf import settings
from concurrent.futures import ThreadPoolExecutor, as_completed
from .utils.file_parser import extract_file_text, chunk_text
from .utils.ai_helper import get_top_chunks, cosine_similarity
from django.http import StreamingHttpResponse
import hashlib
import logging
import numpy as np
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

client = OpenAI(api_key=settings.OPENAI_API_KEY)

class UploadExcelView(APIView):
    def post(self, request, *args, **kwargs):
        file = request.FILES.get('file')

        if not file:
            return Response({"error": "No file provided"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Read Excel file
            df = pd.read_excel(file)

            # Normalize column names (remove spaces etc.)
            df.columns = [col.strip() for col in df.columns]

            required_columns = [
                "S.NO",
                "TYPE",
                "Classification",
                "Project",
                "Auditor",
                "Questions",
                "Responses"
            ]

            # Validate columns
            for col in required_columns:
                if col not in df.columns:
                    return Response({"error": f"Missing column: {col}"}, status=status.HTTP_400_BAD_REQUEST)

            records = []
            for _, row in df.iterrows():
                record = AuditRecord(
                    serial_no=row.get("S.NO"),
                    type=row.get("TYPE"),
                    classification=row.get("Classification"),
                    project=row.get("Project"),
                    auditor=row.get("Auditor"),
                    question=row.get("Questions"),
                    response=row.get("Responses"),
                )
                records.append(record)

            # Bulk insert (fast)
            AuditRecord.objects.bulk_create(records)

            return Response({
                "message": "File uploaded successfully",
                "records_inserted": len(records)
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
class DashboardStatsView(APIView):
    def get(self, request, *args, **kwargs):

        queryset = AuditRecord.objects.all()

        # 🔹 Total Questions
        total_questions = queryset.count()

        # 🔹 Type-wise count
        type_counts = (
            queryset
            .values('type')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        total_types = type_counts.count()
        
        auditor_counts = (
            queryset
            .values('auditor')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        
        total_auditors = auditor_counts.count()

        # 🔹 Category + Type mapping (MAIN LOGIC 🔥)
        category_type_data = (
            queryset
            .values('classification', 'type')
            .annotate(count=Count('id'))
        )

        # Transform into required structure
        category_map = {}

        for item in category_type_data:
            classification = item['classification']
            type_name = item['type']
            count = item['count']

            if classification not in category_map:
                category_map[classification] = {
                    "classification": classification,
                    "total_count": 0,
                    "types": []
                }

            category_map[classification]["types"].append({
                "type": type_name,
                "count": count
            })

            category_map[classification]["total_count"] += count

        category_breakdown = list(category_map.values())
        total_categories = len(category_breakdown)

        return Response({
            "total_questions": total_questions,

            "total_types": total_types,
            "type_breakdown": list(type_counts),

            "total_auditors": total_auditors,
            "auditor_breakdown": list(auditor_counts),

            "total_categories": total_categories,
            "category_breakdown": category_breakdown,
        })
        
DB_SIMILARITY_THRESHOLD  = 0.85   # min score to count as a DB match
DOC_SIMILARITY_THRESHOLD = 0.75   # min score for a document chunk to be "relevant"
#   ↑ This is the key fix for the wrong SOURCE:DOCUMENT bug.
#     Chunks below 0.75 are ignored even if they exist.
 
# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
 
def get_list(data, key):
    value = data.get(key)
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []
 
 
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
 
 
def _embed(text: str) -> list:
    return client.embeddings.create(
        model="text-embedding-3-small",
        input=text
    ).data[0].embedding
 
 
def cosine_similarity(a, b):
    a, b = np.array(a), np.array(b)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom else 0.0
 
 
# ─────────────────────────────────────────────────────────────
# FAST DB search  — uses pre-stored embeddings (no N API calls)
# ─────────────────────────────────────────────────────────────
 
def find_similar_db_answer(query_embedding: list, threshold: float = DB_SIMILARITY_THRESHOLD):
    """
    Searches ALL AuditRecord rows using pre-stored question_embedding.
    Cost: 0 extra API calls (query is already embedded by the caller).
    Falls back to live embedding only for records that are missing the cached vector.
    """
    # Load only records that have pre-stored embeddings (fast path)
    records_with_emb = list(
        AuditRecord.objects.exclude(question_embedding__isnull=True)
        .values("question_embedding", "response")
    )
 
    # Also load records without embeddings (slow fallback — should be empty after backfill)
    records_without_emb = list(
        AuditRecord.objects.filter(question_embedding__isnull=True)
        .values("id", "question", "response")
    )
 
    best_score  = 0.0
    best_answer = None
 
    # Fast path: compare against pre-stored vectors (pure numpy, no API calls)
    for r in records_with_emb:
        score = cosine_similarity(query_embedding, r["question_embedding"])
        if score > threshold and score > best_score:
            best_score  = score
            best_answer = r["response"]
 
    # Slow fallback: embed on the fly for records without stored vectors
    if records_without_emb:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(_embed, r["question"]): r
                for r in records_without_emb
            }
            for future in as_completed(futures):
                r = futures[future]
                try:
                    emb   = future.result()
                    score = cosine_similarity(query_embedding, emb)
                    if score > threshold and score > best_score:
                        best_score  = score
                        best_answer = r["response"]
 
                    # Cache the embedding so next call is fast
                    AuditRecord.objects.filter(id=r["id"]).update(
                        question_embedding=emb
                    )
                except Exception as e:
                    print(f"[DB fallback embed error] {e}")
 
    return best_answer
 
 
# ─────────────────────────────────────────────────────────────
# Document chunk search  — with relevance threshold
# ─────────────────────────────────────────────────────────────
 
def get_relevant_chunks(query_embedding: list, top_k: int = 5, threshold: float = DOC_SIMILARITY_THRESHOLD):
    """
    Returns top_k document chunks whose similarity to the query
    is >= threshold.  Returns empty list if nothing is relevant enough.
    This prevents false SOURCE:DOCUMENT labels.
    """
    chunks = DocumentChunk.objects.filter(status="completed")[:2000]
    query_vec = np.array(query_embedding)
 
    scored = []
    for chunk in chunks:
        chunk_vec = np.array(chunk.embedding)
        denom = np.linalg.norm(query_vec) * np.linalg.norm(chunk_vec)
        if denom == 0:
            continue
        score = float(np.dot(query_vec, chunk_vec) / denom)
        if score >= threshold:                    # ← only keep relevant chunks
            scored.append((score, chunk.content))
 
    scored.sort(key=lambda x: x[0], reverse=True)
    return [content for _, content in scored[:top_k]]
 
 
# ─────────────────────────────────────────────────────────────
# File processing (background, job-aware)
# ─────────────────────────────────────────────────────────────
 
def process_files_for_job(job_id: str, file_texts: list):
    try:
        job = ProcessingJob.objects.get(job_id=job_id)
        job.current_phase = "processing_files"
        job.save(update_fields=["current_phase", "updated_at"])
 
        for file_name, text in file_texts:
            job_file = ProcessingJobFile.objects.get(job=job, file_name=file_name)
            job_file.status = "processing"
            job_file.save(update_fields=["status"])
 
            job.current_file = file_name
            job.save(update_fields=["current_file", "updated_at"])
 
            chunks = chunk_text(text, chunk_size=1000, overlap=200)
            job_file.total_chunks = len(chunks)
            job_file.save(update_fields=["total_chunks"])
 
            ProcessingJob.objects.filter(job_id=job_id).update(
                total_chunks=models.F("total_chunks") + len(chunks)
            )
 
            for chunk in chunks:
                try:
                    content_hash = hashlib.sha256(chunk.encode()).hexdigest()
 
                    if not DocumentChunk.objects.filter(content_hash=content_hash).exists():
                        emb = client.embeddings.create(
                            model="text-embedding-3-small",
                            input=chunk
                        )
                        DocumentChunk.objects.create(
                            content=chunk,
                            embedding=emb.data[0].embedding,
                            status="completed",
                            content_hash=content_hash,
                            source=file_name,
                        )
 
                    ProcessingJobFile.objects.filter(id=job_file.id).update(
                        processed_chunks=models.F("processed_chunks") + 1
                    )
                    ProcessingJob.objects.filter(job_id=job_id).update(
                        processed_chunks=models.F("processed_chunks") + 1
                    )
 
                except Exception as e:
                    print(f"Embedding error [{file_name}]: {e}")
 
            job_file.refresh_from_db()
            job_file.status = "completed"
            job_file.save(update_fields=["status"])
 
            ProcessingJob.objects.filter(job_id=job_id).update(
                processed_files=models.F("processed_files") + 1
            )
 
        job.refresh_from_db()
        job.current_file = None
        job.save(update_fields=["current_file", "updated_at"])
 
    except Exception as e:
        ProcessingJob.objects.filter(job_id=job_id).update(
            status=ProcessingJob.STATUS_FAILED,
            error_message=str(e),
        )
        raise
 
 
_executor = ThreadPoolExecutor(max_workers=4)
 
 
# ─────────────────────────────────────────────────────────────
# Filter Records
# ─────────────────────────────────────────────────────────────
 
class FilterRecordsView(APIView):
    def get(self, request, *args, **kwargs):
        queryset = AuditRecord.objects.all()
 
        types           = request.GET.get("type")
        classifications = request.GET.get("classification")
        auditors        = request.GET.get("auditor")
 
        if types:
            queryset = queryset.filter(type__in=[t.strip() for t in types.split(",")])
        if classifications:
            queryset = queryset.filter(classification__in=[c.strip() for c in classifications.split(",")])
        if auditors:
            queryset = queryset.filter(auditor__in=[a.strip() for a in auditors.split(",")])
 
        return Response({
            "total_count": queryset.count(),
            "results": list(queryset.values("id", "type", "classification", "project", "auditor", "question", "response"))
        })
 
 
# ─────────────────────────────────────────────────────────────
# Main AI Stream View
# ─────────────────────────────────────────────────────────────
 
class RunAIScreenStreamView(APIView):
    def post(self, request):
        queries = get_list(request.data, "queries")
        notes   = request.data.get("notes", "")
        files   = request.FILES.getlist("files")
 
        if not queries:
            return Response({"error": "At least one query is required."}, status=400)
 
        # Extract file texts upfront
        file_texts = []
        for f in files:
            text = extract_file_text(f)
            if text:
                file_texts.append((f.name, text))
 
        # Create job
        job = ProcessingJob.objects.create(
            status=ProcessingJob.STATUS_PROCESSING,
            total_files=len(file_texts),
            total_queries=len(queries),
            current_phase="starting",
        )
        for file_name, _ in file_texts:
            ProcessingJobFile.objects.create(job=job, file_name=file_name)
 
        job_id = str(job.job_id)
 
        def generate():
            yield _sse("job_created", {"job_id": job_id})
 
            # Start background file processing
            file_future = None
            if file_texts:
                yield _sse("phase", {
                    "phase": "processing_files",
                    "message": f"Processing {len(file_texts)} file(s) in background…",
                })
                file_future = _executor.submit(process_files_for_job, job_id, file_texts)
 
            # Answer queries immediately (don't wait for file processing)
            ProcessingJob.objects.filter(job_id=job_id).update(current_phase="answering_queries")
            yield _sse("phase", {
                "phase": "answering_queries",
                "message": f"Answering {len(queries)} query/queries…",
            })
 
            for idx, query in enumerate(queries):
                ProcessingJob.objects.filter(job_id=job_id).update(
                    current_query=query,
                    processed_queries=idx,
                )
                yield _sse("query_start", {
                    "query": query,
                    "index": idx + 1,
                    "total": len(queries),
                })
 
                # ── Embed the query ONCE — reused for DB search + doc search ──
                try:
                    query_embedding = _embed(query)
                except Exception as e:
                    yield _sse("error", {"message": f"Embedding failed for query {idx+1}: {e}"})
                    yield _sse("query_done", {"index": idx + 1})
                    continue
 
                # ── Step 1: Full DB search (fast — pre-stored vectors) ─────────
                db_answer = None
                try:
                    db_answer = find_similar_db_answer(query_embedding)
                except Exception as e:
                    print(f"DB search error: {e}")
 
                if db_answer:
                    yield _sse("query_source", {"source": "DATABASE"})
                    yield _sse("query_chunk",  {"text": db_answer})
                    yield _sse("query_done",   {"index": idx + 1})
                    ProcessingJob.objects.filter(job_id=job_id).update(processed_queries=idx + 1)
                    continue
 
                # ── Step 2: Document search (with relevance threshold) ─────────
                top_chunks = []
                try:
                    top_chunks = get_relevant_chunks(query_embedding)
                except Exception as e:
                    print(f"Chunk search error: {e}")
 
                # ← Only mark as DOCUMENT if chunks are genuinely relevant (score ≥ 0.75)
                has_relevant_context = len(top_chunks) > 0
                context_text = "\n\n".join(
                    f"{i+1}. {chunk}" for i, chunk in enumerate(top_chunks)
                ) if has_relevant_context else ""
 
                # ── Step 3: Handle large notes ────────────────────────────────
                note_context = ""
                if notes:
                    if len(notes) <= 15000:
                        note_context = notes
                    else:
                        note_chunks   = chunk_text(notes, chunk_size=12000, overlap=500)
                        note_summaries = []
                        for nc in note_chunks:
                            try:
                                r = client.chat.completions.create(
                                    model="gpt-4o-mini",
                                    messages=[{"role": "user", "content":
                                        f"Summarize the following audit notes concisely, retaining all key facts:\n\n{nc}"
                                    }],
                                    temperature=0.2,
                                    max_tokens=1000,
                                )
                                note_summaries.append(r.choices[0].message.content)
                            except Exception as e:
                                print(f"Note summarization error: {e}")
                        note_context = "\n\n".join(note_summaries)
 
                # ── Determine source label ─────────────────────────────────────
                if has_relevant_context:
                    source = "DOCUMENT"
                elif note_context:
                    source = "NOTES"
                else:
                    source = "LLM_FALLBACK"
 
                yield _sse("query_source", {"source": source})
 
                prompt = f"""You are a professional audit assistant. Follow this STRICT priority order:
 
1. DOCUMENT CONTEXT — use it if relevant and sufficient.
2. NOTES — use if they provide useful audit-related information.
3. INDUSTRY STANDARD — if neither contains sufficient information, provide a professional, industry-standard audit answer. Clearly state you are doing so.
 
QUESTION:
{query}
 
NOTES:
{note_context or "(none provided)"}
 
DOCUMENT CONTEXT:
{context_text or "(no relevant document content found)"}
 
INSTRUCTIONS:
- Be precise, professional, and thorough.
- Do NOT hallucinate. If unsure, say so explicitly.
- If using document/notes context, cite what you are basing the answer on.
- If falling back to industry standards, start your answer with: "Based on industry-standard audit practices:"
"""
 
                try:
                    response = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3,
                        stream=True,
                    )
                    for chunk in response:
                        delta = chunk.choices[0].delta.content
                        if delta:
                            yield _sse("query_chunk", {"text": delta})
                except Exception as e:
                    yield _sse("error", {"message": f"LLM error for query {idx+1}: {str(e)}"})
 
                yield _sse("query_done",   {"index": idx + 1})
                ProcessingJob.objects.filter(job_id=job_id).update(processed_queries=idx + 1)
 
            # Wait for file processing to finish
            if file_future:
                yield _sse("phase", {
                    "phase": "waiting_for_files",
                    "message": "Waiting for background file processing to finish…",
                })
                try:
                    file_future.result()
                except Exception as e:
                    yield _sse("error", {"message": f"File processing failed: {str(e)}"})
 
            ProcessingJob.objects.filter(job_id=job_id).update(
                status=ProcessingJob.STATUS_COMPLETED,
                current_phase="done",
                current_query=None,
                current_file=None,
            )
            yield _sse("job_done", {"job_id": job_id})
 
        return StreamingHttpResponse(generate(), content_type="text/event-stream")
 
 
# ─────────────────────────────────────────────────────────────
# Processing Status View
# ─────────────────────────────────────────────────────────────
 
class ProcessingStatusView(APIView):
    def get(self, request):
        job_id = request.GET.get("job_id")
        return self._job_status(job_id) if job_id else self._global_status()
 
    def _job_status(self, job_id):
        try:
            job = ProcessingJob.objects.get(job_id=job_id)
        except ProcessingJob.DoesNotExist:
            return Response({"error": "Job not found."}, status=404)
 
        files = [{
            "file_name":         jf.file_name,
            "status":            jf.status,
            "total_chunks":      jf.total_chunks,
            "processed_chunks":  jf.processed_chunks,
            "progress":          jf.progress(),
            "error":             jf.error,
        } for jf in job.files.all()]
 
        return Response({
            "job_id":                    str(job.job_id),
            "status":                    job.status,
            "current_phase":             job.current_phase,
            "total_files":               job.total_files,
            "processed_files":           job.processed_files,
            "total_chunks":              job.total_chunks,
            "processed_chunks":          job.processed_chunks,
            "file_progress_percentage":  job.file_progress(),
            "current_file":              job.current_file,
            "files":                     files,
            "total_queries":             job.total_queries,
            "processed_queries":         job.processed_queries,
            "query_progress_percentage": job.query_progress(),
            "current_query":             job.current_query,
            "error_message":             job.error_message,
            "created_at":                job.created_at,
            "updated_at":                job.updated_at,
        })
 
    def _global_status(self):
        from django.db.models import Count
 
        total     = DocumentChunk.objects.count()
        completed = DocumentChunk.objects.filter(status="completed").count()
        progress  = round((completed / total) * 100, 2) if total > 0 else 0
 
        file_stats = (
            DocumentChunk.objects
            .values("source")
            .annotate(
                total_chunks=Count("id"),
                completed_chunks=Count("id", filter=models.Q(status="completed")),
            )
        )
 
        file_list = [{
            "file_name":        f["source"],
            "total_chunks":     f["total_chunks"],
            "processed_chunks": f["completed_chunks"],
            "progress":         round((f["completed_chunks"] / f["total_chunks"]) * 100, 2)
                                if f["total_chunks"] > 0 else 0,
        } for f in file_stats]
 
        return Response({
            "total_chunks":       total,
            "processed_chunks":   completed,
            "pending_chunks":     total - completed,
            "progress_percentage": progress,
            "files":              file_list,
        })