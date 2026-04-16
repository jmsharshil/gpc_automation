from django.db import models
import pandas as pd
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .models import AuditRecord, DocumentChunk
from django.db.models import Count
import json
from openai import OpenAI
from django.conf import settings
from concurrent.futures import ThreadPoolExecutor
from .utils.file_parser import extract_file_text, chunk_text
from .utils.ai_helper import get_top_chunks, cosine_similarity
from django.http import StreamingHttpResponse
import hashlib

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
        
class FilterRecordsView(APIView):
    def get(self, request, *args, **kwargs):

        queryset = AuditRecord.objects.all()

        # 🔹 Get query params
        types = request.GET.get('type')
        classifications = request.GET.get('classification')
        auditors = request.GET.get('auditor')

        # 🔹 Apply filters (multi-select supported)
        if types:
            type_list = [t.strip() for t in types.split(',')]
            queryset = queryset.filter(type__in=type_list)

        if classifications:
            classification_list = [c.strip() for c in classifications.split(',')]
            queryset = queryset.filter(classification__in=classification_list)

        if auditors:
            auditor_list = [a.strip() for a in auditors.split(',')]
            queryset = queryset.filter(auditor__in=auditor_list)

        # 🔹 Total count
        total_count = queryset.count()

        # 🔹 Data (you can paginate later if needed)
        data = list(
            queryset.values(
                'id',
                'type',
                'classification',
                'project',
                'auditor',
                'question',
                'response'
            )
        )

        return Response({
            "total_count": total_count,
            "results": data
        })
        
executor = ThreadPoolExecutor(max_workers=4)        
        
def get_list(data, key):
    value = data.get(key)

    if not value:
        return []

    if isinstance(value, list):
        return value

    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]

    return []

def process_files_background(file_texts):
    for file_name, text in file_texts:

        # ❌ REMOVE THIS:
        # text[:20000]

        chunks = chunk_text(text, 1000)

        for chunk in chunks:
            try:
                content_hash = hashlib.sha256(chunk.encode()).hexdigest()

                # ✅ avoid duplicate chunks
                if DocumentChunk.objects.filter(content_hash=content_hash).exists():
                    continue

                emb = client.embeddings.create(
                    model="text-embedding-3-small",
                    input=chunk
                )

                DocumentChunk.objects.create(
                    content=chunk,
                    embedding=emb.data[0].embedding,
                    status="completed",
                    content_hash=content_hash,
                    source=file_name
                )

            except Exception as e:
                print("Embedding error:", str(e))
                
def find_similar_db_answer(query, existing_data):
    if not existing_data:
        return None

    query_emb = client.embeddings.create(
        model="text-embedding-3-small",
        input=query
    ).data[0].embedding

    best_score = 0
    best_answer = None

    for item in existing_data:
        q_emb = client.embeddings.create(
            model="text-embedding-3-small",
            input=item["question"]
        ).data[0].embedding

        score = cosine_similarity(query_emb, q_emb)

        if score > 0.85:  # threshold
            if score > best_score:
                best_score = score
                best_answer = item["response"]

    return best_answer

class RunAIScreenStreamView(APIView):

    def post(self, request):

        queries = get_list(request.data, "queries")
        notes = request.data.get("notes", "")
        files = request.FILES.getlist("files")

        types = get_list(request.data, "type")
        classifications = get_list(request.data, "classification")
        auditors = get_list(request.data, "auditor")

        # 🔹 APPLY FILTERS
        queryset = AuditRecord.objects.all()

        if types:
            queryset = queryset.filter(type__in=types)

        if classifications:
            queryset = queryset.filter(classification__in=classifications)

        if auditors:
            queryset = queryset.filter(auditor__in=auditors)

        # 🔹 EXISTING DATA
        existing_data = list(
            queryset.values("question", "response")[:100]
        )

        # 🔹 PROCESS FILES IN BACKGROUND
        file_texts = []
        for f in files:
            text = extract_file_text(f)
            if text:
                file_texts.append((f.name, text))

        if file_texts:
            executor.submit(process_files_background, file_texts)

        def generate():

            for query in queries:

                yield f"\n\n===== QUERY: {query} =====\n\n"

                # ✅ STEP 1: DB CHECK
                db_answer = find_similar_db_answer(query, existing_data)

                if db_answer:
                    yield "SOURCE:DATABASE\n"
                    yield f"{db_answer}\n\n"
                    continue

                # ✅ STEP 2: DOCUMENT SEARCH
                emb = client.embeddings.create(
                    model="text-embedding-3-small",
                    input=query
                )

                query_embedding = emb.data[0].embedding
                top_chunks = get_top_chunks(query_embedding)

                has_context = len(top_chunks) > 0 and any(len(c.strip()) > 50 for c in top_chunks)

                context_text = "\n\n".join([
                    f"{i+1}. {chunk}" for i, chunk in enumerate(top_chunks)
                ])

                prompt = f"""
You are a professional audit assistant.

Follow STRICT priority:
1. Use database knowledge if relevant
2. Use document context
3. Use notes
4. If nothing found → give industry-standard answer

QUESTION:
{query}

NOTES:
{notes}

DOCUMENT CONTEXT:
{context_text}

INSTRUCTIONS:
- Be precise and professional
- Do not hallucinate
- If unsure, say clearly
"""

                # ✅ Mark source BEFORE streaming
                if has_context:
                    yield "SOURCE:DOCUMENT\n"
                else:
                    yield "SOURCE:LLM_FALLBACK\n"

                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    stream=True
                )

                for chunk in response:
                    if chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content

                yield "\n\n"

        return StreamingHttpResponse(generate(), content_type="text/plain")
        
class ProcessingStatusView(APIView):
    def get(self, request):

        total = DocumentChunk.objects.count()
        completed = DocumentChunk.objects.filter(status="completed").count()

        progress = round((completed / total) * 100, 2) if total > 0 else 0

        # ✅ group by file
        file_stats = (
            DocumentChunk.objects
            .values("source")
            .annotate(
                total_chunks=Count("id"),
                completed_chunks=Count("id", filter=models.Q(status="completed"))
            )
        )

        file_list = []

        for f in file_stats:
            file_progress = 0
            if f["total_chunks"] > 0:
                file_progress = round(
                    (f["completed_chunks"] / f["total_chunks"]) * 100, 2
                )

            file_list.append({
                "file_name": f["source"],
                "total_chunks": f["total_chunks"],
                "processed_chunks": f["completed_chunks"],
                "progress": file_progress
            })

        return Response({
            "total_chunks": total,
            "processed_chunks": completed,
            "pending_chunks": total - completed,
            "progress_percentage": progress,
            "files": file_list
        })