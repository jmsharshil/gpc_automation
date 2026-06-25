"""
article_interpretation/views.py

Endpoints:
  POST  /api/articles/extract/          — Upload file, receive EXTRACTION_SCHEMA JSON
  GET   /api/articles/openai-settings/  — Fetch current user's OpenAI settings
  PATCH /api/articles/openai-settings/  — Update current user's OpenAI settings
"""

import os
from pathlib import Path

from django.conf import settings
from rest_framework.views import APIView
import threading
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from concurrent.futures import ThreadPoolExecutor

from .models import ArticlesOpenAISetting, ArticleGlobalOpenAISetting, ExtractionRecord, SecurityFieldAudit
from .serializers import ArticlesOpenAISettingSerializer, ArticleGlobalOpenAISettingSerializer, ExtractionRecordListSerializer, ExtractionRecordDetailSerializer, SecurityFieldAuditSerializer, SecurityUpdateSerializer
from . import openai_extractor
from rest_framework import status, permissions
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from django.db import connection, close_old_connections



# Fallback defaults used when no per-user setting exists
# DEFAULT_MODEL     = "gpt-4.1"
# DEFAULT_MAX_CHARS = 120000
# DEFAULT_QUALITY_PASS = False

DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_QUALITY_REASONING_EFFORT = "high"
DEFAULT_MAX_CHARS = 120000
DEFAULT_MAX_OUTPUT_TOKENS = 8000

_executor_lock      = threading.Lock()
_executor_instance  = None
_EXECUTOR_MAX_WORKERS = 5

# ─────────────────────────────────────────────────────────────────────────────
# Settings endpoint
# ─────────────────────────────────────────────────────────────────────────────

def _get_executor() -> ThreadPoolExecutor:
    global _executor_instance
    # Fast path: executor exists and is still alive.
    if _executor_instance is not None and not _executor_instance._shutdown:
        return _executor_instance
    # Slow path: create (or recreate) under lock.
    with _executor_lock:
        if _executor_instance is None or _executor_instance._shutdown:
            _executor_instance = ThreadPoolExecutor(
                max_workers=_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="article_extract",
            )
        return _executor_instance


# ─────────────────────────────────────────────────────────────────────────────
# Background worker — runs inside the thread pool
# ─────────────────────────────────────────────────────────────────────────────

def _run_extraction_thread(
    record_id: int,
    file_name: str,
    file_bytes: bytes,
    model: str,
    api_key: str,
    max_chars: int,
    reasoning_effort: str,
    quality_reasoning_effort: str,
    max_output_tokens: int,
    quality_pass: bool,
    seniority_pass: bool,
):
    try:
        # ── Mark as PROCESSING ────────────────────────────────────────────────
        close_old_connections()
        ExtractionRecord.objects.filter(pk=record_id).update(
            status=ExtractionRecord.STATUS_CHOICES[1][0]
        )

        # ── Run the extraction (slow: up to 3 OpenAI API calls) ───────────────
        result = openai_extractor.run_extraction(
            file_name=file_name,
            file_bytes=file_bytes,
            model=model,
            api_key=api_key,
            max_chars=max_chars,
            reasoning_effort=reasoning_effort,
            quality_reasoning_effort=quality_reasoning_effort,
            max_output_tokens=max_output_tokens,
            quality_pass=quality_pass,
            seniority_pass=seniority_pass,
        )

        # ── Save result → DONE ────────────────────────────────────────────────
        # Refresh connection — it may have gone stale during the long OpenAI call.
        close_old_connections()
        
        company_name  = result.get("company_name", "")
        document_name = result.get("document_name", "")
        result["company_name"]  = company_name
        result["document_name"] = document_name
        
        rows = ExtractionRecord.objects.filter(pk=record_id).update(
            status=ExtractionRecord.STATUS_CHOICES[2][0],
            company_name=result.get("company_name", ""),
            document_name=result.get("document_name", ""),
            raw_json=result,
            error_message="",
        )

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        try:
            close_old_connections()
            ExtractionRecord.objects.filter(pk=record_id).update(
                status=ExtractionRecord.STATUS_CHOICES[3][0],
                error_message=error_msg,
            )
        except Exception as db_exc:
            # Log instead of silently swallowing — makes root cause visible in server logs.
            pass

    finally:
        # Always close the thread-local DB connection to avoid leaks
        connection.close()

