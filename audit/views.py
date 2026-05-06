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
 
 
def _generate_query_expansions(query: str) -> list[str]:
    """
    Uses the LLM to generate multiple semantically equivalent phrasings
    of the query — synonyms, related terms, domain variations.
 
    This solves the core problem: a short or specific query like
    "Please provide support for exit term" has a narrow embedding that
    misses semantically related DB records phrased as "exit scenario",
    "exit planning", "exit duration", "liquidity event term", etc.
 
    By generating N alternate phrasings and embedding each one separately,
    we cast a wider semantic net while staying on-topic. Each phrasing
    is a genuine restatement of the same underlying question — not noise.
 
    Returns a list of strings: [core_concept, phrasing_1, phrasing_2, ...]
    The first item is always the core concept (numbers/filler stripped).
    Subsequent items are domain-aware alternate phrasings.
 
    Falls back to [query] if the LLM call fails — Pass 1 still runs normally.
    """
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    "You are a semantic search assistant for a professional audit database.\n\n"
                    "Given the audit query below, generate 4 outputs:\n"
                    "1. Core concept: 5-8 keywords capturing the essential topic "
                    "(remove specific numbers, percentages, company names, question phrasing)\n"
                    "2. Alternate phrasing 1: rephrase the question using different but "
                    "semantically equivalent audit terminology\n"
                    "3. Alternate phrasing 2: rephrase again using related domain terms "
                    "or synonyms an auditor might use\n"
                    "4. Alternate phrasing 3: a broader version of the question that "
                    "captures related concepts\n\n"
                    "Output exactly 4 lines, one per item, no labels, no numbering, "
                    "no extra text.\n\n"
                    f"Query: {query}"
                )
            }],
            temperature=0.0,
            max_tokens=200,
        )
        lines = [
            line.strip()
            for line in resp.choices[0].message.content.strip().split("\n")
            if line.strip()
        ]
        # Return up to 4 expansions, always include at least the first line
        return lines[:4] if lines else [query]
    except Exception as e:
        print(f"[Query expansion error] {e}")
        return [query]
 
 
# ─────────────────────────────────────────────────────────────
# DB search — multi-pass query expansion search
# ─────────────────────────────────────────────────────────────
 
