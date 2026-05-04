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
        
# ─────────────────────────────────────────────────────────────
# Thresholds
# ─────────────────────────────────────────────────────────────
#
# text-embedding-3-small cosine similarity on domain-specific audit text:
#
#   ≥ 0.82  STRONG       — same concept, very close or exact wording
#   ≥ 0.70  PARTIAL      — same concept, paraphrased / reworded
#   ≥ 0.60  CONCEPT_PASS — same concept, different specific values
#                          (e.g. "unsystematic risk premium 5%" vs "...10.5%")
#                          Only used in the concept re-rank pass, not the
#                          raw query pass, so noise stays filtered out.
#   < 0.60  NOISE        — different topic entirely
#
DB_STRONG_THRESHOLD  = 0.82
DB_PARTIAL_THRESHOLD = 0.70
DB_CONCEPT_THRESHOLD = 0.60   # used in concept re-rank pass only
DB_MAX_RESULTS       = 20     # raised: concept pass may find many valid variants
 
DOC_SIMILARITY_THRESHOLD = 0.50
 
 
# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
 
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
 
 
def get_queries_from_request(data) -> list:
    """
    Extracts queries without splitting on commas or single newlines.
 
    A single query may contain multiple paragraphs (separated by \\n\\n or \\n).
    We must never split WITHIN a query — only BETWEEN separate queries.
 
    Rules:
      - Repeated form-data keys (recommended): each key = one complete query.
        Paragraphs inside a single key value are preserved as-is.
      - Single string fallback: split on double-newline only.
    """
    if hasattr(data, "getlist"):
        values = data.getlist("queries")
        if values:
            return [v.strip() for v in values if v.strip()]
 
    value = data.get("queries")
    if not value:
        return []
    if isinstance(value, list):
        return [str(q).strip() for q in value if str(q).strip()]
    if isinstance(value, str):
        import re
        parts = re.split(r'\n{2,}', value)
        return [q.strip() for q in parts if q.strip()]
    return []
 
 
def _extract_core_concept(query: str) -> str:
    """
    Uses the LLM to extract the core semantic concept from a query.
 
    This replaces the old regex-based approach which only worked for
    numeric variations (e.g. "5%" vs "10%") and failed on all other
    query types.
 
    The LLM understands what the question is actually asking about and
    produces a concept phrase that captures meaning independent of:
      - specific numeric values
      - question phrasing / sentence structure
      - filler words and hedging language
 
    Examples:
      "Why have you selected an unsystematic risk premium of 5%?"
      → "unsystematic risk premium selection rationale justification"
 
      "Please provide your reasoning for excluding Ideal Power in the
       Asset Volatility calculation."
      → "asset volatility calculation exclusion criteria reasoning"
 
      "In the footnote on the IPO value, it is noted that it is based on
       an implied value based on the original issuance price of Series H1
       of 84.57 at 12.5%. However, should this be adjusted to assume some
       type of rate of return in an IPO exit for the Series H-1?"
      → "IPO exit value adjustment preferred stock rate of return
         liquidation preference Series H"
 
    Falls back to the original query if the LLM call fails, so the
    first pass still runs correctly.
    """
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    "You are a semantic search assistant for an audit database.\n\n"
                    "Extract the core concept from the following audit query. "
                    "Output only 5–10 keywords or a short phrase that captures "
                    "the essential meaning — what topic, method, or issue the "
                    "question is fundamentally about. Remove specific numeric "
                    "values, percentages, company names, and question phrasing. "
                    "Output only the concept phrase, nothing else.\n\n"
                    f"Query: {query}"
                )
            }],
            temperature=0.0,
            max_tokens=60,
        )
        concept = resp.choices[0].message.content.strip()
        return concept if concept else query
    except Exception as e:
        print(f"[Concept extraction error] {e}")
        return query
 
 
# ─────────────────────────────────────────────────────────────
# DB search — two-pass concept-aware search
# ─────────────────────────────────────────────────────────────
 
