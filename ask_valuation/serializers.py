from rest_framework import serializers
from .models import Guide, GuideChunk, ValuationOpenAISetting, ValuationSession, ValuationMessage

class GuideSerializer(serializers.ModelSerializer):
    pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = Guide
        fields = [
            'id', 'name', 'year', 'pdf_file', 'pdf_url',
            'is_active', 'total_pages', 'total_chunks',
            'processing_status', 'processing_error',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'pdf_url', 'total_pages', 'total_chunks',
            'processing_status', 'processing_error',
            'created_at', 'updated_at',
        ]
        extra_kwargs = {
            'pdf_file': {'write_only': True, 'required': False},
        }

    def get_pdf_url(self, obj):
        request = self.context.get('request')
        if obj.pdf_file and hasattr(obj.pdf_file, 'url'):
            url = obj.pdf_file.url
            return request.build_absolute_uri(url) if request else url
        return None


class GuideListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for guide selection lists."""

    class Meta:
        model = Guide
        fields = ['id', 'name', 'year', 'pdf_file', 'is_active', 'total_pages', 'processing_status']

class ValuationMessageSerializer(serializers.ModelSerializer):
    sources = serializers.SerializerMethodField()

    class Meta:
        model = ValuationMessage
        fields = [
            'id', 'session', 'role', 'content', 'sources',
            'guides_used', 'edited', 'edited_at',
            'tokens_used', 'created_at',
        ]
        read_only_fields = [
            'id', 'session', 'role', 'guides_used',
            'edited', 'edited_at',
            'tokens_used', 'created_at',
        ]

    def get_sources(self, obj):
        raw = obj.sources or []
        seen = {}
        for entry in raw:
            gid = entry.get('guide_id')
            if gid is not None and gid not in seen:
                seen[gid] = {
                    'guide_id': gid,
                    'guide_name': entry.get('guide_name', ''),
                }
        return list(seen.values())

class ValuationSessionSerializer(serializers.ModelSerializer):
    messages = ValuationMessageSerializer(many=True, read_only=True)
    selected_guides = GuideListSerializer(many=True, read_only=True)
    selected_guide_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Guide.objects.filter(is_active=True),
        source='selected_guides',
        write_only=True,
        required=False,
    )

    class Meta:
        model = ValuationSession
        fields = [
            'id', 'owner', 'title', 'system_prompt', 'selected_guides', 'selected_guide_ids',
            'messages', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'owner', 'selected_guides', 'created_at', 'updated_at']

    def create(self, validated_data):
        guides = validated_data.pop('selected_guides', [])
        session = super().create(validated_data)
        if guides:
            session.selected_guides.set(guides)
        return session

    def update(self, instance, validated_data):
        guides = validated_data.pop('selected_guides', None)
        session = super().update(instance, validated_data)
        if guides is not None:
            session.selected_guides.set(guides)
        return session


class ValuationSessionListSerializer(serializers.ModelSerializer):
    """Lightweight list serializer — no nested messages."""
    selected_guides = GuideListSerializer(many=True, read_only=True)
    message_count = serializers.SerializerMethodField()

    class Meta:
        model = ValuationSession
        fields = [
            'id', 'title', 'system_prompt', 'selected_guides', 'message_count',
            'created_at', 'updated_at',
        ]

    def get_message_count(self, obj):
        return obj.messages.count()
    
class ValuationOpenAISettingSerializer(serializers.ModelSerializer):

    class Meta:
        model = ValuationOpenAISetting
        fields = "__all__"