def search_db_records(query_embedding: list, query_text: str) -> dict:
    """
    Improved multi-pass semantic + hybrid search with:
    - question + response embedding matching
    - keyword boosting
    - dynamic thresholding
    - top-k fallback (prevents low result issue)
    - full backward compatibility
    """

    # ─────────────────────────────────────────────
    # Fetch records
    # ─────────────────────────────────────────────
    records_with_emb = list(
        AuditRecord.objects.exclude(
            question_embedding__isnull=True,
            response_embedding__isnull=True,
        )
        .values(
            "question_embedding", "response_embedding",
            "serial_no", "type", "project", "auditor", "question", "response",
        )
    )

    # ─────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────
    def normalize_query(q: str) -> str:
        return q.lower().replace("please provide", "").strip()

    def keyword_score(query: str, text: str) -> float:
        q_words = set(query.lower().split())
        t_words = set(text.lower().split())
        if not q_words:
            return 0.0
        return len(q_words & t_words) / len(q_words)

    normalized_query = normalize_query(query_text)

    # ─────────────────────────────────────────────
    # Scoring function
    # ─────────────────────────────────────────────
    def score_records(embedding: list):
        scored = []

        for r in records_with_emb:
            try:
                # Semantic similarity (question + response)
                q_score = cosine_similarity(embedding, r["question_embedding"])
                r_score = cosine_similarity(embedding, r["response_embedding"])
                semantic_score = max(q_score, r_score)

                # Keyword boost
                kw_score = keyword_score(normalized_query, r["question"])

                # Hybrid score
                final_score = (0.75 * semantic_score) + (0.25 * kw_score)

                scored.append((final_score, r))
            except Exception as e:
                print(f"[Scoring error] {e}")

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    # ─────────────────────────────────────────────
    # Pass 0 — Raw query
    # ─────────────────────────────────────────────
    scored_all = score_records(query_embedding)

    # ─────────────────────────────────────────────
    # Query expansion (safe fallback if fails)
    # ─────────────────────────────────────────────
    try:
        expansions = _generate_query_expansions(query_text)
    except Exception as e:
        print(f"[Expansion error] {e}")
        expansions = [query_text]

    # ─────────────────────────────────────────────
    # Expansion passes
    # ─────────────────────────────────────────────
    expansion_results = []

    def process_expansion(text):
        try:
            emb = _embed(text)
            return score_records(emb)
        except Exception as e:
            print(f"[Expansion scoring error] {e}")
            return []

    with ThreadPoolExecutor(max_workers=min(5, len(expansions))) as pool:
        futures = [pool.submit(process_expansion, exp) for exp in expansions]

        for future in as_completed(futures):
            try:
                expansion_results.extend(future.result())
            except Exception as e:
                print(f"[Expansion future error] {e}")

    # ─────────────────────────────────────────────
    # Merge all scores (keep best per serial_no)
    # ─────────────────────────────────────────────
    merged_scores = {}

    for score, r in scored_all + expansion_results:
        sno = r["serial_no"]

        if sno not in merged_scores or score > merged_scores[sno]["score"]:
            merged_scores[sno] = {
                "score":     score,
                "serial_no": sno,
                "type":      r["type"],
                "project":   r["project"],
                "auditor":   r["auditor"],
                "question":  r["question"],
                "response":  r["response"],
            }

    merged = list(merged_scores.values())
    merged.sort(key=lambda x: x["score"], reverse=True)

    # ─────────────────────────────────────────────
    # Thresholds (relaxed)
    # ─────────────────────────────────────────────
    STRONG_THRESHOLD  = 0.75
    PARTIAL_THRESHOLD = 0.55

    strong  = [m for m in merged if m["score"] >= STRONG_THRESHOLD]
    partial = [m for m in merged if PARTIAL_THRESHOLD <= m["score"] < STRONG_THRESHOLD]

    # ─────────────────────────────────────────────
    # 🔥 CRITICAL: Top-K fallback (prevents 1-result issue)
    # ─────────────────────────────────────────────
    MIN_RESULTS = 5
    MAX_RESULTS = DB_MAX_RESULTS  # keep your existing cap

    if len(strong) + len(partial) < MIN_RESULTS:
        fallback = merged[:MAX_RESULTS]

        strong  = fallback[:3]   # first few as strong
        partial = fallback[3:]

    else:
        strong  = strong[:MAX_RESULTS]
        partial = partial[:MAX_RESULTS]

    return {
        "strong": strong,
        "partial": partial,
    }
 
 
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
 
                ai_prompt = f"""You are a senior valuation and audit support professional operating at Big Four standards (Deloitte, PwC, EY, KPMG), with expertise in 409A valuations, purchase price allocations (ASC 805), business enterprise valuations, ASC 718, convertible instrument valuation, debt valuation, and estate and gift tax valuation.
 
Your task is to write a single, well-structured paragraph that provides an audit-defensible response to the question below. The response must be suitable for review by auditors, valuation specialists, tax advisors, and regulators.
 
STRICT REQUIREMENTS:
- Write exactly ONE well-structured paragraph. No bullet points, numbering, or sub-headings.
- The response must be fact-based, clear, and logically structured.
- Provide traceability and support for all conclusions, including assumptions and methodologies where relevant.
- Use appropriate professional valuation terminology and concepts.
- Explicitly state the valuation approach or framework where relevant (e.g., income, market, or cost approach) and justify its appropriateness.
- Clearly identify key assumptions (explicit or implicit) and ensure they are reasonable, supportable, and consistent with the context.
- Do not reach conclusions that extend beyond the available evidence; where information is insufficient, explicitly state the limitation and provide a conditional or alternative view.
- Use precise, measured language and avoid absolute statements unless fully supported.
- Ensure the response is written such that an independent reviewer can understand the rationale, assumptions, and conclusion without additional clarification.
- Align with applicable authoritative guidance (e.g., ASC 820, ASC 805, ASC 718, AICPA valuation guidance, IRS standards), applying the most relevant standard based on the context.
- Consider materiality where relevant and avoid overemphasis on immaterial factors.
- Maintain independence and avoid bias.
 
DATABASE ENTRIES:
{db_context_for_prompt or "(none)"}
 
DATABASE USAGE LIMITATION:
The database entries are provided only as reference examples of how similar questions were addressed in other projects. Use them strictly for understanding response structure, audit-defensible reasoning style, tone, and level of analytical rigor. Treat all such entries as originating from separate engagements and maintain strict professional independence. Do NOT copy, rely on, or incorporate any project-specific quantitative or qualitative information from these entries, including but not limited to assumptions, financial data, company-specific facts, valuation inputs, multiples, discounts, forecasts, risk factors, dates, or conclusions, unless such information is independently supported by the current question, document context, or notes.
 
DOCUMENT CONTEXT:
{doc_context_text or "(no document uploaded)"}
 
DOCUMENT USAGE:
If document context is provided, prioritize it as the primary source of factual support and ensure conclusions are directly traceable to it.
 
NOTES:
{note_context or "(none provided)"}
 
NOTES USAGE:
Incorporate any tone, emphasis, or specific instructions provided in the notes while maintaining professional audit standards.
 
QUESTION:
{query}
 
Write one audit-defensible valuation paragraph now:"""
 
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