class ArticlesOpenAISettingView(APIView):
    """
    GET  /api/articles/openai-settings/  — Return current user's settings
    POST /api/articles/openai-settings/  — Update current user's settings (partial)
    """

    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request, *args, **kwargs):
        setting, _ = ArticlesOpenAISetting.objects.get_or_create(user=request.user)
        return Response(ArticlesOpenAISettingSerializer(setting).data)

    def post(self, request, *args, **kwargs):
        setting, _ = ArticlesOpenAISetting.objects.get_or_create(user=request.user)
        serializer = ArticlesOpenAISettingSerializer(setting, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ArticleGlobalOpenAISettingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        setting = ArticleGlobalOpenAISetting.get_settings()

        serializer = ArticleGlobalOpenAISettingSerializer(setting)

        return Response(serializer.data)

    def post(self, request):
        setting = ArticleGlobalOpenAISetting.get_settings()

        serializer = ArticleGlobalOpenAISettingSerializer(
            setting,
            data=request.data,
            partial=True
        )

        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(serializer.data)

# ─────────────────────────────────────────────────────────────────────────────
# Extraction endpoint
# ─────────────────────────────────────────────────────────────────────────────

class ArticleExtractView(APIView):
    """
    POST api/v1/article-interpretation/extract/
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser]
    ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".rtf", ".odt", ".txt", ".md"}

    def post(self, request, *args, **kwargs):
        # ── 1. Validate file ──────────────────────────────────────────────────
        input_file = request.FILES.get("input_file")
        if not input_file:
            return Response(
                {"error": "'input_file' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ext = Path(input_file.name).suffix.lower()
        if ext not in self.ALLOWED_EXTENSIONS:
            return Response(
                {
                    "error": (
                        f"Unsupported file type '{ext}'. "
                        f"Allowed: {', '.join(sorted(self.ALLOWED_EXTENSIONS))}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── 2. Resolve per-user OpenAI settings ───────────────────────────────
        user_setting = getattr(request.user, "articles_openai_setting", None)
        model = getattr(user_setting, "default_model", None) or DEFAULT_MODEL
        max_chars = getattr(user_setting, "max_chars", None) or DEFAULT_MAX_CHARS
        reasoning_effort = getattr(user_setting, "reasoning_effort", None) or DEFAULT_REASONING_EFFORT
        quality_reasoning_effort = getattr(user_setting, "quality_reasoning_effort", None) or DEFAULT_QUALITY_REASONING_EFFORT
        max_output_tokens = getattr(user_setting, "max_output_tokens", None) or DEFAULT_MAX_OUTPUT_TOKENS
        quality_pass = getattr(user_setting, "quality_pass", True)
        seniority_pass = getattr(user_setting, "seniority_pass", True)

        api_key = getattr(settings, "OPENAI_API_KEY", None) or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return Response(
                {"error": "OpenAI API key is not configured on the server."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        file_bytes = input_file.read()
        file_name  = input_file.name

        # ── 4. Create DB record immediately (status=PENDING) ──────────────────
        record = ExtractionRecord.objects.create(
            user          = request.user if request.user.is_authenticated else None,
            original_name = file_name,
            status        = ExtractionRecord.STATUS_CHOICES[0][0],
        )

        # ── 5. Submit extraction to background thread pool ────────────────────────────
        _get_executor().submit(
            _run_extraction_thread,
            record_id = record.pk,
            file_name = file_name,
            file_bytes = file_bytes,
            model = model,
            api_key = api_key,
            max_chars = max_chars,
            reasoning_effort = reasoning_effort,
            quality_reasoning_effort = quality_reasoning_effort,
            max_output_tokens = max_output_tokens,
            quality_pass = quality_pass,
            seniority_pass = seniority_pass,
        )

        status_url = request.build_absolute_uri(
            f"/api/v1/articles/extract/{record.pk}/status/"
        )
        return Response(
            {
                "extraction_id": record.pk,
                "status":        ExtractionRecord.STATUS_CHOICES[0][0],
                "status_url":    status_url,
                "message":       "Extraction started. Poll status_url to track progress.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

class ExtractionStatusView(APIView):
    """
    GET api/v1/article-interpretation/extract/<pk>/status/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        record = get_object_or_404(ExtractionRecord, pk=pk, user=request.user)

        if record.status in (ExtractionRecord.STATUS_CHOICES[0][0], ExtractionRecord.STATUS_CHOICES[2][0]):
            return Response(
                {
                    "extraction_id": record.pk,
                    "status": record.status,
                    "message": "Extraction is in progress. Please poll again shortly.",
                },
                status=status.HTTP_202_ACCEPTED,
            )

        if record.status == ExtractionRecord.STATUS_CHOICES[3][0]:  # STATUS_FAILED
            return Response(
                {
                    "extraction_id": record.pk,
                    "status": "failed",
                    "error": record.error_message,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # STATUS_DONE — return full result
        return Response(
            {
                "extraction_id": record.pk,
                "status": "done",
                **record.raw_json,
            },
            status=status.HTTP_200_OK,
        )
        
# ─────────────────────────────────────────────────────────────────────────────
# List / Detail
# ─────────────────────────────────────────────────────────────────────────────

class ExtractionRecordListView(APIView):
    """
    GET api/v1/article-interpretation/extractions/

    Returns all ExtractionRecord rows belonging to request.user (newest first).
    Does NOT include raw_json to keep payload small.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        records = ExtractionRecord.objects.filter(user=request.user)
        return Response(ExtractionRecordListSerializer(records, many=True).data)


class ExtractionRecordDetailView(APIView):
    """
    GET   api/v1/article-interpretation/extractions/<pk>/
    PATCH api/v1/article-interpretation/extractions/<pk>/
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_record(self, pk, user):
        return get_object_or_404(ExtractionRecord, pk=pk, user=user)

    def get(self, request, pk, *args, **kwargs):
        record = self._get_record(pk, request.user)
        return Response(ExtractionRecordDetailSerializer(record).data)

    def patch(self, request, pk, *args, **kwargs):
        record = self._get_record(pk, request.user)

        # Only allow editing completed records
        if record.status != ExtractionRecord.STATUS_CHOICES[2][0]:  # STATUS_DONE
            return Response(
                {
                    "error": (
                        f"Cannot edit extraction #{pk} — "
                        f"current status is '{record.status}'. "
                        f"Only 'done' extractions can be edited."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        # ── Validate request body ─────────────────────────────────────────────
        body_serializer = SecurityUpdateSerializer(data=request.data)
        body_serializer.is_valid(raise_exception=True)
        data = body_serializer.validated_data

        target_array = data["target"]          # "securities" or "cap_table" — always set by serializer
        sec_index    = data["security_index"]
        new_fields   = data["fields"]
        req_sec_name = data.get("security_name", "")

        # ── Locate the target security inside raw_json ────────────────────────

        name_key = "security_name" if target_array == "securities" else "series_name"
        rows = (record.raw_json or {}).get(target_array, [])
        # securities = (record.raw_json or {}).get("securities", [])

        if sec_index >= len(rows):
            return Response(
                {
                    "error": (
                        f"security_index {sec_index} is out of range. "
                        f"This extraction's '{target_array}' array has {len(rows)} rows (0-based)."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_security = rows[sec_index]
        # actual_sec_name = target_security.get("security_name", "") or req_sec_name
        actual_sec_name = target_security.get(name_key, "") or req_sec_name


        # Optional sanity check: warn if provided security_name doesn't match
        if req_sec_name and actual_sec_name and req_sec_name != actual_sec_name:
            name_warning = (
                f"Provided security_name '{req_sec_name}' does not match "
                f"'{actual_sec_name}' at index {sec_index}. "
                f"Fields were applied to index {sec_index}."
            )
        else:
            name_warning = None

        # ── Apply changes + build audit rows ─────────────────────────────────
        audit_entries = []
        for field_name, new_value in new_fields.items():
            old_value     = str(target_security.get(field_name, ""))
            new_value_str = str(new_value)

            if old_value == new_value_str:
                continue  # nothing changed — skip

            # Patch the field in-place
            target_security[field_name] = new_value_str

            # Record audit entry
            audit_entries.append(
                SecurityFieldAudit(
                    extraction     = record,
                    changed_by     = request.user if request.user.is_authenticated else None,
                    security_index = sec_index,
                    security_name  = actual_sec_name,
                    field_name     = field_name,
                    old_value      = old_value,
                    new_value      = new_value_str,
                )
            )

        if not audit_entries:
            return Response(
                {"detail": "No fields were changed (submitted values are identical to current values)."},
                status=status.HTTP_200_OK,
            )

        # ── Persist changes ───────────────────────────────────────────────────
        SecurityFieldAudit.objects.bulk_create(audit_entries)
        record.save()  # triggers auto_now on updated_at + persists raw_json

        response_data = ExtractionRecordDetailSerializer(record).data
        response_data["fields_changed"] = len(audit_entries)
        if name_warning:
            response_data["warning"] = name_warning

        return Response(response_data, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────────────────────────

class ExtractionAuditView(APIView):
    """
    GET api/v1/article-interpretation/extractions/<pk>/audit/

    Returns the full audit log for one extraction (newest change first).
    Only accessible by the owner of the extraction.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        record = get_object_or_404(ExtractionRecord, pk=pk, user=request.user)
        logs   = record.audit_logs.select_related("changed_by").all()
        return Response(SecurityFieldAuditSerializer(logs, many=True).data)