def search_db_records(query_embedding: list, query_text: str) -> dict:
    """
    Two-pass search for maximum recall on same-concept variations:
 
    Pass 1 — Raw query embedding (threshold 0.70):
        Catches exact and near-exact matches. Same concept, same or similar phrasing.
 
    Pass 2 — Core concept embedding (threshold 0.60):
        Strips specific numbers and filler words, re-embeds, searches again.
        Catches same-concept records with different numeric values.
        e.g. "unsystematic risk premium 5%" finds "...3%" "...8%" "...10.5%"
 
    Results from both passes are merged, deduplicated by serial_no, and
    sorted best-score-first. Hard cap of DB_MAX_RESULTS applied after merge.
 
    Returns {"strong": [...], "partial": [...]} where:
        strong  = score >= DB_STRONG_THRESHOLD (0.82)
        partial = score in [DB_CONCEPT_THRESHOLD, DB_STRONG_THRESHOLD) (0.60–0.82)
    """
    records_with_emb = list(
        AuditRecord.objects.exclude(question_embedding__isnull=True)
        .values("question_embedding", "serial_no", "type", "project", "auditor", "question", "response")
    )
    records_without_emb = list(
        AuditRecord.objects.filter(question_embedding__isnull=True)
        .values("id", "serial_no", "type", "project", "auditor", "question", "response")
    )
 
    # Cache embeddings for records missing them (should be empty post-backfill)
    if records_without_emb:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_embed, r["question"]): r for r in records_without_emb}
            for future in as_completed(futures):
                r = futures[future]
                try:
                    emb = future.result()
                    AuditRecord.objects.filter(id=r["id"]).update(question_embedding=emb)
                    records_with_emb.append({
                        "question_embedding": emb,
                        "serial_no": r["serial_no"],
                        "type":      r["type"],
                        "project":   r["project"],
                        "auditor":   r["auditor"],
                        "question":  r["question"],
                        "response":  r["response"],
                    })
                except Exception as e:
                    print(f"[DB fallback embed error] {e}")
 
    # ── Pass 1: raw query embedding (threshold = DB_PARTIAL_THRESHOLD 0.70) ──
    pass1_hits = {}   # serial_no → record dict
    for r in records_with_emb:
        score = cosine_similarity(query_embedding, r["question_embedding"])
        if score >= DB_PARTIAL_THRESHOLD:
            sno = r["serial_no"]
            if sno not in pass1_hits or score > pass1_hits[sno]["score"]:
                pass1_hits[sno] = {
                    "score":     score,
                    "serial_no": sno,
                    "type":      r["type"],
                    "project":   r["project"],
                    "auditor":   r["auditor"],
                    "question":  r["question"],
                    "response":  r["response"],
                }
 
    # ── Pass 2: concept embedding (threshold = DB_CONCEPT_THRESHOLD 0.60) ────
    # Only run if concept differs meaningfully from original query
    pass2_hits = {}
    core_concept = _extract_core_concept(query_text)
    if core_concept and core_concept.lower() != query_text.lower():
        try:
            concept_embedding = _embed(core_concept)
            for r in records_with_emb:
                sno = r["serial_no"]
                if sno in pass1_hits:
                    continue   # already captured in pass 1, skip
                score = cosine_similarity(concept_embedding, r["question_embedding"])
                if score >= DB_CONCEPT_THRESHOLD:
                    if sno not in pass2_hits or score > pass2_hits[sno]["score"]:
                        pass2_hits[sno] = {
                            "score":     score,
                            "serial_no": sno,
                            "type":      r["type"],
                            "project":   r["project"],
                            "auditor":   r["auditor"],
                            "question":  r["question"],
                            "response":  r["response"],
                        }
        except Exception as e:
            print(f"[Concept embed error] {e}")
 
    # ── Merge, sort, cap ──────────────────────────────────────────────────────
    all_hits = list(pass1_hits.values()) + list(pass2_hits.values())
    all_hits.sort(key=lambda x: x["score"], reverse=True)
    all_hits = all_hits[:DB_MAX_RESULTS]
 
    strong  = [h for h in all_hits if h["score"] >= DB_STRONG_THRESHOLD]
    partial = [h for h in all_hits if h["score"] < DB_STRONG_THRESHOLD]
 
    return {"strong": strong, "partial": partial}
 
 
