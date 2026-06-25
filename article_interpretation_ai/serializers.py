from rest_framework import serializers
from .models import ArticlesOpenAISetting, ArticleGlobalOpenAISetting, ExtractionRecord, SecurityFieldAudit


class ArticlesOpenAISettingSerializer(serializers.ModelSerializer):
    class Meta:
        model  = ArticlesOpenAISetting
        fields = [
            "default_model", "reasoning_effort", "quality_reasoning_effort",
            "max_output_tokens", "max_chars", "quality_pass", "seniority_pass",
        ]

class ArticleGlobalOpenAISettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ArticleGlobalOpenAISetting
        fields = "__all__"
        read_only_fields = ("updated_at",)
        
class ExtractionRecordListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list view — no raw_json to keep payload small."""
    user = serializers.StringRelatedField(read_only=True)

    class Meta:
        model  = ExtractionRecord
        fields = [
            "id", "user", "original_name", "company_name",
            "document_name", "status", "error_message","created_at", "updated_at",
        ]


class ExtractionRecordDetailSerializer(serializers.ModelSerializer):
    """Full serializer including raw_json, used in detail + after extract/update."""
    user = serializers.StringRelatedField(read_only=True)

    class Meta:
        model  = ExtractionRecord
        fields = [
            "id", "user", "original_name", "company_name",
            "document_name",  "status", "error_message","raw_json", "created_at", "updated_at",
        ]


# ─────────────────────────────────────────────────────────────────────────────
# PATCH request body serializer
# ─────────────────────────────────────────────────────────────────────────────

class SecurityUpdateSerializer(serializers.Serializer):
    """
    Validates the PATCH body:
    {
        "target":         "securities",    // optional — "securities" (default) or "cap_table"
        "security_index": 0,               // required — 0-based position in the target array
        "security_name":  "Series A ...", // optional — used for validation/readability
        "fields": {
            "conversion_ratio": "1:1.26",
            "oip_original_issue_price": "$1.00"
        }
    }
    """
    TARGET_SECURITIES = "securities"
    TARGET_CAP_TABLE  = "cap_table"
    TARGET_CHOICES    = [TARGET_SECURITIES, TARGET_CAP_TABLE]

    target         = serializers.ChoiceField(
        choices=TARGET_CHOICES,
        default=TARGET_SECURITIES,
        required=False,
        help_text="Which array inside raw_json to patch: 'securities' or 'cap_table'.",
    )
    security_index = serializers.IntegerField(min_value=0)
    security_name  = serializers.CharField(required=False, allow_blank=True)
    fields         = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        allow_empty=False,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Audit log serializer
# ─────────────────────────────────────────────────────────────────────────────

class SecurityFieldAuditSerializer(serializers.ModelSerializer):
    changed_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model  = SecurityFieldAudit
        fields = [
            "id", "changed_by", "security_index", "security_name",
            "field_name", "old_value", "new_value", "changed_at",
        ]
