import logging
import concurrent.futures

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.http import StreamingHttpResponse

from rest_framework import generics, status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from openai import OpenAI
from django.conf import settings

from .models import Guide, ValuationSession, ValuationMessage
from .serializers import (
    GuideSerializer,
    GuideListSerializer,
    ValuationSessionSerializer,
    ValuationSessionListSerializer,
    ValuationMessageSerializer,
)
from .rag_utils import (
    process_guide_pdf,
    get_relevant_chunks_for_guides,
)
from crm_chat.models import UserOpenAISetting

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 2000
DEFAULT_MAX_CONTEXT_CHUNKS = 20


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _get_openai_settings(user):
    """Return (model, temperature, max_tokens, max_context_chunks) from UserOpenAISetting."""
    try:
        s = UserOpenAISetting.objects.get(user=user)
        return (
            s.default_model or DEFAULT_MODEL,
            float(s.temperature) if s.temperature is not None else DEFAULT_TEMPERATURE,
            int(s.max_tokens) if s.max_tokens else DEFAULT_MAX_TOKENS,
            int(s.max_context_chunks) if s.max_context_chunks else DEFAULT_MAX_CONTEXT_CHUNKS,
        )
    except UserOpenAISetting.DoesNotExist:
        return DEFAULT_MODEL, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS, DEFAULT_MAX_CONTEXT_CHUNKS


def _run_guide_processing(guide) -> int:
    """Run process_guide_pdf in a fresh event loop thread (Windows-safe)."""
    return process_guide_pdf(guide)


def _build_rag_context(guide_ids: list,user_text: str,max_chunks: int = DEFAULT_MAX_CONTEXT_CHUNKS,) -> tuple[list, list]:
    """
    Embed the user query, retrieve the top-k chunks across the selected guides,
    and return (context_parts, sources).

    sources: one entry per guide — no page numbers or chunk indexes.
    """
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    emb_response = client.embeddings.create(
        model="text-embedding-3-small",
        input=[user_text],
        encoding_format="float",
    )
    query_embedding = emb_response.data[0].embedding

    # Pass query_text so hybrid search can keyword-match paragraph refs (e.g. 8.30, 6.54)
    chunks = get_relevant_chunks_for_guides(
        guide_ids, query_embedding, max_chunks, query_text=user_text
    )

    context_parts: list[str] = []
    seen_guides: dict[int, str] = {}  # guide_id → guide_name

    for chunk in chunks:
        context_parts.append(
            f"[{chunk.guide.name} – Page {chunk.page_number}]\n{chunk.text}"
        )
        if chunk.guide_id not in seen_guides:
            seen_guides[chunk.guide_id] = chunk.guide.name

    sources = [
        {"guide_id": gid, "guide_name": gname}
        for gid, gname in seen_guides.items()
    ]

    return context_parts, sources


def _build_messages_payload(
    session: ValuationSession,
    user_text: str,
    context_parts: list,
    exclude_message_pk=None,
) -> list:
    """
    Assemble the full OpenAI messages payload.
    """
    guide_names = ", ".join(g.name for g in session.selected_guides.all())
    system_prompt = VALUATION_SYSTEM_PROMPT
    messages_payload: list[dict] = []

    if system_prompt:
        messages_payload.append({"role": "system", "content": system_prompt})

    if context_parts:
        separator = "\n\n" + "=" * 60 + "\n\n"
        rag_block = separator.join(context_parts)
        rag_content = (
            f"\n\n📄 Selected Guidelines: {guide_names}"
            f"\n\n🔗 Relevant excerpts from the guidelines:\n\n{rag_block}"
            f"\n\n---\n"                          # ← separator
            f"CITATION INSTRUCTIONS: For every claim you make, cite it inline "
            f"immediately after the relevant sentence using this exact format: "
            f"(Source: <Guide Name>, Page <number>). "
            f"At the end of your response, add a section titled '📄 References' "
            f"listing each unique source used: Guide Name — Pages X, Y, Z."
        )

        if messages_payload and messages_payload[0]["role"] == "system":
            messages_payload[0]["content"] += rag_content
        else:
            messages_payload.insert(0,
                {
                    "role": "system",
                    "content": f"Answer ONLY based on the following document excerpts:\n{rag_content}",
                },
            )

    # Last 10 messages — exclude the just-created user message to avoid duplication
    recent = list(session.messages.order_by("-created_at")[:10])[::-1]
    for m in recent:
        if exclude_message_pk is not None and m.pk == exclude_message_pk:
            continue
        messages_payload.append({"role": m.role, "content": m.content or ""})

    messages_payload.append({"role": "user", "content": user_text})
    return messages_payload