# ─────────────────────────────────────────────────────────────
# Document chunk search — scoped to current job only
# FIX: Pass job_id so we ONLY search chunks uploaded in this request.
#      Chunks from previous requests are completely invisible.
# ─────────────────────────────────────────────────────────────
 
def get_relevant_chunks(query_embedding: list, job_id: str, top_k: int = 5, threshold: float = DOC_SIMILARITY_THRESHOLD):
    """
    Searches ONLY the DocumentChunk rows that belong to this specific job_id.
    This prevents chunks from previous requests from leaking into new queries.
    """
    chunks = DocumentChunk.objects.filter(
        status="completed",
        job_id=job_id          # ← scoped to this request only
    )[:2000]
 
    if not chunks:
        return []
 
    query_vec = np.array(query_embedding)
    scored = []
 
    for chunk in chunks:
        chunk_vec = np.array(chunk.embedding)
        denom = np.linalg.norm(query_vec) * np.linalg.norm(chunk_vec)
        if denom == 0:
            continue
        score = float(np.dot(query_vec, chunk_vec) / denom)
        if score >= threshold:
            scored.append((score, chunk.content))
 
    scored.sort(key=lambda x: x[0], reverse=True)
    return [content for _, content in scored[:top_k]]
 
 
# ─────────────────────────────────────────────────────────────
# File processing — stores job_id on every chunk
# ─────────────────────────────────────────────────────────────
 
def process_files_for_job(job_id: str, file_texts: list):
    """
    Embeds and stores chunks tagged with job_id.
    Note: content_hash dedup is intentionally removed here so the same
    file uploaded in two different requests each gets its own scoped chunks.
    If storage is a concern, dedup can be re-added at query time instead.
    """
    try:
        ProcessingJob.objects.filter(job_id=job_id).update(current_phase="processing_files")
        job = ProcessingJob.objects.get(job_id=job_id)
 
        for file_name, text in file_texts:
            job_file = ProcessingJobFile.objects.get(job=job, file_name=file_name)
            job_file.status = "processing"
            job_file.save(update_fields=["status"])
 
            ProcessingJob.objects.filter(job_id=job_id).update(current_file=file_name)
 
            chunks = chunk_text(text, chunk_size=1000, overlap=200)
            job_file.total_chunks = len(chunks)
            job_file.save(update_fields=["total_chunks"])
            ProcessingJob.objects.filter(job_id=job_id).update(
                total_chunks=models.F("total_chunks") + len(chunks)
            )
 
            for chunk in chunks:
                try:
                    emb = client.embeddings.create(
                        model="text-embedding-3-small", input=chunk
                    )
                    content_hash = hashlib.sha256(chunk.encode()).hexdigest()
 
                    # Store chunk tagged with job_id — no global dedup
                    DocumentChunk.objects.create(
                        content=chunk,
                        embedding=emb.data[0].embedding,
                        status="completed",
                        content_hash=content_hash,
                        source=file_name,
                        job_id=job_id,           # ← tag with job
                    )
 
                    ProcessingJobFile.objects.filter(id=job_file.id).update(
                        processed_chunks=models.F("processed_chunks") + 1
                    )
                    ProcessingJob.objects.filter(job_id=job_id).update(
                        processed_chunks=models.F("processed_chunks") + 1
                    )
                except Exception as e:
                    print(f"Embedding error [{file_name}]: {e}")
 
            job_file.status = "completed"
            job_file.save(update_fields=["status"])
            ProcessingJob.objects.filter(job_id=job_id).update(
                processed_files=models.F("processed_files") + 1
            )
 
        ProcessingJob.objects.filter(job_id=job_id).update(current_file=None)
 
    except Exception as e:
        ProcessingJob.objects.filter(job_id=job_id).update(
            status=ProcessingJob.STATUS_FAILED,
            error_message=str(e),
        )
        raise
 
 
