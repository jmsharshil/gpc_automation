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
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser

from .models import ArticlesOpenAISetting, ArticleGlobalOpenAISetting, ExtractionRecord, SecurityFieldAudit
from .serializers import ArticlesOpenAISettingSerializer, ArticleGlobalOpenAISettingSerializer, ExtractionRecordListSerializer, ExtractionRecordDetailSerializer, SecurityFieldAuditSerializer, SecurityUpdateSerializer
from . import openai_extractor
from rest_framework import status, permissions
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404



# Fallback defaults used when no per-user setting exists
# DEFAULT_MODEL     = "gpt-4.1"
# DEFAULT_MAX_CHARS = 120000
# DEFAULT_QUALITY_PASS = False

DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_QUALITY_REASONING_EFFORT = "high"
DEFAULT_MAX_CHARS = 120000
DEFAULT_MAX_OUTPUT_TOKENS = 8000


# ─────────────────────────────────────────────────────────────────────────────
# Settings endpoint
# ─────────────────────────────────────────────────────────────────────────────

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

        # ── 3. Run extraction ─────────────────────────────────────────────────
        try:
            result = openai_extractor.run_extraction(
                file_name=input_file.name,
                file_bytes=input_file.read(),
                model=model,
                api_key=api_key,
                max_chars=max_chars,
                reasoning_effort=reasoning_effort,
                quality_reasoning_effort=quality_reasoning_effort,
                max_output_tokens=max_output_tokens,
                quality_pass=quality_pass,
                seniority_pass=seniority_pass,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except RuntimeError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except Exception as exc:
            return Response(
                {"error": f"Extraction failed: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # ── 4. Save result to DB ──────────────────────────────────────────────
        record = ExtractionRecord.objects.create(
            user          = request.user if request.user.is_authenticated else None,
            original_name = input_file.name,
            company_name  = result.get("company_name", ""),
            document_name = result.get("document_name", ""),
            raw_json      = result,
        )

        # ── 5. Return result with extraction_id ───────────────────────────────
        return Response(
            {"extraction_id": record.pk, **result},
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

        # ── Validate request body ─────────────────────────────────────────────
        body_serializer = SecurityUpdateSerializer(data=request.data)
        body_serializer.is_valid(raise_exception=True)
        data = body_serializer.validated_data

        sec_index    = data["security_index"]
        new_fields   = data["fields"]
        req_sec_name = data.get("security_name", "")

        # ── Locate the target security inside raw_json ────────────────────────
        securities = (record.raw_json or {}).get("securities", [])

        if sec_index >= len(securities):
            return Response(
                {
                    "error": (
                        f"security_index {sec_index} is out of range. "
                        f"This extraction has {len(securities)} securities (0-based)."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_security = securities[sec_index]
        actual_sec_name = target_security.get("security_name", "") or req_sec_name

        # Optional sanity check: warn if provided security_name doesn't match
        if req_sec_name and actual_sec_name and req_sec_name != actual_sec_name:
            # We still proceed — just log the mismatch in the response
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
            old_value = str(target_security.get(field_name, ""))
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