VALUATION_SYSTEM_PROMPT = """
You are a senior valuation expert assistant specializing in analyzing valuation guides, professional standards, technical manuals, methodologies, and reference documents. Your primary responsibility is to answer the user's question using ONLY the retrieved document context provided to you.

IMPORTANT RULES:
- Use only information found in the retrieved document context.
- Never invent facts, examples, calculations, page numbers, or references.
- Never use external knowledge unless explicitly requested.
- If the retrieved content does not contain enough information to answer the question, clearly state this.

RESPONSE FORMAT:

# Answer

Begin with a direct answer to the user's question.

Do NOT start with phrases such as 'Based on the provided context' or 'According to the document'.

## Overview
Provide a clear explanation of the topic.

## Detailed Explanation
Provide a thorough explanation of the relevant guidance, methodology, principles, and requirements.

For every concept discussed, explain:
1. What it means.
2. Why it is important.
3. When it applies.
4. How it is used in practice.
5. Key assumptions.
6. Limitations.
7. Exceptions.
8. Practical implications.

Expand the explanation substantially instead of merely summarizing retrieved text. Assume the user prefers a detailed professional explanation. Generate approximately 2-3 times more detail than a standard answer whenever sufficient source material exists.

## Key Principles or Requirements
Present important requirements, criteria, or guidance in bullet points.

## Practical Application
Explain how the guidance would be applied in real-world valuation assignments and professional practice.

## Examples and Illustrations from the Guide
If the retrieved content contains examples, illustrations, calculations, case studies, scenarios, or tables:
- Include them.
- Explain them step-by-step.
- Explain their significance.
- Explain how they relate to the user's question.

If no example exists, explicitly state:
'No specific example or illustration was found in the retrieved sections of the guide.'

## Important Considerations
Discuss assumptions, limitations, caveats, professional judgment areas, exceptions, and risks of misapplication, only if supported by the retrieved content.

## Sources
For every major section, include source references.

Use the format:
Source: Page X

or

Source: Pages X, Y, Z

If a document name is available:

Source: [Document Name], Page X

Never invent page numbers.
Always preserve source traceability.

When multiple retrieved chunks are available:
- Combine information intelligently.
- Remove duplication.
- Present one coherent answer.
- Preserve all relevant page references.

If formulas, calculations, valuation models, or methodologies appear in the retrieved content:
- Explain each component.
- Explain how it is used.
- Explain the interpretation.
- Walk through any available example calculations.

Use tables whenever they improve readability.

Act like a senior valuation consultant and instructor. Your objective is not only to answer the question but also to help the user understand the reasoning, application, implications, and professional interpretation of the guidance.

# Summary

After every answer, provide a Summary section containing 7-10 concise bullet points highlighting the most important takeaways.

If the retrieved content is insufficient, state:

'The retrieved sections of the guide do not provide enough information to fully answer this question.'

Writing style must be professional, consultant-level, educational, highly detailed, and easy to understand.
"""

# ─────────────────────────────────────────────
# Guide CRUD
# ─────────────────────────────────────────────