# ─────────────────────────────────────────────────────────────
# Cleanup helper — delete chunks for a completed job
# Call this if you want to keep the DB lean after a job finishes.
# ─────────────────────────────────────────────────────────────
 
def cleanup_job_chunks(job_id: str):
    deleted, _ = DocumentChunk.objects.filter(job_id=job_id).delete()
    print(f"[cleanup] Deleted {deleted} chunks for job {job_id}")
 
 
# ─────────────────────────────────────────────────────────────
# Notes helper
# ─────────────────────────────────────────────────────────────
 
def _prepare_notes(notes: str) -> str:
    if not notes:
        return ""
    if len(notes) <= 15000:
        return notes
    note_chunks = chunk_text(notes, chunk_size=12000, overlap=500)
    summaries   = []
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
            summaries.append(r.choices[0].message.content)
        except Exception as e:
            print(f"Note summarization error: {e}")
    return "\n\n".join(summaries)
 
 
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
        queries = get_queries_from_request(request.data)
        notes   = request.data.get("notes", "")
        files   = request.FILES.getlist("files")
 
        if not queries:
            return Response({"error": "At least one query is required."}, status=400)
 
        file_texts = []
        for f in files:
            text = extract_file_text(f)
            if text:
                file_texts.append((f.name, text))
 
        # files_uploaded_this_request is the single source of truth.
        # If False, document search is completely skipped — no DB lookup at all.
        files_uploaded_this_request = len(file_texts) > 0
 
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
            yield _sse("job_created", {"job_id": job_id, "total_queries": len(queries)})
 
            # ── Process files synchronously BEFORE answering queries ──────────
            # Only runs if files were attached in THIS request.
            if files_uploaded_this_request:
                yield _sse("phase", {
                    "phase": "processing_files",
                    "message": f"Embedding {len(file_texts)} file(s)…",
                })
                try:
                    process_files_for_job(job_id, file_texts)
                    yield _sse("phase", {
                        "phase": "files_ready",
                        "message": "Files processed and ready for search.",
                    })
                except Exception as e:
                    yield _sse("error", {"message": f"File processing failed: {e}"})
                    # Don't abort — continue with DB + notes only
 
            # ── Answer queries ────────────────────────────────────────────────
            ProcessingJob.objects.filter(job_id=job_id).update(current_phase="answering_queries")
            yield _sse("phase", {
                "phase": "answering_queries",
                "message": f"Answering {len(queries)} query/queries…",
            })
 
            note_context = _prepare_notes(notes)
 
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
 
                try:
                    query_embedding = _embed(query)
                except Exception as e:
                    yield _sse("error", {"message": f"Embedding failed for query {idx+1}: {e}"})
                    yield _sse("query_done", {"index": idx + 1})
                    continue
 
                # ── Step 1: DB search (tiered, multi-match) ───────────────────
                db_results = {"strong": [], "partial": []}
                try:
                    db_results = search_db_records(query_embedding, query)
                except Exception as e:
                    print(f"DB search error: {e}")
 
                all_db_matches = db_results["strong"] + db_results["partial"]
                # strong matches first (already sorted by score), then partial
                # Deduplicate by question text in case of overlap
                seen_questions = set()
                deduped_matches = []
                for m in all_db_matches:
                    if m["question"] not in seen_questions:
                        seen_questions.add(m["question"])
                        deduped_matches.append(m)
 
                # ── Step 2: Document search — ONLY if files in this request ───
                top_chunks = []
                if files_uploaded_this_request:
                    try:
                        top_chunks = get_relevant_chunks(query_embedding, job_id=job_id)
                    except Exception as e:
                        print(f"Chunk search error: {e}")
 
                doc_context_text = "\n\n".join(
                    f"{i+1}. {chunk}" for i, chunk in enumerate(top_chunks)
                ) if top_chunks else ""
 
                # ══════════════════════════════════════════════════════════════
                # RESPONSE STRUCTURE — always two parts:
                #   Part 1: All DB matches (if any), each as a structured event
                #   Part 2: AI Generated answer (always, unconditionally)
                # ══════════════════════════════════════════════════════════════
 
                # ── Part 1: Emit each DB match as its own structured event ────
                # Frontend receives a separate "db_match" event per record,
                # each containing s_no, question, answer, source label.
                # No LLM involved — raw DB data, fully traceable.
                if deduped_matches:
                    yield _sse("db_matches_start", {
                        "total": len(deduped_matches),
                        "query_index": idx + 1,
                    })
                    for match in deduped_matches:
                        yield _sse("db_match", {
                            "s_no":     match["serial_no"],
                            "type":     match["type"],
                            "project":  match["project"],
                            "auditor":  match["auditor"],
                            "question": match["question"],
                            "answer":   match["response"],
                            "score":    round(match["score"], 4),
                            "source":   "Database",
                        })
                    yield _sse("db_matches_end", {"query_index": idx + 1})
 
                # ── Part 2: AI Generated answer — always produced ─────────────
                # Uses DB matches + document + notes as context.
                # Labelled "AI Generated" regardless of what sources exist.
                yield _sse("ai_answer_start", {"query_index": idx + 1})
 
                # Build context block for the AI prompt from DB matches
                db_context_for_prompt = ""
                if deduped_matches:
                    db_context_for_prompt = "\n\n".join(
                        f"S.No {m['serial_no']} | Score: {m['score']:.2f}\n"
                        f"Q: {m['question']}\n"
                        f"A: {m['response']}"
                        for m in deduped_matches[:10]   # cap at 10 for prompt size
                    )
 
                ai_prompt = f"""You are a senior audit professional with Big Four standards (Deloitte, PwC, EY, KPMG).
 
Your task is to write a single, well-structured paragraph that provides an audit-defensible answer to the question below.
 
STRICT REQUIREMENTS:
- Write exactly ONE well-structured paragraph. No bullet points. No numbered lists. No sub-headings.
- The answer must be fact-based, clear, and logically structured.
- It must provide traceability and logical justification for any conclusions.
- It must align with risk mitigation and professional audit requirements.
- It must be suitable for professional audit review at Big Four standards.
- Do NOT hallucinate. If a fact is uncertain, qualify it explicitly.
- If relevant database entries or document context are provided below, incorporate
  the most pertinent information into your answer naturally within the paragraph.
- If no relevant context exists, answer from industry-standard audit knowledge.
 
QUESTION:
{query}
 
DATABASE ENTRIES (for reference — incorporate relevant facts if applicable):
{db_context_for_prompt or "(none)"}
 
DOCUMENT CONTEXT (for reference — use if relevant):
{doc_context_text or "(no document uploaded)"}
 
NOTES (tone/format guidance and supplementary context):
{note_context or "(none provided)"}
 
Write one audit-defensible paragraph now:"""
 
                try:
                    ai_response = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": ai_prompt}],
                        temperature=0.3,
                        stream=True,
                    )
                    for chunk in ai_response:
                        delta = chunk.choices[0].delta.content
                        if delta:
                            yield _sse("ai_answer_chunk", {"text": delta})
                except Exception as e:
                    yield _sse("error", {"message": f"AI generation error for query {idx+1}: {str(e)}"})
 
                yield _sse("ai_answer_end", {
                    "query_index": idx + 1,
                    "source": "AI Generated",
                })
 
                yield _sse("query_done", {"index": idx + 1})
                ProcessingJob.objects.filter(job_id=job_id).update(processed_queries=idx + 1)
 
            # ── Cleanup this job's chunks from DB (keep DB lean) ──────────────
            if files_uploaded_this_request:
                try:
                    cleanup_job_chunks(job_id)
                except Exception as e:
                    print(f"Chunk cleanup error: {e}")
 
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
            "total_chunks":        total,
            "processed_chunks":    completed,
            "pending_chunks":      total - completed,
            "progress_percentage": progress,
            "files":               file_list,
        })