class GuideListCreateAPIView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_serializer_class(self):
        if self.request.method == "GET":
            return GuideListSerializer
        return GuideSerializer

    def get_queryset(self):
        return Guide.objects.filter(is_active=True).order_by("name")

    def create(self, request, *args, **kwargs):
        serializer = GuideSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        guide = serializer.save()

        if guide.pdf_file:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(_run_guide_processing, guide)
                    future.result()
            except Exception as exc:
                logger.exception(
                    "Guide processing failed for guide_id=%s: %s", guide.id, exc
                )
                guide.processing_status = "failed"
                guide.processing_error = str(exc)
                guide.save(update_fields=["processing_status", "processing_error"])

        return Response(
            GuideSerializer(guide, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class GuideDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    serializer_class = GuideSerializer
    queryset = Guide.objects.all()

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        guide = self.get_object()

        serializer = self.get_serializer(guide, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        guide = serializer.save()

        if request.FILES.get("pdf_file"):
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(_run_guide_processing, guide)
                    future.result()
            except Exception as exc:
                logger.exception(
                    "Guide re-processing failed for guide_id=%s: %s", guide.id, exc
                )

        return Response(GuideSerializer(guide, context={"request": request}).data)


class GuideReprocessAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        guide = get_object_or_404(Guide, pk=pk)
        if not guide.pdf_file:
            return Response(
                {"error": "No PDF file attached to this guide."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(_run_guide_processing, guide)
                num_chunks = future.result()

            return Response(
                {
                    "message": f'Guide "{guide.name}" re-processed successfully.',
                    "total_chunks": num_chunks,
                    "total_pages": guide.total_pages,
                }
            )
        except Exception as exc:
            logger.exception("Reprocess failed for guide_id=%s: %s", guide.id, exc)
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ─────────────────────────────────────────────
# Valuation Session CRUD
# ─────────────────────────────────────────────

class ValuationSessionListCreateAPIView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method == "GET":
            return ValuationSessionListSerializer
        return ValuationSessionSerializer

    def get_queryset(self):
        return ValuationSession.objects.filter(
            owner=self.request.user
        ).prefetch_related("selected_guides")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class ValuationSessionDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ValuationSessionSerializer

    def get_queryset(self):
        return ValuationSession.objects.filter(
            owner=self.request.user
        ).prefetch_related("selected_guides", "messages")


class UpdateSessionGuidesAPIView(APIView):
    """PATCH selected guides for an existing session."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser]

    def patch(self, request, session_pk):
        session = get_object_or_404(ValuationSession, pk=session_pk, owner=request.user)
        guide_ids = request.data.get("guide_ids", [])

        if not isinstance(guide_ids, list) or not guide_ids:
            return Response(
                {"error": "guide_ids must be a non-empty list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        guides = Guide.objects.filter(id__in=guide_ids, is_active=True)
        if not guides.exists():
            return Response(
                {"error": "No valid active guides found for the given IDs."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session.selected_guides.set(guides)
        session.updated_at = timezone.now()
        session.save(update_fields=["updated_at"])

        return Response(ValuationSessionListSerializer(session).data)


# ─────────────────────────────────────────────
# Messaging
# ─────────────────────────────────────────────

class AskQuestionAPIView(APIView):
    """Non-streaming ask — mirrors SendMessageAPIView in crm_chat."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def post(self, request, session_pk):
        session = get_object_or_404(ValuationSession, pk=session_pk, owner=request.user)

        user_text = request.data.get("content", "").strip()
        if not user_text:
            return Response(
                {"error": "content is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        guide_ids = list(session.selected_guides.values_list("id", flat=True))
        if not guide_ids:
            return Response(
                {"error": "Please select at least one guideline before asking a question."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Save user message first (needed for history deduplication below)
        user_msg = ValuationMessage.objects.create(
            session=session,
            role="user",
            content=user_text,
            guides_used=guide_ids,
        )

        # Resolve user OpenAI settings
        ai_model, ai_temp, ai_max_tokens, ai_max_chunks = _get_openai_settings(request.user)

        # Build RAG context
        context_parts, sources = [], []
        try:
            context_parts, sources = _build_rag_context(guide_ids, user_text, ai_max_chunks)
        except Exception as exc:
            logger.error("RAG retrieval failed for session_id=%s: %s", session.id, exc)

        # Build prompt — exclude the just-saved user_msg from history to avoid duplication
        messages_payload = _build_messages_payload(
            session, user_text, context_parts, exclude_message_pk=user_msg.pk
        )

        # Call OpenAI
        try:
            client = OpenAI(api_key=settings.OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model=ai_model,
                messages=messages_payload,
                temperature=ai_temp,
                max_tokens=ai_max_tokens,
            )
            assistant_text = resp.choices[0].message.content or ""
            tokens_used = resp.usage.total_tokens if resp.usage else None

        except Exception as exc:
            logger.exception("OpenAI call failed for session_id=%s: %s", session.id, exc)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        # Save assistant message
        assistant_msg = ValuationMessage.objects.create(
            session=session,
            role="assistant",
            content=assistant_text,
            sources=sources,
            guides_used=guide_ids,
            tokens_used=tokens_used,
        )

        session.updated_at = timezone.now()
        session.save(update_fields=["updated_at"])

        return Response(
            ValuationMessageSerializer(assistant_msg).data,
            status=status.HTTP_201_CREATED,
        )


class AskQuestionStreamAPIView(APIView):
    """Streaming ask (SSE) — mirrors StreamingChatAPIView in crm_chat."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def post(self, request, session_pk):
        session = get_object_or_404(ValuationSession, pk=session_pk, owner=request.user)

        user_text = request.data.get("content", "").strip()
        if not user_text:
            return Response(
                {"error": "content is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        guide_ids = list(session.selected_guides.values_list("id", flat=True))
        if not guide_ids:
            return Response(
                {"error": "Please select at least one guideline before asking a question."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Save user message first
        user_msg = ValuationMessage.objects.create(
            session=session,
            role="user",
            content=user_text,
            guides_used=guide_ids,
        )

        # Resolve user OpenAI settings (before RAG, consistent with non-streaming path)
        ai_model, ai_temp, ai_max_tokens, ai_max_chunks = _get_openai_settings(request.user)

        # Build RAG context using the resolved max_chunks (was using default previously)
        context_parts, sources = [], []
        try:
            context_parts, sources = _build_rag_context(guide_ids, user_text, ai_max_chunks)
        except Exception as exc:
            logger.error(
                "RAG retrieval failed (stream) for session_id=%s: %s", session.id, exc
            )

        # Build prompt — exclude just-saved user_msg from history
        messages_payload = _build_messages_payload(
            session, user_text, context_parts, exclude_message_pk=user_msg.pk
        )

        def event_stream():
            full_text: list[str] = []
            client = OpenAI(api_key=settings.OPENAI_API_KEY)

            try:
                with client.chat.completions.create(
                    model=ai_model,
                    messages=messages_payload,
                    temperature=ai_temp,
                    max_tokens=ai_max_tokens,
                    stream=True,
                ) as stream:
                    for chunk in stream:
                        delta = chunk.choices[0].delta if chunk.choices else None
                        if delta and delta.content:
                            piece = delta.content
                            full_text.append(piece)
                            escaped = piece.replace("\n", "\\n")
                            yield f"data: {escaped}\n\n"

                # Stream finished — save assistant message (mirrors crm_chat)
                assistant_text = "".join(full_text)
                ValuationMessage.objects.create(
                    session=session,
                    role="assistant",
                    content=assistant_text,
                    sources=sources,
                    guides_used=guide_ids,
                    # tokens_used not available from streaming response
                )
                session.updated_at = timezone.now()
                session.save(update_fields=["updated_at"])

            except Exception as exc:
                logger.exception(
                    "Streaming failed for session_id=%s: %s", session.id, exc
                )
                yield f"data: [ERROR] {str(exc)}\n\n"

            finally:
                yield "data: [DONE]\n\n"

        response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


class EditMessageAPIView(APIView):
    """Edit a user message and regenerate the assistant reply."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def patch(self, request, session_pk, message_pk):
        session = get_object_or_404(ValuationSession, pk=session_pk, owner=request.user)
        msg = get_object_or_404(ValuationMessage, pk=message_pk, session=session)

        if msg.role != "user":
            return Response(
                {"error": "Only user messages can be edited."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        new_content = request.data.get("content", "").strip()
        if not new_content:
            return Response(
                {"error": "content is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        # Update message content
        msg.content = new_content
        msg.edited = True
        msg.edited_at = timezone.now()
        msg.save()

        # Delete assistant messages that came *after* this user message.
        # Use created_at__gt (strictly after) so we never accidentally delete
        # the edited user message itself if timestamps collide.
        ValuationMessage.objects.filter(
            session=session,
            role="assistant",
            created_at__gt=msg.created_at,
        ).delete()

        guide_ids = list(session.selected_guides.values_list("id", flat=True))
        if not guide_ids:
            return Response(
                {"error": "No guides selected in this session."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Re-run RAG + re-answer
        ai_model, ai_temp, ai_max_tokens, ai_max_chunks = _get_openai_settings(request.user)

        context_parts, sources = [], []
        try:
            context_parts, sources = _build_rag_context(guide_ids, new_content, ai_max_chunks)
        except Exception as exc:
            logger.error(
                "RAG retrieval failed (edit) for session_id=%s: %s", session.id, exc
            )

        # Exclude the edited message itself from history (it's appended as the
        # final user turn by _build_messages_payload)
        messages_payload = _build_messages_payload(
            session, new_content, context_parts, exclude_message_pk=msg.pk
        )

        try:
            client = OpenAI(api_key=settings.OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model=ai_model,
                messages=messages_payload,
                temperature=ai_temp,
                max_tokens=ai_max_tokens,
            )
            assistant_text = resp.choices[0].message.content or ""
            tokens_used = resp.usage.total_tokens if resp.usage else None

        except Exception as exc:
            logger.exception(
                "OpenAI call failed (edit) for session_id=%s: %s", session.id, exc
            )
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        assistant_msg = ValuationMessage.objects.create(
            session=session,
            role="assistant",
            content=assistant_text,
            sources=sources,
            guides_used=guide_ids,
            tokens_used=tokens_used,
        )

        session.updated_at = timezone.now()
        session.save(update_fields=["updated_at"])

        return Response(ValuationMessageSerializer(assistant_msg).data)


# ─────────────────────────────────────────────
# Message list
# ─────────────────────────────────────────────

class ValuationMessageListAPIView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ValuationMessageSerializer

    def get_queryset(self):
        session_pk = self.kwargs["session_pk"]
        session = get_object_or_404(ValuationSession, pk=session_pk, owner=self.request.user)
        return session.messages.order_by("created_at")


# ─────────────────────────────────────────────
# Delete Session
# ─────────────────────────────────────────────

class DeleteSessionAPIView(APIView):
    """Delete a valuation session and all its messages."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk):
        session = get_object_or_404(ValuationSession, pk=pk, owner=request.user)
        session_title = session.title or f"Session {session.pk}"
        message_count = session.messages.count()

        # CASCADE on the FK will auto-delete messages, but we log the count
        session.delete()

        return Response(
            {
                "message": f'Session "{session_title}" deleted successfully.',
                "deleted_messages": message_count,
            },
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────
# Delete Guide
# ─────────────────────────────────────────────

class DeleteGuideAPIView(APIView):
    """Delete a guide, its chunks, and the uploaded PDF file."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk):
        guide = get_object_or_404(Guide, pk=pk)
        guide_name = guide.name
        chunk_count = guide.chunks.count()

        # Delete the physical PDF file from storage
        if guide.pdf_file:
            try:
                guide.pdf_file.delete(save=False)
            except Exception as exc:
                logger.warning(
                    "Could not delete PDF file for guide_id=%s: %s", pk, exc
                )

        # CASCADE on the FK will auto-delete chunks
        guide.delete()

        return Response(
            {
                "message": f'Guide "{guide_name}" deleted successfully.',
                "deleted_chunks": chunk_count,
            },
            status=status.HTTP_200_OK,
        )
        
class OpenGuideChatAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        guide_ids = request.data.get("guide_ids", [])

        if not isinstance(guide_ids, list) or not guide_ids:
            return Response(
                {"error": "guide_ids must be a non-empty list"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        guides = list(
            Guide.objects.filter(
                id__in=guide_ids,
                is_active=True
            )
        )

        if len(guides) != len(set(guide_ids)):
            return Response(
                {"error": "One or more guides are invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        requested_ids = set(guide_ids)

        # Find existing session with exact same guides
        sessions = (
            ValuationSession.objects
            .filter(owner=request.user)
            .prefetch_related("selected_guides")
        )

        for session in sessions:
            session_ids = set(
                session.selected_guides.values_list(
                    "id",
                    flat=True
                )
            )

            if session_ids == requested_ids:
                return Response({
                    "session_id": session.id,
                    "existing": True
                })

        # Create new session
        title = " + ".join(
            g.name for g in guides
        )

        session = ValuationSession.objects.create(
            owner=request.user,
            title=title,
            system_prompt=VALUATION_SYSTEM_PROMPT
        )

        session.selected_guides.set(guides)

        return Response({
            "session_id": session.id,
            "existing": False
        })
        
class ClearSessionAPIView(APIView):
    """
    Delete all messages from a session but keep the session itself.
    """

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk):
        session = get_object_or_404(
            ValuationSession,
            pk=pk,
            owner=request.user
        )

        deleted_count = session.messages.count()

        session.messages.all().delete()

        session.updated_at = timezone.now()
        session.save(update_fields=["updated_at"])

        return Response(
            {
                "message": "Chat history cleared successfully.",
                "deleted_messages": deleted_count,
                "session_id": session.id,
            },
            status=status.HTTP_200_OK,
        )